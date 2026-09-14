"""The agent loop — a faithful port of ``packages/agent/src/agent-loop.ts``.

The control flow is deliberately reproduced rather than "cleaned up", because the
observable event sequence is part of the contract:

* The **outer** loop re-enters when follow-up messages arrive after the agent
  would otherwise stop.
* The **inner** loop runs while there are tool calls to process *or* pending
  steering messages to inject.
* Steering is drained at exactly three points: before the first turn, after
  ``prepare_next_turn`` (only when nothing is pending, so one-at-a-time mode
  cannot deliver two messages in a turn), and after ``turn_end``.
* ``should_stop_after_turn`` runs after ``turn_end`` and before **any** queue
  poll, so returning ``True`` skips follow-ups entirely.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
from dataclasses import replace
from typing import Any, Callable

from pi_ai import (
    AbortSignal,
    AssistantMessage,
    Context,
    EventStream,
    StreamOptions,
    TextContent,
    Tool,
    ToolResultMessage,
    validate_tool_arguments,
)

from .stream_fn import get_default_stream_fn
from .types import (
    AfterToolCallResult,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentTool,
    AgentToolResult,
    BeforeToolCallResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PrepareNextTurnContext,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)

#: Assistant-stream events that mutate the live partial message.
_UPDATE_TYPES = frozenset(
    {
        "text_start",
        "text_delta",
        "text_end",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
    }
)


# --------------------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------------------


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """Start a loop with new prompt messages; returns the event stream."""
    stream: EventStream[AgentEvent, list[AgentMessage]] = _create_agent_stream()

    async def _run() -> list[AgentMessage]:
        return await run_agent_loop(prompts, context, config, stream.push, signal, stream_fn)

    _drive(_run(), stream)
    return stream


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    stream_fn: StreamFn | None = None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """Continue an existing context without adding a new message (retries)."""
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    stream: EventStream[AgentEvent, list[AgentMessage]] = _create_agent_stream()

    async def _run() -> list[AgentMessage]:
        return await run_agent_loop_continue(context, config, stream.push, signal, stream_fn)

    _drive(_run(), stream)
    return stream


def _drive(coro, stream: EventStream) -> None:
    """Run the loop as a task and surface failures into the stream.

    Upstream leaks a stream that never ends when the loop throws; Python gets
    explicit failure propagation instead.
    """

    async def _wrapper() -> None:
        try:
            result = await coro
            stream.end(result)
        except BaseException as error:  # noqa: BLE001 - forwarded to consumers
            stream.fail(error)

    task = asyncio.ensure_future(_wrapper())
    # Keep a strong reference: a bare task can be garbage collected mid-flight.
    _PENDING_TASKS.add(task)
    task.add_done_callback(_PENDING_TASKS.discard)


_PENDING_TASKS: set[asyncio.Task] = set()


def _create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream(
        lambda event: event.type == "agent_end",
        lambda event: event.messages if event.type == "agent_end" else [],
    )


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn | None,
) -> list[AgentMessage]:
    new_messages: list[AgentMessage] = list(prompts)
    current_context = AgentContext(
        messages=[*context.messages, *prompts],
        system_prompt=context.system_prompt,
        tools=context.tools,
    )

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())
    for prompt in prompts:
        await _emit(emit, MessageStartEvent(message=prompt))
        await _emit(emit, MessageEndEvent(message=prompt))

    await _run_loop(
        current_context, new_messages, config, signal, emit, stream_fn or get_default_stream_fn()
    )
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: AbortSignal | None,
    stream_fn: StreamFn | None,
) -> list[AgentMessage]:
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    new_messages: list[AgentMessage] = []
    # Upstream spreads the context, so the caller's message list is shared and
    # the loop appends into it.
    current_context = AgentContext(
        messages=context.messages,
        system_prompt=context.system_prompt,
        tools=context.tools,
    )

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())

    await _run_loop(
        current_context, new_messages, config, signal, emit, stream_fn or get_default_stream_fn()
    )
    return new_messages


# --------------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------------


class _Batch:
    __slots__ = ("messages", "terminate")

    def __init__(self, messages: list[ToolResultMessage], terminate: bool) -> None:
        self.messages = messages
        self.terminate = terminate


class _Finalized:
    __slots__ = ("tool_call", "result", "is_error")

    def __init__(self, tool_call: Any, result: AgentToolResult, is_error: bool) -> None:
        self.tool_call = tool_call
        self.result = result
        self.is_error = is_error


class _Prepared:
    __slots__ = ("tool_call", "tool", "args")

    def __init__(self, tool_call: Any, tool: AgentTool, args: Any) -> None:
        self.tool_call = tool_call
        self.tool = tool
        self.args = args


class _Immediate:
    __slots__ = ("result", "is_error")

    def __init__(self, result: AgentToolResult, is_error: bool) -> None:
        self.result = result
        self.is_error = is_error


async def _run_loop(
    initial_context: AgentContext,
    new_messages: list[AgentMessage],
    initial_config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
    stream_function: StreamFn,
) -> None:
    current_context = initial_context
    config = initial_config
    last_completed_turn: PrepareNextTurnContext | None = None
    pending: list[AgentMessage] = (
        list(await _maybe_await(config.get_steering_messages()) or [])
        if config.get_steering_messages
        else []
    )

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending:
            if last_completed_turn is not None:
                if config.prepare_next_turn is not None:
                    update = await _maybe_await(config.prepare_next_turn(last_completed_turn))
                    if update is not None:
                        if update.context is not None:
                            current_context = update.context
                        new_config = replace(config)
                        if update.model is not None:
                            new_config.model = update.model
                        if update.thinking_level is not None:
                            new_config.reasoning = (
                                None if update.thinking_level == "off" else update.thinking_level
                            )
                        config = new_config
                # Preparation can be long-running (compaction). Pick up steering
                # queued meanwhile, but only when the earlier poll found nothing,
                # or one-at-a-time mode would deliver two messages this turn.
                if not pending and config.get_steering_messages is not None:
                    pending = list(await _maybe_await(config.get_steering_messages()) or [])
                await _emit(emit, TurnStartEvent())

            if pending:
                for message in pending:
                    await _emit(emit, MessageStartEvent(message=message))
                    await _emit(emit, MessageEndEvent(message=message))
                    current_context.messages.append(message)
                    new_messages.append(message)
                pending = []

            message = await _stream_assistant_response(
                current_context, config, signal, emit, stream_function
            )
            new_messages.append(message)

            if message.stop_reason in ("error", "aborted"):
                await _emit(emit, TurnEndEvent(message=message, tool_results=[]))
                await _emit(emit, AgentEndEvent(messages=new_messages))
                return

            tool_calls = [c for c in message.content if getattr(c, "type", None) == "toolCall"]

            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False
            if tool_calls:
                # A "length" stop truncated the message, so every tool call in it
                # may carry silently incomplete arguments. Fail the whole batch
                # instead of executing borked calls.
                if message.stop_reason == "length":
                    batch = await _fail_tool_calls_from_truncated_message(tool_calls, emit)
                else:
                    batch = await _execute_tool_calls(
                        current_context, message, config, signal, emit
                    )
                tool_results.extend(batch.messages)
                has_more_tool_calls = not batch.terminate

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            await _emit(emit, TurnEndEvent(message=message, tool_results=tool_results))

            last_completed_turn = PrepareNextTurnContext(
                message=message,
                tool_results=tool_results,
                context=current_context,
                new_messages=new_messages,
            )

            if config.should_stop_after_turn is not None and await _maybe_await(
                config.should_stop_after_turn(last_completed_turn)
            ):
                await _emit(emit, AgentEndEvent(messages=new_messages))
                return

            if config.get_steering_messages is not None:
                pending = list(await _maybe_await(config.get_steering_messages()) or [])

        follow_up = (
            list(await _maybe_await(config.get_follow_up_messages()) or [])
            if config.get_follow_up_messages
            else []
        )
        if follow_up:
            pending = follow_up
            continue

        break

    await _emit(emit, AgentEndEvent(messages=new_messages))


# --------------------------------------------------------------------------------------
# Streaming one assistant response
# --------------------------------------------------------------------------------------


def _to_llm_tools(tools: list[AgentTool]) -> list[Tool]:
    return [Tool(name=t.name, description=t.description, parameters=t.parameters) for t in tools]


def _build_stream_options(
    config: AgentLoopConfig, api_key: str | None, signal: AbortSignal | None
) -> StreamOptions:
    base = config.stream_options
    return StreamOptions(
        api_key=api_key,
        signal=signal,
        reasoning=config.reasoning,
        session_id=config.session_id,
        headers=base.headers if base else None,
        temperature=base.temperature if base else None,
        max_tokens=base.max_tokens if base else None,
        top_p=base.top_p if base else None,
        thinking_budgets=base.thinking_budgets if base else None,
        provider_options=dict(base.provider_options) if base else {},
        request_timeout=base.request_timeout if base else None,
        extra=dict(base.extra) if base else {},
    )


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
    stream_function: StreamFn,
) -> AssistantMessage:
    messages: list[AgentMessage] = context.messages
    if config.transform_context is not None:
        messages = await _maybe_await(config.transform_context(messages, signal))

    llm_messages = await _maybe_await(config.convert_to_llm(messages))
    llm_context = Context(
        messages=list(llm_messages),
        system_prompt=context.system_prompt,
        tools=_to_llm_tools(context.tools),
    )

    # Resolve the API key per request so expiring OAuth tokens stay fresh.
    resolved_api_key = config.api_key
    if config.get_api_key is not None:
        resolved_api_key = (
            await _maybe_await(config.get_api_key(config.model.provider)) or resolved_api_key
        )

    response = stream_function(
        config.model, llm_context, _build_stream_options(config, resolved_api_key, signal)
    )

    partial: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        event_type = event.type
        if event_type == "start":
            partial = event.partial
            context.messages.append(partial)
            added_partial = True
            await _emit(emit, MessageStartEvent(message=copy.copy(partial)))
        elif event_type in _UPDATE_TYPES:
            if partial is not None:
                partial = event.partial
                context.messages[-1] = partial
                await _emit(
                    emit,
                    MessageUpdateEvent(
                        message=copy.copy(partial), assistant_message_event=event
                    ),
                )
        elif event_type in ("done", "error"):
            final = await response.result()
            if added_partial:
                context.messages[-1] = final
            else:
                context.messages.append(final)
                await _emit(emit, MessageStartEvent(message=copy.copy(final)))
            await _emit(emit, MessageEndEvent(message=final))
            return final

    final = await response.result()
    if added_partial:
        context.messages[-1] = final
    else:
        context.messages.append(final)
        await _emit(emit, MessageStartEvent(message=copy.copy(final)))
    await _emit(emit, MessageEndEvent(message=final))
    return final


# --------------------------------------------------------------------------------------
# Tool execution
# --------------------------------------------------------------------------------------


async def _fail_tool_calls_from_truncated_message(
    tool_calls: list[Any], emit: AgentEventSink
) -> _Batch:
    messages: list[ToolResultMessage] = []
    for tool_call in tool_calls:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id, tool_name=tool_call.name, args=tool_call.arguments
            ),
        )
        finalized = _Finalized(
            tool_call,
            _error_result(
                f'Tool call "{tool_call.name}" was not executed: the response hit the output '
                "token limit, so its arguments may be truncated. Re-issue the tool call with "
                "complete arguments."
            ),
            True,
        )
        await _emit_tool_execution_end(finalized, emit)
        message = _tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)
    return _Batch(messages, terminate=False)


async def _execute_tool_calls(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _Batch:
    tool_calls = [c for c in assistant_message.content if getattr(c, "type", None) == "toolCall"]
    has_sequential = any(
        getattr(_find_tool(current_context.tools, tc.name), "execution_mode", "parallel") == "sequential"
        for tc in tool_calls
    )
    if config.tool_execution == "sequential" or has_sequential:
        return await _execute_sequential(
            current_context, assistant_message, tool_calls, config, signal, emit
        )
    return await _execute_parallel(
        current_context, assistant_message, tool_calls, config, signal, emit
    )


def _find_tool(tools: list[AgentTool], name: str) -> AgentTool | None:
    for tool in tools:
        if tool.name == name:
            return tool
    return None


async def _execute_sequential(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[Any],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _Batch:
    finalized_calls: list[_Finalized] = []
    messages: list[ToolResultMessage] = []

    for tool_call in tool_calls:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id, tool_name=tool_call.name, args=tool_call.arguments
            ),
        )

        preparation = await _prepare_tool_call(
            current_context, assistant_message, tool_call, config, signal
        )
        if isinstance(preparation, _Immediate):
            finalized = _Finalized(tool_call, preparation.result, preparation.is_error)
        else:
            executed = await _execute_prepared_tool_call(preparation, signal, emit)
            finalized = await _finalize_executed_tool_call(
                current_context, assistant_message, preparation, executed, config, signal
            )

        await _emit_tool_execution_end(finalized, emit)
        message = _tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        finalized_calls.append(finalized)
        messages.append(message)

        if signal is not None and signal.aborted:
            break

    return _Batch(messages, terminate=_should_terminate(finalized_calls))


async def _execute_parallel(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[Any],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> _Batch:
    entries: list[Any] = []

    for tool_call in tool_calls:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                tool_call_id=tool_call.id, tool_name=tool_call.name, args=tool_call.arguments
            ),
        )

        preparation = await _prepare_tool_call(
            current_context, assistant_message, tool_call, config, signal
        )
        if isinstance(preparation, _Immediate):
            finalized = _Finalized(tool_call, preparation.result, preparation.is_error)
            await _emit_tool_execution_end(finalized, emit)
            entries.append(finalized)
            if signal is not None and signal.aborted:
                break
            continue

        entries.append(_make_thunk(current_context, assistant_message, preparation, config, signal, emit))
        if signal is not None and signal.aborted:
            break

    # Preflight is strictly sequential (above); execution starts for every
    # allowed call in one tick, and results keep source order.
    ordered = await asyncio.gather(
        *[entry() if callable(entry) else _resolved(entry) for entry in entries]
    )

    messages: list[ToolResultMessage] = []
    for finalized in ordered:
        message = _tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)

    return _Batch(messages, terminate=_should_terminate(ordered))


async def _resolved(value: Any) -> Any:
    return value


def _make_thunk(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    preparation: _Prepared,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> Callable[[], Any]:
    async def _thunk() -> _Finalized:
        if signal is not None and signal.aborted:
            finalized = _Finalized(
                preparation.tool_call, _error_result("Operation aborted"), True
            )
            await _emit_tool_execution_end(finalized, emit)
            return finalized
        executed = await _execute_prepared_tool_call(preparation, signal, emit)
        finalized = await _finalize_executed_tool_call(
            current_context, assistant_message, preparation, executed, config, signal
        )
        await _emit_tool_execution_end(finalized, emit)
        return finalized

    return _thunk


def _should_terminate(finalized_calls: list[_Finalized]) -> bool:
    return bool(finalized_calls) and all(
        finalized.result.terminate is True for finalized in finalized_calls
    )


async def _prepare_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: Any,
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> _Prepared | _Immediate:
    tool = _find_tool(current_context.tools, tool_call.name)
    if tool is None:
        return _Immediate(_error_result(f"Tool {tool_call.name} not found"), True)

    try:
        prepared_call = tool_call
        if tool.prepare_arguments is not None:
            prepared_args = tool.prepare_arguments(tool_call.arguments)
            if prepared_args is not tool_call.arguments:
                prepared_call = copy.copy(tool_call)
                prepared_call.arguments = prepared_args

        validated_args = validate_tool_arguments(tool, prepared_call.arguments)

        if config.before_tool_call is not None:
            before = await _maybe_await(
                config.before_tool_call(
                    _before_context(assistant_message, tool_call, validated_args, current_context),
                    signal,
                )
            )
            if signal is not None and signal.aborted:
                return _Immediate(_error_result("Operation aborted"), True)
            if isinstance(before, BeforeToolCallResult) and before.block:
                result = _error_result(before.reason or "Tool execution was blocked")
                if before.terminate is True:
                    result.terminate = True
                return _Immediate(result, True)

        if signal is not None and signal.aborted:
            return _Immediate(_error_result("Operation aborted"), True)

        return _Prepared(tool_call, tool, validated_args)
    except Exception as error:  # noqa: BLE001 - validation failures become tool results
        return _Immediate(_error_result(str(error)), True)


def _before_context(assistant_message, tool_call, args, context):
    from .types import BeforeToolCallContext

    return BeforeToolCallContext(
        assistant_message=assistant_message, tool_call=tool_call, args=args, context=context
    )


async def _execute_prepared_tool_call(
    prepared: _Prepared,
    signal: AbortSignal | None,
    emit: AgentEventSink,
) -> tuple[AgentToolResult, bool]:
    update_tasks: list[asyncio.Future] = []
    accepting = [True]

    def on_update(partial_result: AgentToolResult) -> None:
        if not accepting[0]:
            return
        update_tasks.append(
            asyncio.ensure_future(
                _emit(
                    emit,
                    ToolExecutionUpdateEvent(
                        tool_call_id=prepared.tool_call.id,
                        tool_name=prepared.tool_call.name,
                        args=prepared.tool_call.arguments,
                        partial_result=partial_result,
                    ),
                )
            )
        )

    try:
        assert prepared.tool.execute is not None, f"Tool {prepared.tool.name} has no execute function"
        result = await prepared.tool.execute(
            prepared.tool_call.id, prepared.args, signal, on_update
        )
        accepting[0] = False
        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)
        return result, False
    except Exception as error:  # noqa: BLE001 - tool failures become tool results
        accepting[0] = False
        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)
        return _error_result(str(error)), True
    finally:
        accepting[0] = False


async def _finalize_executed_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    prepared: _Prepared,
    executed: tuple[AgentToolResult, bool],
    config: AgentLoopConfig,
    signal: AbortSignal | None,
) -> _Finalized:
    from .types import AfterToolCallContext

    result, is_error = executed

    if config.after_tool_call is not None:
        try:
            after = await _maybe_await(
                config.after_tool_call(
                    AfterToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=prepared.tool_call,
                        args=prepared.args,
                        result=result,
                        is_error=is_error,
                        context=current_context,
                    ),
                    signal,
                )
            )
            if isinstance(after, AfterToolCallResult):
                if after.content is not None:
                    result.content = after.content
                if after.details is not None:
                    result.details = after.details
                if after.usage is not None:
                    result.usage = after.usage
                if after.terminate is not None:
                    result.terminate = after.terminate
                if after.is_error is not None:
                    is_error = after.is_error
        except Exception as error:  # noqa: BLE001 - upstream discards the result here
            result = _error_result(str(error))
            is_error = True

    return _Finalized(prepared.tool_call, result, is_error)


def _error_result(message: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=message)], details={})


def _tool_result_message(finalized: _Finalized) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=finalized.tool_call.id,
        tool_name=finalized.tool_call.name,
        content=list(finalized.result.content or []),
        details=finalized.result.details,
        usage=finalized.result.usage,
        added_tool_names=(
            list(finalized.result.added_tool_names) if finalized.result.added_tool_names else None
        ),
        is_error=finalized.is_error,
        timestamp=int(time.time() * 1000),
    )


async def _emit_tool_execution_end(finalized: _Finalized, emit: AgentEventSink) -> None:
    await _emit(
        emit,
        ToolExecutionEndEvent(
            tool_call_id=finalized.tool_call.id,
            tool_name=finalized.tool_call.name,
            result=finalized.result,
            is_error=finalized.is_error,
        ),
    )


async def _emit_tool_result_message(message: ToolResultMessage, emit: AgentEventSink) -> None:
    await _emit(emit, MessageStartEvent(message=message))
    await _emit(emit, MessageEndEvent(message=message))


# --------------------------------------------------------------------------------------
# Emission helper
# --------------------------------------------------------------------------------------


async def _emit(sink: AgentEventSink, event: AgentEvent) -> None:
    """Deliver an event, awaiting the listener when it is async.

    Listeners must be awaited rather than fired-and-forgotten so that a slow
    consumer cannot observe events out of order.
    """
    result = sink(event)
    if inspect.isawaitable(result):
        await result


async def _maybe_await(value):
    """Accept both a plain value and an awaitable.

    Every upstream hook is typed ``T | Promise<T>``; requiring async callables in
    Python would make simple converters and predicates needlessly awkward.
    """
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["agent_loop", "agent_loop_continue", "run_agent_loop", "run_agent_loop_continue"]
