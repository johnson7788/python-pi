"""The session orchestration layer.

Port of ``packages/coding-agent/src/core/agent-session.ts`` upstream (3552
lines), reduced to the subset the v0.1 core library needs.  An
:class:`AgentSession` wires a :class:`~pi_agent.Agent` to the coding agent's
session store, resources, settings and modes:

* it builds the active tool set and the system prompt once at construction;
* it persists every finalized message through the :class:`SessionManager` -- the
  **only** place conversation history is written;
* it runs auto-compaction before the next LLM call (the agent's
  ``prepare_next_turn`` hook) and after a completed run;
* it retries transient provider errors with exponential backoff;
* it expands ``/skill:<name>`` commands and ``/name`` prompt templates before
  sending;
* it forwards agent events to subscribers so print/json modes can render them.

Deliberately deferred from upstream (documented, not implemented in v0.1)
-----------------------------------------------------------------------
* the extension system (``ExtensionRunner``): extension commands, every
  ``session_*`` / ``input`` / ``before_agent_start`` / tool hooks, and the
  ``entry_appended`` / ``queue_update`` session events;
* the pre-prompt compaction check (``_checkCompaction(lastAssistant, false)``);
  compaction is only checked through ``prepare_next_turn`` and after a run;
* overflow recovery (``isContextOverflow`` / ``isRecoverableLength``) and the
  split-turn compaction path -- only threshold compaction is implemented;
* branch navigation / branch summaries, bash execution (``!`` commands),
  ``sendCustomMessage``, fork, export, model cycling and scoped models.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping, Sequence

from pi_ai import (
    AbortController,
    AbortSignal,
    AssistantMessage,
    AuthenticationError,
    ImageContent,
    Model,
    TextContent,
    UserMessage,
    clamp_thinking_level,
)
from pi_agent import (
    Agent,
    AgentContext,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentTool,
    PrepareNextTurnContext,
    StreamFn,
)

from .compaction import (
    DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    DEFAULT_COMPACTION_RESERVE_TOKENS,
    DEFAULT_COMPACTION_SETTINGS,
    CompactionResult,
    CompactionSettings,
    calculate_context_tokens,
    compact_session,
    estimate_context_tokens,
    should_compact,
)
from .messages import convert_to_llm
from .resource_loader import PromptTemplate, ResourceLoader
from .session_manager import SessionManager, get_latest_compaction_entry
from .settings import SettingsManager
from .skills import expand_skill_command
from .system_prompt import build_system_prompt, get_system_prompt_options
from .tools import DEFAULT_ACTIVE_TOOL_NAMES, create_builtin_tools

#: Upstream default thinking level; clamped to the model's supported set.
DEFAULT_THINKING_LEVEL = "medium"

#: How a prompt submitted while the agent is streaming should be delivered.
StreamingBehavior = Literal["steer", "followUp"]

#: Outcome of a compaction check.
_CompactionOutcome = Literal["none", "compacted", "retry"]

Listener = Callable[[Any], Any]

# -- retry settings defaults (upstream ``settings.retry``) ------------------------------

_DEFAULT_RETRY_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "maxAgentDelayMs": 60000,
}

#: Deterministic quota/billing failures must not be retried (upstream classifier).
_NON_RETRYABLE_ERROR_RE = re.compile(
    r"GoUsageLimitError|FreeUsageLimitError|Monthly usage limit reached|available balance"
    r"|insufficient_quota|out of budget|quota exceeded|billing",
    re.IGNORECASE,
)

#: Transient provider/transport failures that are worth retrying (upstream
#: allow-list, reduced to the patterns that matter for the v0.1 providers).
_RETRYABLE_ERROR_RE = re.compile(
    r"overloaded|rate.?limit|too many requests|429|500|502|503|504|524"
    r"|service.?unavailable|server.?error|internal.?error|provider.?returned.?error"
    r"|exceeded request buffer limit while retrying upstream"
    r"|network.?error|connection.?error|connection.?refused|connection.?lost"
    r"|other side closed|fetch failed|getaddrinfo|ENOTFOUND|EAI_AGAIN"
    r"|upstream.?connect|reset before headers|socket hang up|socket connection was closed"
    r"|timed? out|timeout|terminated"
    r"|websocket.?closed|websocket.?error"
    r"|ended without|stream ended before message_stop"
    r"|stream ended before a terminal response event|http2 request did not get a response"
    r"|retry delay|you can retry your request|try your request again|please retry your request"
    r"|ResourceExhausted",
    re.IGNORECASE,
)

# -- prompt template expansion (port of core/prompt-templates.ts) -----------------------

_TEMPLATE_PATTERN = re.compile(
    r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)
_TEMPLATE_NAME_PATTERN = re.compile(r"^/([^\s]+)(?:\s+([\s\S]*))?$")


def parse_command_args(args_string: str) -> list[str]:
    """Split template arguments respecting single/double quoted strings."""
    args: list[str] = []
    current = ""
    quote: str | None = None
    for char in args_string:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in ("\"", "'"):
            quote = char
        elif char.isspace():
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


def substitute_args(content: str, args: Sequence[str]) -> str:
    """Substitute ``$1`` / ``$@`` / ``$ARGUMENTS`` / ``${N:-default}`` / ``${@:N:L}``.

    Replacement happens on the template string only; argument and default values
    are never recursively substituted (matching upstream).
    """
    all_args = " ".join(args)

    def _replace(match: re.Match[str]) -> str:
        default_target, default_value, slice_start, slice_length, simple = match.groups()
        if default_target:
            if default_target in ("@", "ARGUMENTS"):
                value: str | None = all_args
            else:
                index = int(default_target) - 1
                value = args[index] if 0 <= index < len(args) else None
            return value if value else default_value
        if slice_start:
            start = max(0, int(slice_start) - 1)
            if slice_length:
                return " ".join(args[start : start + int(slice_length)])
            return " ".join(args[start:])
        if simple in ("ARGUMENTS", "@"):
            return all_args
        index = int(simple) - 1
        return args[index] if 0 <= index < len(args) else ""

    return _TEMPLATE_PATTERN.sub(_replace, content)


def expand_prompt_template(text: str, templates: Sequence[PromptTemplate]) -> str:
    """Expand ``/name args`` against the discovered prompt templates."""
    if not text.startswith("/"):
        return text
    match = _TEMPLATE_NAME_PATTERN.match(text)
    if match is None:
        return text
    name = match.group(1)
    args_string = match.group(2) or ""
    for template in templates:
        if template.name == name:
            return substitute_args(template.content, parse_command_args(args_string))
    return text


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _content_text(content: Any) -> str:
    """Text of a ``string | TextContent[]`` content field."""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(block.text for block in content if isinstance(block, TextContent))


def _user_message(text: str, images: Sequence[ImageContent] | None = None) -> UserMessage:
    content: list[Any] = [TextContent(text=text)]
    if images:
        content.extend(images)
    return UserMessage(content=content, timestamp=int(time.time() * 1000))


def _iso_to_millis(value: str) -> int:
    """``new Date(iso).getTime()`` for session-entry timestamps."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _retry_delay_ms(settings: Mapping[str, Any], attempt: int) -> int:
    """Exponential backoff capped by ``maxAgentDelayMs`` (upstream formula)."""
    base = int(settings.get("baseDelayMs") or 0)
    cap = int(settings.get("maxAgentDelayMs") or _DEFAULT_RETRY_SETTINGS["maxAgentDelayMs"])
    return min(base * (2 ** max(0, attempt - 1)), cap)


def _validate_queue_mode(mode: str) -> str:
    """Reject an unknown queue mode instead of silently accepting it."""
    if mode not in ("all", "one-at-a-time"):
        raise ValueError(f"Invalid queue mode: {mode!r} (expected 'all' or 'one-at-a-time')")
    return mode


def _arity(fn: Callable[..., Any]) -> int:
    """Number of positional parameters a callable accepts."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return 1
    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            count += 1
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return 9
    return count


def _custom_message(custom_type: str, content: Any, display: bool, details: Any) -> Any:
    from .messages import CustomMessage

    return CustomMessage(
        custom_type=custom_type,
        content=content,
        display=bool(display),
        details=details,
        timestamp=int(time.time() * 1000),
    )


def _custom_message_from_spec(spec: Any) -> Any:
    """Build a CustomMessage from a ``before_agent_start`` injection.

    Accepts both a :class:`~.extensions.types.CustomMessageSpec` and a plain dict,
    because an extension written against pi's JSON shape will hand back the latter.
    """
    from .messages import CustomMessage

    if isinstance(spec, Mapping):
        custom_type = spec.get("custom_type") or spec.get("customType") or "extension"
        content = spec.get("content", "")
        display = spec.get("display", True)
        details = spec.get("details")
    else:
        custom_type = (
            getattr(spec, "custom_type", None) or getattr(spec, "customType", None) or "extension"
        )
        content = getattr(spec, "content", "")
        display = getattr(spec, "display", True)
        details = getattr(spec, "details", None)

    return CustomMessage(
        custom_type=str(custom_type),
        content=content,
        display=bool(display),
        details=details,
        timestamp=int(time.time() * 1000),
    )


def _replace_message_in_place(target: Any, replacement: Any) -> None:
    """Copy ``replacement``'s fields onto ``target``, keeping the object identity.

    The agent loop stores the finalized message before emitting ``message_end``, and
    persistence reads the same object afterwards, so mutating in place is what keeps
    agent state, downstream events and the session file in agreement.
    """
    import dataclasses

    if target is replacement or type(target) is not type(replacement):
        return

    if dataclasses.is_dataclass(target):
        for field_info in dataclasses.fields(target):
            setattr(target, field_info.name, getattr(replacement, field_info.name))
        return

    if hasattr(target, "__dict__"):
        vars(target).clear()
        vars(target).update(vars(replacement))


def _with_message(event: Any, message: Any) -> Any:
    """Return a copy of ``event`` carrying a replacement message."""
    import dataclasses

    if dataclasses.is_dataclass(event):
        return dataclasses.replace(event, message=message)
    return event


def _to_extension_event(event: Any) -> Any:
    """Map an agent-loop event onto the extension event of the same name.

    Tool *call*/*result* and session lifecycle events are deliberately absent:
    those are driven from their own hooks, not from the loop's event stream.
    """
    from .extensions import types as ext

    kind = getattr(event, "type", None)

    if kind == "agent_start":
        return ext.AgentStartEvent()
    if kind == "agent_end":
        return ext.AgentEndEvent(messages=list(getattr(event, "messages", None) or []))
    if kind == "turn_start":
        return ext.TurnStartEvent()
    if kind == "turn_end":
        return ext.TurnEndEvent(
            message=getattr(event, "message", None),
            tool_results=list(getattr(event, "tool_results", None) or []),
        )
    if kind == "message_start":
        return ext.MessageStartEvent(message=getattr(event, "message", None))
    if kind == "message_update":
        return ext.MessageUpdateEvent(
            message=getattr(event, "message", None),
            assistant_message_event=getattr(event, "assistant_message_event", None),
        )
    if kind == "message_end":
        return ext.MessageEndEvent(message=getattr(event, "message", None))
    if kind == "tool_execution_start":
        return ext.ToolExecutionStartEvent(
            tool_call_id=getattr(event, "tool_call_id", ""),
            tool_name=getattr(event, "tool_name", ""),
            args=getattr(event, "args", None),
        )
    if kind == "tool_execution_update":
        return ext.ToolExecutionUpdateEvent(
            tool_call_id=getattr(event, "tool_call_id", ""),
            tool_name=getattr(event, "tool_name", ""),
            args=getattr(event, "args", None),
            partial_result=getattr(event, "partial_result", None),
        )
    if kind == "tool_execution_end":
        return ext.ToolExecutionEndEvent(
            tool_call_id=getattr(event, "tool_call_id", ""),
            tool_name=getattr(event, "tool_name", ""),
            result=getattr(event, "result", None),
            is_error=bool(getattr(event, "is_error", False)),
        )
    return None


# --------------------------------------------------------------------------------------
# AgentSession
# --------------------------------------------------------------------------------------


class AgentSession:
    """Ties the agent loop to a persisted session, resources and settings."""

    def __init__(
        self,
        model: Model,
        cwd: str,
        session_manager: SessionManager,
        settings_manager: SettingsManager | None = None,
        resource_loader: ResourceLoader | None = None,
        auth_storage: Any = None,
        stream_fn: StreamFn | None = None,
        tools: Sequence[AgentTool] | None = None,
        active_tool_names: Sequence[str] | None = None,
        thinking_level: str | None = None,
        extension_runner: Any = None,
        scrub_shell_env: bool = False,
    ) -> None:
        self._model = model
        self._cwd = cwd
        self._session_manager = session_manager
        self._settings_manager = settings_manager
        self._resource_loader = resource_loader
        self._auth_storage = auth_storage
        self._stream_fn = stream_fn
        self._extension_runner = extension_runner
        self._active_tool_names = (
            list(active_tool_names) if active_tool_names is not None else list(DEFAULT_ACTIVE_TOOL_NAMES)
        )
        self._tools: list[AgentTool] = (
            list(tools)
            if tools is not None
            else create_builtin_tools(
                cwd, self._active_tool_names, scrub_secrets=scrub_shell_env
            )
        )
        # Extension tools are merged in and enabled by default: an extension
        # registers a tool because it wants it usable.
        if extension_runner is not None:
            for tool in extension_runner.get_tools():
                if all(existing.name != tool.name for existing in self._tools):
                    self._tools.append(tool)
                if tool.name not in self._active_tool_names:
                    self._active_tool_names.append(tool.name)
        self._base_system_prompt = self._build_system_prompt()

        context = session_manager.build_context()
        if thinking_level is not None:
            initial_level = thinking_level
        elif session_manager.get_entries():
            initial_level = context.thinking_level
        else:
            initial_level = DEFAULT_THINKING_LEVEL
        self._thinking_level = clamp_thinking_level(model, initial_level)

        self._is_agent_run_active = False
        self._idle_future: asyncio.Future[None] | None = None
        self._listeners: list[Listener] = []
        self._steering_texts: list[str] = []
        self._follow_up_texts: list[str] = []
        self._last_assistant_message: AssistantMessage | None = None
        self._retry_attempt = 0
        self._retry_abort: AbortController | None = None
        self._last_compaction_error: str | None = None
        #: Runtime toggles driven by the RPC surface.  ``None`` means "follow settings".
        self._auto_compaction_override: bool | None = None
        self._auto_retry_override: bool | None = None
        self._is_compacting = False
        self._disposed = False

        self._agent = Agent(
            model,
            self._base_system_prompt,
            self._tools,
            self._convert_to_llm,
            stream_fn=stream_fn,
            prepare_next_turn=self._prepare_next_turn,
            get_api_key=self._get_api_key if auth_storage is not None else None,
            before_tool_call=self._before_tool_call if extension_runner is not None else None,
            after_tool_call=self._after_tool_call if extension_runner is not None else None,
            messages=list(context.messages),
            thinking_level=self._thinking_level,
        )
        # The single place history is observed and persisted.
        self._unsubscribe_agent = self._agent.subscribe(self._handle_agent_event)

    # -- accessors ---------------------------------------------------------------------

    @property
    def agent(self) -> Agent:
        """The underlying agent loop instance."""
        return self._agent

    @property
    def model(self) -> Model:
        return self._model

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def session_file(self) -> str | None:
        """Path of the session file, or ``None`` for an in-memory session."""
        return self._session_manager.get_session_file()

    @property
    def session_id(self) -> str:
        return self._session_manager.get_session_id()

    @property
    def messages(self) -> list[AgentMessage]:
        """The live conversation (extended message union)."""
        return self._agent.state.messages

    @property
    def thinking_level(self) -> str:
        return self._thinking_level

    @property
    def is_streaming(self) -> bool:
        """``True`` while a prompt run (including retries/compaction) is active."""
        return self._is_agent_run_active

    @property
    def is_idle(self) -> bool:
        return not self._is_agent_run_active

    @property
    def prompt_templates(self) -> list[PromptTemplate]:
        return self._resource_loader.prompt_templates() if self._resource_loader is not None else []

    @property
    def active_tool_names(self) -> list[str]:
        return [tool.name for tool in self._tools]

    def state(self) -> dict[str, Any]:
        """Small snapshot of the session, suitable for logging or mode headers."""
        return {
            "model": self._model,
            "thinking_level": self._thinking_level,
            "is_streaming": self._is_agent_run_active,
            "session_file": self._session_manager.get_session_file(),
            "session_id": self._session_manager.get_session_id(),
            "session_name": self._session_manager.get_session_name(),
            "message_count": len(self._agent.state.messages),
        }

    #: Upstream spelling.
    get_state = state

    # -- events ------------------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register an agent-event listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _unsubscribe

    async def _emit(self, event: Any) -> None:
        for listener in list(self._listeners):
            result = listener(event)
            if inspect.isawaitable(result):
                await result

    # -- prompting ---------------------------------------------------------------------

    async def prompt(
        self,
        text: str,
        *,
        images: Sequence[ImageContent] | None = None,
        streaming_behavior: StreamingBehavior | None = None,
    ) -> None:
        """Send a prompt, queueing it when the agent is already streaming.

        The order mirrors pi exactly and is observable: extension ``/command``s run
        first (they manage their own LLM interaction), then the ``input`` event may
        intercept or rewrite the text, then ``/skill:<name>`` and ``/template``
        expansion happens, and finally ``before_agent_start`` may inject a message
        or replace the system prompt for this turn.

        Raises:
            RuntimeError: streaming without ``streaming_behavior``.
            AuthenticationError: the provider has no usable credential.
        """
        if self._disposed:
            raise RuntimeError("AgentSession has been disposed")

        if await self._try_extension_command(text):
            return

        if self._extension_runner is not None:
            input_result = await self._extension_runner.emit_input(
                text, images, "user", streaming_behavior
            )
            if input_result.action == "handled":
                return
            if input_result.action == "transform":
                if input_result.text is not None:
                    text = input_result.text
                if input_result.images is not None:
                    images = input_result.images

        expanded_text = text
        if text.startswith("/"):
            expanded_text = self._expand_skill_command(text)
            expanded_text = expand_prompt_template(expanded_text, self.prompt_templates)

        if self._is_agent_run_active:
            if streaming_behavior is None:
                raise RuntimeError(
                    "Agent is already processing. Specify streamingBehavior ('steer' or "
                    "'followUp') to queue the message."
                )
            if streaming_behavior == "followUp":
                await self.follow_up(expanded_text, images)
            else:
                await self.steer(expanded_text, images)
            return

        self._assert_usable_auth()

        messages: list[AgentMessage] = [_user_message(expanded_text, images)]
        if self._extension_runner is not None:
            before = await self._extension_runner.emit_before_agent_start(
                expanded_text, images, self._agent.state.system_prompt, None
            )
            if before is not None:
                if before.system_prompt is not None:
                    self._agent.state.system_prompt = before.system_prompt
                for spec in before.messages:
                    messages.append(_custom_message_from_spec(spec))

        await self._run_agent_prompt(messages)

    async def steer(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        """Queue a message delivered after the current turn's tool calls."""
        self._steering_texts.append(text)
        self._agent.steer(_user_message(text, images))

    async def follow_up(self, text: str, images: Sequence[ImageContent] | None = None) -> None:
        """Queue a message delivered once the agent would otherwise stop."""
        self._follow_up_texts.append(text)
        self._agent.follow_up(_user_message(text, images))

    def get_steering_messages(self) -> list[str]:
        return list(self._steering_texts)

    def get_follow_up_messages(self) -> list[str]:
        return list(self._follow_up_texts)

    def clear_queue(self) -> dict[str, list[str]]:
        """Drop queued messages; returns what was dropped."""
        steering = list(self._steering_texts)
        follow_up = list(self._follow_up_texts)
        self._steering_texts.clear()
        self._follow_up_texts.clear()
        self._agent.clear_all_queues()
        return {"steering": steering, "followUp": follow_up}

    # -- lifecycle ---------------------------------------------------------------------

    async def abort(self) -> None:
        """Cancel the running turn (and any retry backoff), then wait for idle."""
        self._agent.abort()
        if self._retry_abort is not None:
            self._retry_abort.abort("Retry cancelled")
        await self.wait_for_idle()

    async def wait_for_idle(self) -> None:
        """Wait until the current prompt run (and its retries) has finished."""
        future = self._idle_future
        if future is None:
            return
        await asyncio.shield(future)

    def dispose(self) -> None:
        """Detach from the agent and drop listeners."""
        if self._disposed:
            return
        self._disposed = True
        if self._is_agent_run_active:
            self._agent.abort()
        self._unsubscribe_agent()
        self._listeners.clear()

    async def _run_agent_prompt(self, messages: list[AgentMessage]) -> None:
        self._is_agent_run_active = True
        idle_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._idle_future = idle_future
        try:
            await self._agent.prompt(messages)
            while await self._handle_post_agent_run():
                await self._agent.continue_()
        finally:
            self._is_agent_run_active = False
            self._idle_future = None
            if not idle_future.done():
                idle_future.set_result(None)

    async def _handle_post_agent_run(self) -> bool:
        """Retry or compact after a completed run; ``True`` continues the agent."""
        message = self._last_assistant_message
        self._last_assistant_message = None
        if message is None:
            return False

        if self._is_retryable_error(message) and await self._prepare_retry(message):
            return True

        if getattr(message, "stop_reason", None) == "error" and self._retry_attempt > 0:
            # Budget exhausted (or the error stopped being retryable): clear the
            # counter so the next run starts fresh.
            self._retry_attempt = 0

        if await self._check_compaction(message, skip_aborted_check=True) == "retry":
            return True

        # The loop drains both queues before agent_end, so anything left was
        # queued during the run and needs a continuation.
        return self._agent.has_queued_messages()

    # -- persistence + state reduction -------------------------------------------------

    async def _handle_agent_event(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        message = getattr(event, "message", None)

        if event_type == "message_start" and getattr(message, "role", None) == "user":
            text = _content_text(getattr(message, "content", ""))
            if text:
                if text in self._steering_texts:
                    self._steering_texts.remove(text)
                elif text in self._follow_up_texts:
                    self._follow_up_texts.remove(text)

        # Extensions observe every lifecycle event.  ``message_end`` is special:
        # handlers may replace the message.  The agent already stored the original
        # object in its state before emitting this event, so the only way to keep
        # state, listeners and persistence consistent is to mutate that object in
        # place rather than swap the reference (upstream does the same).
        if self._extension_runner is not None:
            if event_type == "message_end" and message is not None:
                replacement = await self._extension_runner.emit_message_end(message)
                if replacement is not None:
                    _replace_message_in_place(message, replacement)
            else:
                await self._dispatch_extension_event(event)

        await self._emit(event)

        if event_type != "message_end" or message is None:
            return

        role = getattr(message, "role", None)
        if role == "custom":
            await self._session_manager.append_custom_message(
                message.custom_type, message.content, message.display, message.details
            )
        elif role in ("user", "assistant", "toolResult"):
            await self._session_manager.append_message(message)
        # bashExecution / compactionSummary / branchSummary are persisted elsewhere.

        if role == "assistant":
            self._last_assistant_message = message
            if getattr(message, "stop_reason", None) != "error" and self._retry_attempt > 0:
                # Reset immediately so retries do not accumulate across the
                # multiple LLM calls a single turn can make.
                self._retry_attempt = 0

    # -- extensions --------------------------------------------------------------------

    def set_extension_runner(self, runner: Any) -> None:
        """Attach (or replace) the extension runner.

        Called again after a session replacement, because handlers hold a context
        that points at the live session.
        """
        self._extension_runner = runner
        if runner is None:
            return
        for tool in runner.get_tools():
            if all(existing.name != tool.name for existing in self._tools):
                self._tools.append(tool)
            if tool.name not in self._active_tool_names:
                self._active_tool_names.append(tool.name)

    @property
    def extension_runner(self) -> Any:
        return self._extension_runner

    async def _before_tool_call(self, context: Any, signal: Any) -> Any:
        """Bridge the agent's pre-tool hook to the extension ``tool_call`` event."""
        from .extensions.types import ToolCallEvent

        tool_call = getattr(context, "tool_call", None)
        event = ToolCallEvent(
            tool_name=getattr(tool_call, "name", ""),
            tool_call_id=getattr(tool_call, "id", ""),
            input=getattr(context, "args", None),
        )
        result = await self._extension_runner.emit_tool_call(event)
        if result is None:
            return None

        from pi_agent import BeforeToolCallResult

        return BeforeToolCallResult(
            block=result.block, reason=result.reason, terminate=result.terminate
        )

    async def _after_tool_call(self, context: Any, signal: Any) -> Any:
        """Bridge the agent's post-tool hook to the extension ``tool_result`` event."""
        from .extensions.types import ToolResultEvent

        tool_call = getattr(context, "tool_call", None)
        result = getattr(context, "result", None)
        event = ToolResultEvent(
            tool_name=getattr(tool_call, "name", ""),
            tool_call_id=getattr(tool_call, "id", ""),
            content=list(getattr(result, "content", None) or []),
            details=getattr(result, "details", None),
            is_error=bool(getattr(context, "is_error", False)),
        )
        updated = await self._extension_runner.emit_tool_result(event)
        if updated is None:
            return None

        from pi_agent import AfterToolCallResult

        return AfterToolCallResult(
            content=updated.content, details=updated.details, is_error=updated.is_error
        )

    async def _dispatch_extension_event(self, event: Any) -> None:
        """Forward one lifecycle event to extensions as a notification."""
        if self._extension_runner is None:
            return
        mapped = _to_extension_event(event)
        if mapped is not None:
            await self._extension_runner.emit(mapped)

    async def _try_extension_command(self, text: str) -> bool:
        """Run a ``/name`` extension command; returns True when it was handled."""
        if self._extension_runner is None or not text.startswith("/"):
            return False
        name, _, args = text[1:].partition(" ")
        command = self._extension_runner.get_commands().get(name)
        if command is None:
            return False

        ctx = self._extension_runner.create_context()
        result = command.handler(args, ctx) if _arity(command.handler) >= 2 else command.handler(args)
        if inspect.isawaitable(result):
            await result
        return True

    async def emit_session_start(
        self, reason: str = "startup", previous_session_file: str | None = None
    ) -> None:
        if self._extension_runner is None:
            return
        from .extensions.types import SessionStartEvent

        await self._extension_runner.emit(
            SessionStartEvent(reason=reason, previous_session_file=previous_session_file)
        )

    async def emit_session_shutdown(self, reason: str = "quit") -> None:
        if self._extension_runner is None:
            return
        from .extensions.types import SessionShutdownEvent

        await self._extension_runner.emit(SessionShutdownEvent(reason=reason))

    async def emit_session_info_changed(self, name: str | None = None) -> None:
        if self._extension_runner is None:
            return
        from .extensions.types import SessionInfoChangedEvent

        await self._extension_runner.emit(SessionInfoChangedEvent(name=name))

    # -- helpers exposed to extensions -------------------------------------------------

    def get_system_prompt(self) -> str:
        return self._agent.state.system_prompt

    def request_abort(self) -> None:
        """Synchronous abort trigger (``ctx.abort()`` has no await slot)."""
        self._agent.abort()

    async def send_custom_message(
        self,
        content: Any,
        *,
        custom_type: str = "extension",
        display: bool = True,
        details: Any = None,
        deliver_as: str | None = None,
    ) -> None:
        """Inject a message into the conversation.

        ``deliver_as`` mirrors pi: ``"steer"`` and ``"followUp"`` queue it for the
        running agent, otherwise it enters the transcript immediately and is
        persisted.  Injecting mid-run is best-effort by nature — the loop reads its
        message list at turn boundaries.
        """
        message = _custom_message(custom_type, content, display, details)
        if deliver_as == "steer":
            self._steering_texts.append(_content_text(content))
            self._agent.steer(message)
            return
        if deliver_as == "followUp":
            self._follow_up_texts.append(_content_text(content))
            self._agent.follow_up(message)
            return
        await self._session_manager.append_custom_message(custom_type, content, display, details)
        self._agent.state.messages.append(message)

    async def append_custom_entry(self, custom_type: str, data: Any = None) -> str:
        return await self._session_manager.append_custom(custom_type, data)

    async def set_label(self, entry_id: str, label: str | None) -> None:
        await self._session_manager.append_label(entry_id, label)

    async def exec(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a subprocess on behalf of an extension."""
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd or self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"Command timed out after {timeout}s: {command}") from None
        return {
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
            "code": process.returncode,
        }

    def get_active_tools(self) -> list[str]:
        return list(self._active_tool_names)

    def get_all_tools(self) -> list[AgentTool]:
        return list(self._tools)

    async def set_active_tools(self, names: Sequence[str]) -> None:
        unknown = [name for name in names if all(tool.name != name for tool in self._tools)]
        if unknown:
            raise ValueError(f"Unknown tools: {', '.join(unknown)}")
        self._active_tool_names = list(names)
        self._agent.set_tools([tool for tool in self._tools if tool.name in self._active_tool_names])
        self._base_system_prompt = self._build_system_prompt()
        self._agent.state.system_prompt = self._base_system_prompt

    def get_commands(self) -> list[dict[str, Any]]:
        """Prompt templates, skills and extension commands, in that order."""
        commands: list[dict[str, Any]] = []
        for template in self.prompt_templates:
            commands.append({"name": template.name, "description": template.description, "source": "prompt"})
        if self._resource_loader is not None:
            for skill in self._resource_loader.skills():
                if getattr(skill, "disable_model_invocation", False):
                    continue
                commands.append(
                    {"name": f"skill:{skill.name}", "description": skill.description, "source": "skill"}
                )
        if self._extension_runner is not None:
            for name, command in self._extension_runner.get_commands().items():
                commands.append(
                    {"name": name, "description": command.description, "source": "extension"}
                )
        return commands

    # -- auth --------------------------------------------------------------------------

    def _assert_usable_auth(self) -> None:
        if self._auth_storage is None:
            return
        resolved = self._auth_storage.resolve(self._model.provider)
        if resolved is None or not resolved.api_key:
            raise AuthenticationError(self._model.provider)

    async def _get_api_key(self, provider: str) -> str | None:
        if self._auth_storage is None:
            return None
        resolved = self._auth_storage.resolve(provider)
        return resolved.api_key if resolved is not None else None

    def _resolve_api_key(self) -> str | None:
        if self._auth_storage is None:
            return None
        resolved = self._auth_storage.resolve(self._model.provider)
        return resolved.api_key if resolved is not None else None

    # -- prompt expansion --------------------------------------------------------------

    def _expand_skill_command(self, text: str) -> str:
        if not text.startswith("/skill:"):
            return text
        space_index = text.find(" ")
        skill_name = text[7:] if space_index == -1 else text[7:space_index]
        args = "" if space_index == -1 else text[space_index + 1 :].strip()
        skills = self._resource_loader.skills() if self._resource_loader is not None else []
        expanded = expand_skill_command(skill_name, skills, args)
        return expanded if expanded is not None else text

    # -- retry -------------------------------------------------------------------------

    def _retry_settings(self) -> dict[str, Any]:
        settings = self._retry_settings_from_config()
        if self._auto_retry_override is not None:
            settings["enabled"] = self._auto_retry_override
        return settings

    def _retry_settings_from_config(self) -> dict[str, Any]:
        settings = dict(_DEFAULT_RETRY_SETTINGS)
        if self._settings_manager is None:
            return settings
        getter = getattr(self._settings_manager, "get_retry_settings", None)
        resolved: Mapping[str, Any] = {}
        if callable(getter):
            try:
                resolved = getter()
            except Exception:  # noqa: BLE001 - a bad setting must not break retries
                resolved = {}
        else:
            raw = self._settings_manager.get("retry")
            resolved = raw if isinstance(raw, Mapping) else {}
        for key in settings:
            value = resolved.get(key)
            if value is not None:
                settings[key] = value
        return settings

    def _is_retryable_error(self, message: AssistantMessage) -> bool:
        """Whether a failed assistant message looks like a transient error."""
        if getattr(message, "stop_reason", None) != "error":
            return False
        text = getattr(message, "error_message", None) or ""
        if not text:
            return False
        if _NON_RETRYABLE_ERROR_RE.search(text):
            return False
        return bool(_RETRYABLE_ERROR_RE.search(text))

    async def _prepare_retry(self, message: AssistantMessage) -> bool:
        settings = self._retry_settings()
        if not settings.get("enabled"):
            return False

        self._retry_attempt += 1
        if self._retry_attempt > int(settings.get("maxRetries") or 0):
            # Keep the attempt count so the caller can report the final failure.
            self._retry_attempt -= 1
            return False

        delay_ms = _retry_delay_ms(settings, self._retry_attempt)

        # Remove the failed assistant from agent state (it stays in the session
        # file for history); agent.continue() rejects an assistant tail.
        messages = self._agent.state.messages
        if messages and getattr(messages[-1], "role", None) == "assistant":
            self._agent.set_messages(messages[:-1])

        controller = AbortController()
        self._retry_abort = controller
        try:
            completed = await self._sleep_ms(delay_ms, controller.signal)
        finally:
            self._retry_abort = None
        if not completed:
            self._retry_attempt = 0
            return False
        return True

    async def _sleep_ms(self, delay_ms: int, signal: AbortSignal) -> bool:
        """Sleep for the backoff; ``False`` when the signal fired first."""
        if signal.aborted:
            return False
        if delay_ms <= 0:
            return True
        try:
            await asyncio.wait_for(signal.wait(), timeout=delay_ms / 1000)
        except asyncio.TimeoutError:
            return True
        return False

    # -- compaction --------------------------------------------------------------------

    def _model_key(self) -> str:
        return f"{self._model.provider}/{self._model.id}"

    def _compaction_settings(self) -> CompactionSettings:
        settings = self._compaction_settings_from_config()
        if self._auto_compaction_override is not None:
            settings = CompactionSettings(
                enabled=self._auto_compaction_override,
                reserve_tokens=settings.reserve_tokens,
                keep_recent_tokens=settings.keep_recent_tokens,
            )
        return settings

    def _compaction_settings_from_config(self) -> CompactionSettings:
        if self._settings_manager is None:
            return DEFAULT_COMPACTION_SETTINGS
        try:
            raw = self._settings_manager.get_compaction_settings(self._model_key())
        except Exception:  # noqa: BLE001 - invalid settings must not break the run
            return DEFAULT_COMPACTION_SETTINGS
        return CompactionSettings(
            enabled=bool(raw.get("enabled", True)),
            reserve_tokens=int(raw.get("reserveTokens", DEFAULT_COMPACTION_RESERVE_TOKENS)),
            keep_recent_tokens=int(raw.get("keepRecentTokens", DEFAULT_COMPACTION_KEEP_RECENT_TOKENS)),
        )

    async def _prepare_next_turn(self, turn: PrepareNextTurnContext) -> AgentLoopTurnUpdate | None:
        """Auto-compaction hook: runs before every LLM call after the first turn."""
        message = getattr(turn, "message", None)
        if getattr(message, "role", None) != "assistant":
            return None
        if await self._check_compaction(message, skip_aborted_check=True) == "none":
            return None
        # The summary must take effect immediately: hand the loop the rebuilt
        # context instead of the one it was about to send.
        return AgentLoopTurnUpdate(
            context=AgentContext(
                messages=list(self._agent.state.messages),
                system_prompt=self._agent.state.system_prompt,
                tools=list(self._agent.state.tools),
            )
        )

    async def _check_compaction(
        self, message: AssistantMessage, *, skip_aborted_check: bool
    ) -> _CompactionOutcome:
        settings = self._compaction_settings()
        if not settings.enabled:
            return "none"
        if skip_aborted_check and getattr(message, "stop_reason", None) == "aborted":
            return "none"

        context_window = self._model.context_window if self._model is not None else 0

        # A pre-compaction usage/error must not retrigger compaction right after
        # one just finished.
        path = self._session_manager.get_path()
        compaction_entry = get_latest_compaction_entry(path)
        if compaction_entry is not None:
            message_timestamp = int(getattr(message, "timestamp", 0) or 0)
            if message_timestamp <= _iso_to_millis(compaction_entry.timestamp):
                return "none"

        context_tokens = 0
        usage = getattr(message, "usage", None)
        if getattr(message, "stop_reason", None) != "error" and usage is not None:
            context_tokens = calculate_context_tokens(usage)
        if context_tokens <= 0:
            context_tokens = estimate_context_tokens(self._agent.state.messages)

        if not should_compact(context_tokens, context_window, settings):
            return "none"
        if await self._run_auto_compaction():
            return "compacted"
        return "none"

    async def _run_auto_compaction(self) -> bool:
        """Threshold compaction; failures are recorded, never raised into the run."""
        self._is_compacting = True
        try:
            try:
                result = await compact_session(
                    self._session_manager.get_path(),
                    self._model,
                    settings=self._compaction_settings(),
                    stream_fn=self._stream_fn,
                    api_key=self._resolve_api_key(),
                )
            except Exception as error:  # noqa: BLE001 - upstream swallows auto-compaction errors
                self._last_compaction_error = str(error)
                return False

            await self._session_manager.append_compaction(
                result.summary,
                result.first_kept_entry_id,
                result.tokens_before,
                details=None,
                usage=result.usage,
            )
            self._replace_context_from_session()
            self._last_compaction_error = None
            return True
        finally:
            self._is_compacting = False

    async def compact(self, custom_instructions: str | None = None) -> CompactionResult:
        """Manually compact the session; aborts the current run first."""
        await self.abort()
        self._is_compacting = True
        try:
            result = await compact_session(
                self._session_manager.get_path(),
                self._model,
                settings=self._compaction_settings(),
                stream_fn=self._stream_fn,
                api_key=self._resolve_api_key(),
                custom_instructions=custom_instructions,
            )
            await self._session_manager.append_compaction(
                result.summary,
                result.first_kept_entry_id,
                result.tokens_before,
                details=None,
                usage=result.usage,
            )
            self._replace_context_from_session()
            return result
        finally:
            self._is_compacting = False

    # -- runtime controls --------------------------------------------------------------
    #
    # Small, explicit knobs that the RPC surface needs.  This is the only session
    # state that is deliberately *not* persisted: pi treats it as per-process.

    @property
    def is_compacting(self) -> bool:
        return self._is_compacting

    @property
    def pending_message_count(self) -> int:
        """Queued steering plus follow-up messages not yet delivered."""
        return len(self._steering_texts) + len(self._follow_up_texts)

    @property
    def steering_mode(self) -> str:
        return self._agent.steering_mode

    def set_steering_mode(self, mode: str) -> None:
        self._agent.steering_mode = _validate_queue_mode(mode)

    @property
    def follow_up_mode(self) -> str:
        return self._agent.follow_up_mode

    def set_follow_up_mode(self, mode: str) -> None:
        self._agent.follow_up_mode = _validate_queue_mode(mode)

    @property
    def auto_compaction_enabled(self) -> bool:
        if self._auto_compaction_override is not None:
            return self._auto_compaction_override
        return self._compaction_settings().enabled

    def set_auto_compaction(self, enabled: bool) -> None:
        self._auto_compaction_override = bool(enabled)

    @property
    def auto_retry_enabled(self) -> bool:
        if self._auto_retry_override is not None:
            return self._auto_retry_override
        return bool(self._retry_settings().get("enabled"))

    def set_auto_retry(self, enabled: bool) -> None:
        self._auto_retry_override = bool(enabled)

    def abort_retry(self) -> None:
        """Cancel an in-flight retry backoff, if one is waiting."""
        if self._retry_abort is not None:
            self._retry_abort.abort()

    @property
    def last_compaction_error(self) -> str | None:
        return self._last_compaction_error

    def _replace_context_from_session(self) -> None:
        """Rebuild the in-memory transcript from the session so a summary applies."""
        self._agent.set_messages(self._session_manager.build_context().messages)

    # -- model / thinking --------------------------------------------------------------

    async def set_model(self, model: Model) -> None:
        """Switch models and record the change in the session."""
        self._model = model
        self._agent.state.model = model
        self._thinking_level = clamp_thinking_level(model, self._thinking_level)
        self._agent.state.thinking_level = self._thinking_level
        await self._session_manager.append_model_change(model.provider, model.id)
        if self._extension_runner is not None:
            from .extensions.types import ModelSelectEvent

            await self._extension_runner.emit(
                ModelSelectEvent(provider=model.provider, model_id=model.id)
            )

    async def set_thinking_level(self, level: str) -> None:
        """Set (and clamp) the thinking level, recording the change."""
        self._thinking_level = clamp_thinking_level(self._model, level)
        self._agent.state.thinking_level = self._thinking_level
        await self._session_manager.append_thinking_level_change(self._thinking_level)
        if self._extension_runner is not None:
            from .extensions.types import ThinkingLevelSelectEvent

            await self._extension_runner.emit(
                ThinkingLevelSelectEvent(level=self._thinking_level)
            )

    async def set_session_name(self, name: str) -> None:
        await self._session_manager.set_session_name(name)

    # -- construction helpers ----------------------------------------------------------

    async def _convert_to_llm(self, messages: list[AgentMessage]) -> list[Any]:
        """Async adapter: the agent loop awaits this, the projection is sync."""
        return convert_to_llm(messages)

    def _build_system_prompt(self) -> str:
        loader = self._resource_loader
        tool_snippets = {
            tool.name: tool.prompt_snippet for tool in self._tools if tool.prompt_snippet
        }
        tool_guidelines = {
            tool.name: list(tool.prompt_guidelines or []) for tool in self._tools
        }
        options = get_system_prompt_options(
            cwd=self._cwd,
            selected_tools=[tool.name for tool in self._tools],
            tool_snippets=tool_snippets,
            tool_guidelines=tool_guidelines,
            known_tools={tool.name for tool in self._tools},
            custom_prompt=loader.system_prompt_override() if loader is not None else None,
            append_system_prompt=loader.append_system_prompt() if loader is not None else None,
            context_files=loader.context_files() if loader is not None else None,
            skills=loader.skills() if loader is not None else None,
        )
        return build_system_prompt(options)


__all__ = [
    "DEFAULT_THINKING_LEVEL",
    "AgentSession",
    "StreamingBehavior",
    "expand_prompt_template",
    "parse_command_args",
    "substitute_args",
]
