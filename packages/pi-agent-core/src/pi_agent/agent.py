"""The ``Agent`` class — a faithful port of ``packages/agent/src/agent.ts``.

The subtle part is where conversation state actually lives.  The loop works on a
**copy** of the message list, so ``Agent`` rebuilds its own history from the event
stream: ``message_end`` appends to ``state.messages`` (the only place history
grows), ``turn_end`` records the latest error, and ``agent_end`` only clears the
streaming slot.  A failed run is turned into synthetic events so subscribers
always observe a well-formed turn.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Union

from pi_ai import (
    AbortController,
    AbortSignal,
    AssistantMessage,
    Cost,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    Usage,
)
from pi_ai.types import Message

from .loop import run_agent_loop, run_agent_loop_continue
from .stream_fn import get_default_stream_fn
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    PrepareNextTurnContext,
    StreamFn,
    TransformContextFn,
)

QueueMode = Literal["one-at-a-time", "all"]

_EMPTY_USAGE = Usage(cost=Cost())


class PendingMessageQueue:
    """A message queue whose drain behaviour depends on the delivery mode."""

    __slots__ = ("mode", "_messages")

    def __init__(self, mode: QueueMode = "one-at-a-time") -> None:
        self.mode: QueueMode = mode
        self._messages: list[AgentMessage] = []

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return bool(self._messages)

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            drained, self._messages = self._messages, []
            return drained
        if not self._messages:
            return []
        first, self._messages = self._messages[0], self._messages[1:]
        return [first]

    def clear(self) -> None:
        self._messages = []


@dataclass
class AgentState:
    """Observable agent state.  ``messages``/``tools`` are replaced, not mutated."""

    messages: list[AgentMessage] = field(default_factory=list)
    model: Model | None = None
    thinking_level: str = "off"
    system_prompt: str = ""
    tools: list[AgentTool] = field(default_factory=list)
    is_streaming: bool = False
    streaming_message: AgentMessage | None = None
    pending_tool_calls: set[str] = field(default_factory=set)
    error_message: str | None = None


@dataclass
class _ActiveRun:
    abort_controller: AbortController
    future: asyncio.Future


Listener = Callable[..., Any]


class Agent:
    """Runs the agent loop and maintains conversation state."""

    def __init__(
        self,
        model: Model,
        system_prompt: str,
        tools: list[AgentTool],
        convert_to_llm: Callable[[list[AgentMessage]], Awaitable[list[Message]]],
        *,
        stream_fn: StreamFn | None = None,
        session_id: str | None = None,
        tool_execution: Literal["parallel", "sequential"] = "parallel",
        steering_mode: QueueMode = "one-at-a-time",
        follow_up_mode: QueueMode = "one-at-a-time",
        api_key: str | None = None,
        get_api_key: Callable[[str], Awaitable[str | None]] | None = None,
        transform_context: TransformContextFn | None = None,
        before_tool_call: Callable[..., Awaitable[BeforeToolCallResult | None]] | None = None,
        after_tool_call: Callable[..., Awaitable[AfterToolCallResult | None]] | None = None,
        prepare_next_turn: Callable[[PrepareNextTurnContext], Awaitable[AgentLoopTurnUpdate | None]]
        | None = None,
        should_stop_after_turn: Callable[[PrepareNextTurnContext], Awaitable[bool]] | None = None,
        stream_options: StreamOptions | None = None,
        messages: list[AgentMessage] | None = None,
        thinking_level: str = "off",
    ) -> None:
        self._state = AgentState(
            messages=list(messages or []),
            model=model,
            thinking_level=thinking_level,
            system_prompt=system_prompt,
            tools=list(tools),
        )
        self.session_id = session_id
        self.tool_execution = tool_execution
        self.convert_to_llm = convert_to_llm
        self.stream_fn = stream_fn or get_default_stream_fn()
        self.api_key = api_key
        self.get_api_key = get_api_key
        self.transform_context = transform_context
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.prepare_next_turn = prepare_next_turn
        self.should_stop_after_turn = should_stop_after_turn
        self.stream_options = stream_options

        self.steering_queue = PendingMessageQueue(steering_mode)
        self.follow_up_queue = PendingMessageQueue(follow_up_mode)

        self._listeners: list[tuple[Listener, int]] = []
        self._active_run: _ActiveRun | None = None

    # -- state -------------------------------------------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    def set_messages(self, messages: list[AgentMessage]) -> None:
        """Replace the transcript (copies the top-level list, like upstream)."""
        self._state.messages = list(messages)

    def set_tools(self, tools: list[AgentTool]) -> None:
        self._state.tools = list(tools)

    @property
    def steering_mode(self) -> QueueMode:
        return self.steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self.steering_queue.mode = mode

    @property
    def follow_up_mode(self) -> QueueMode:
        return self.follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self.follow_up_queue.mode = mode

    @property
    def signal(self) -> AbortSignal | None:
        return self._active_run.abort_controller.signal if self._active_run else None

    @property
    def is_streaming(self) -> bool:
        return self._state.is_streaming

    # -- listeners ---------------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener; returns an unsubscribe callable.

        Listeners may take ``(event, signal)`` or just ``(event)``; the arity is
        resolved once here rather than guessed on every event.
        """
        arity = _listener_arity(listener)
        entry = (listener, arity)
        self._listeners.append(entry)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(entry)
            except ValueError:
                pass

        return _unsubscribe

    # -- queueing ----------------------------------------------------------------------

    def steer(self, message: AgentMessage) -> None:
        self.steering_queue.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        self.follow_up_queue.enqueue(message)

    def clear_steering_queue(self) -> None:
        self.steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self.follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self.steering_queue.has_items() or self.follow_up_queue.has_items()

    # -- lifecycle ---------------------------------------------------------------------

    def abort(self) -> None:
        if self._active_run is not None:
            self._active_run.abort_controller.abort()

    async def wait_for_idle(self) -> None:
        if self._active_run is None:
            return
        await asyncio.shield(self._active_run.future)

    def reset(self) -> None:
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before resetting.")
        self._state.messages = []
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        self._state.error_message = None
        self.clear_all_queues()

    # -- prompting ---------------------------------------------------------------------

    async def prompt(
        self,
        input: Union[str, AgentMessage, list[AgentMessage]],
        images: list[ImageContent] | None = None,
    ) -> None:
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. Use steer() or follow_up() to queue "
                "messages, or wait for completion."
            )
        messages = self._normalize_prompt_input(input, images)
        await self._run_prompt_messages(messages)

    async def continue_(self) -> None:
        """Continue from the current transcript (alias: :meth:`continue_run`)."""
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")

        last = self._state.messages[-1] if self._state.messages else None
        if last is None:
            raise RuntimeError("No messages to continue from")

        if getattr(last, "role", None) == "assistant":
            queued_steering = self.steering_queue.drain()
            if queued_steering:
                await self._run_prompt_messages(queued_steering, skip_initial_steering_poll=True)
                return
            queued_follow_ups = self.follow_up_queue.drain()
            if queued_follow_ups:
                await self._run_prompt_messages(queued_follow_ups)
                return
            raise RuntimeError("Cannot continue from message role: assistant")

        await self._run_continuation()

    # ``continue`` is a keyword, so expose it under a Pythonic name too.
    continue_run = continue_

    def _normalize_prompt_input(
        self, input: Union[str, AgentMessage, list[AgentMessage]], images: list[ImageContent] | None
    ) -> list[AgentMessage]:
        if isinstance(input, list):
            return input
        if isinstance(input, str):
            content: list[Any] = [TextContent(text=input)]
            if images:
                content.extend(images)
            return [
                _make_user_message(content)
            ]
        return [input]

    async def _run_prompt_messages(
        self, messages: list[AgentMessage], *, skip_initial_steering_poll: bool = False
    ) -> None:
        async def _executor(signal: AbortSignal) -> None:
            await run_agent_loop(
                messages,
                self._create_context_snapshot(),
                self._create_loop_config(skip_initial_steering_poll),
                self._process_events,
                signal,
                self.stream_fn,
            )

        await self._run_with_lifecycle(_executor)

    async def _run_continuation(self) -> None:
        async def _executor(signal: AbortSignal) -> None:
            await run_agent_loop_continue(
                self._create_context_snapshot(),
                self._create_loop_config(),
                self._process_events,
                signal,
                self.stream_fn,
            )

        await self._run_with_lifecycle(_executor)

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            system_prompt=self._state.system_prompt,
            messages=list(self._state.messages),
            tools=list(self._state.tools),
        )

    def _create_loop_config(self, skip_initial_steering_poll: bool = False) -> AgentLoopConfig:
        skip = [skip_initial_steering_poll]
        should_stop = self.should_stop_after_turn

        async def _should_stop(context: PrepareNextTurnContext) -> bool:
            assert should_stop is not None
            return await should_stop(context)

        async def _get_steering() -> list[AgentMessage]:
            if skip[0]:
                skip[0] = False
                return []
            return self.steering_queue.drain()

        return AgentLoopConfig(
            model=self._state.model,
            reasoning=None if self._state.thinking_level == "off" else self._state.thinking_level,
            session_id=self.session_id,
            tool_execution=self.tool_execution,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            should_stop_after_turn=_should_stop if should_stop is not None else None,
            prepare_next_turn=self.prepare_next_turn,
            convert_to_llm=self.convert_to_llm,
            transform_context=self.transform_context,
            get_api_key=self.get_api_key,
            api_key=self.api_key,
            stream_options=self.stream_options,
            get_steering_messages=_get_steering,
            get_follow_up_messages=lambda: _resolved(self.follow_up_queue.drain()),
        )

    async def _run_with_lifecycle(self, executor: Callable[[AbortSignal], Awaitable[None]]) -> None:
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing.")

        controller = AbortController()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._active_run = _ActiveRun(abort_controller=controller, future=future)

        self._state.is_streaming = True
        self._state.streaming_message = None
        self._state.error_message = None

        try:
            await executor(controller.signal)
        except BaseException as error:  # noqa: BLE001 - surfaced as synthetic events
            await self._handle_run_failure(error, controller.signal.aborted)
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: BaseException, aborted: bool) -> None:
        model = self._state.model
        failure = AssistantMessage(
            content=[TextContent(text="")],
            api=model.api if model else "",
            provider=model.provider if model else "",
            model=model.id if model else "",
            usage=_EMPTY_USAGE,
            stop_reason="aborted" if aborted else "error",
            error_message=str(error),
            timestamp=int(time.time() * 1000),
        )
        await self._process_events(_message_start(failure))
        await self._process_events(_message_end(failure))
        await self._process_events(_turn_end(failure))
        await self._process_events(_agent_end([failure]))

    def _finish_run(self) -> None:
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        if self._active_run is not None and not self._active_run.future.done():
            self._active_run.future.set_result(None)
        self._active_run = None

    async def _process_events(self, event: AgentEvent) -> None:
        event_type = event.type

        if event_type == "message_start" or event_type == "message_update":
            self._state.streaming_message = event.message
        elif event_type == "message_end":
            self._state.streaming_message = None
            self._state.messages.append(event.message)
        elif event_type == "tool_execution_start":
            pending = set(self._state.pending_tool_calls)
            pending.add(event.tool_call_id)
            self._state.pending_tool_calls = pending
        elif event_type == "tool_execution_end":
            pending = set(self._state.pending_tool_calls)
            pending.discard(event.tool_call_id)
            self._state.pending_tool_calls = pending
        elif event_type == "turn_end":
            message = event.message
            if getattr(message, "role", None) == "assistant" and getattr(message, "error_message", None):
                self._state.error_message = message.error_message
        elif event_type == "agent_end":
            self._state.streaming_message = None

        signal = self._active_run.abort_controller.signal if self._active_run else None
        if signal is None:
            raise RuntimeError("Agent listener invoked outside active run")

        for listener, arity in list(self._listeners):
            result = listener(event, signal) if arity >= 2 else listener(event)
            if inspect.isawaitable(result):
                await result


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _listener_arity(listener: Listener) -> int:
    try:
        signature = inspect.signature(listener)
    except (TypeError, ValueError):
        return 1
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return 2
    return positional


async def _resolved(value: list[AgentMessage]) -> list[AgentMessage]:
    return value


def _make_user_message(content: list[Any]) -> AgentMessage:
    from pi_ai import UserMessage

    return UserMessage(content=content, timestamp=int(time.time() * 1000))


def _message_start(message: AgentMessage) -> AgentEvent:
    from .types import MessageStartEvent

    return MessageStartEvent(message=message)


def _message_end(message: AgentMessage) -> AgentEvent:
    from .types import MessageEndEvent

    return MessageEndEvent(message=message)


def _turn_end(message: AgentMessage) -> AgentEvent:
    from .types import TurnEndEvent

    return TurnEndEvent(message=message, tool_results=[])


def _agent_end(messages: list[AgentMessage]) -> AgentEvent:
    from .types import AgentEndEvent

    return AgentEndEvent(messages=messages)


__all__ = ["Agent", "AgentState", "PendingMessageQueue", "QueueMode"]
