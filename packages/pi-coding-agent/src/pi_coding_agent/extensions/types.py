"""Extension event and registration contracts.

Port of ``packages/coding-agent/src/core/extensions/types.ts`` with the TUI-only
surface removed.

**Design choices, and where they come from pi:**

* The extension entry point is a module-level ``setup(pi)`` factory — the direct
  analogue of pi's ``export default function (pi: ExtensionAPI)``.  It may be
  sync or async; an async factory is awaited before startup continues.
* Registration is by explicit method (``pi.on`` / ``pi.register_tool`` / ...)
  rather than by decorator-implicit scanning, because that is how pi works and it
  keeps load order obvious.  ``register_tool`` and ``register_command`` also work
  as decorators, which costs nothing extra.
* There is **no permission sandbox**.  pi is explicit about this: extensions run
  with the full permissions of the process and you should only install ones you
  trust.  The only gate kept is that *project-local* extensions load only after
  the project is trusted, matching pi.  See ``docs/EXTENSIONS.md``.
* Handlers may be sync or async.  pi awaits whatever a handler returns; so do we.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Union

from pi_agent import AgentTool

# --------------------------------------------------------------------------------------
# Events emitted by this port
# --------------------------------------------------------------------------------------

#: Events this port emits.  Anything pi defines that is absent here is documented in
#: ``docs/EXTENSIONS.md`` as not-yet-emitted (provider/TUI/compaction internals).
EMITTED_EVENTS = frozenset(
    {
        "session_start",
        "session_shutdown",
        "session_info_changed",
        "session_before_switch",
        "session_before_fork",
        "input",
        "before_agent_start",
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "tool_call",
        "tool_result",
        "model_select",
        "thinking_level_select",
    }
)


@dataclass
class SessionStartEvent:
    reason: str = "startup"
    previous_session_file: str | None = None
    type: Literal["session_start"] = "session_start"


@dataclass
class SessionShutdownEvent:
    reason: str = "quit"
    type: Literal["session_shutdown"] = "session_shutdown"


@dataclass
class SessionInfoChangedEvent:
    name: str | None = None
    type: Literal["session_info_changed"] = "session_info_changed"


@dataclass
class SessionBeforeSwitchEvent:
    reason: str = "resume"
    target_session_file: str | None = None
    type: Literal["session_before_switch"] = "session_before_switch"


@dataclass
class SessionBeforeForkEvent:
    entry_id: str = ""
    position: str = "before"
    type: Literal["session_before_fork"] = "session_before_fork"


@dataclass
class InputEvent:
    """Fired for every prompt before it is sent or queued."""

    text: str = ""
    images: list[Any] | None = None
    source: str = "user"
    streaming_behavior: str | None = None
    type: Literal["input"] = "input"


@dataclass
class BeforeAgentStartEvent:
    prompt: str = ""
    images: list[Any] | None = None
    system_prompt: str = ""
    system_prompt_options: Any = None
    type: Literal["before_agent_start"] = "before_agent_start"


@dataclass
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass
class AgentEndEvent:
    messages: list[Any] = field(default_factory=list)
    type: Literal["agent_end"] = "agent_end"


@dataclass
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass
class TurnEndEvent:
    message: Any = None
    tool_results: list[Any] = field(default_factory=list)
    type: Literal["turn_end"] = "turn_end"


@dataclass
class MessageStartEvent:
    message: Any = None
    type: Literal["message_start"] = "message_start"


@dataclass
class MessageUpdateEvent:
    message: Any = None
    assistant_message_event: Any = None
    type: Literal["message_update"] = "message_update"


@dataclass
class MessageEndEvent:
    message: Any = None
    type: Literal["message_end"] = "message_end"


@dataclass
class ToolExecutionStartEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None
    type: Literal["tool_execution_start"] = "tool_execution_start"


@dataclass
class ToolExecutionUpdateEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    args: Any = None
    partial_result: Any = None
    type: Literal["tool_execution_update"] = "tool_execution_update"


@dataclass
class ToolExecutionEndEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    result: Any = None
    is_error: bool = False
    type: Literal["tool_execution_end"] = "tool_execution_end"


@dataclass
class ToolCallEvent:
    """Fired before a tool runs.  Mutate ``input`` in place to change the arguments."""

    tool_name: str = ""
    tool_call_id: str = ""
    input: Any = None
    type: Literal["tool_call"] = "tool_call"


@dataclass
class ToolResultEvent:
    tool_name: str = ""
    tool_call_id: str = ""
    content: Any = None
    details: Any = None
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


@dataclass
class ModelSelectEvent:
    provider: str = ""
    model_id: str = ""
    type: Literal["model_select"] = "model_select"


@dataclass
class ThinkingLevelSelectEvent:
    level: str = ""
    type: Literal["thinking_level_select"] = "thinking_level_select"


ExtensionEvent = Union[
    SessionStartEvent,
    SessionShutdownEvent,
    SessionInfoChangedEvent,
    SessionBeforeSwitchEvent,
    SessionBeforeForkEvent,
    InputEvent,
    BeforeAgentStartEvent,
    AgentStartEvent,
    AgentEndEvent,
    TurnStartEvent,
    TurnEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    MessageEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolExecutionEndEvent,
    ToolCallEvent,
    ToolResultEvent,
    ModelSelectEvent,
    ThinkingLevelSelectEvent,
]


# --------------------------------------------------------------------------------------
# Handler results
# --------------------------------------------------------------------------------------


@dataclass
class InputEventResult:
    """How an ``input`` handler wants the prompt treated.

    ``action="continue"`` is the default and lets the next handler decide.
    ``"transform"`` replaces the text (and optionally images); ``"handled"`` stops
    processing entirely — the prompt is consumed and nothing is sent to the model.
    """

    action: Literal["continue", "transform", "handled"] = "continue"
    text: str | None = None
    images: list[Any] | None = None


@dataclass
class BeforeAgentStartResult:
    """Messages to inject, and/or a replacement system prompt for this turn.

    ``system_prompt`` is chained: each extension sees the previous one's value as
    ``event.system_prompt``.  ``messages`` accumulates across extensions.
    """

    messages: list[Any] = field(default_factory=list)
    system_prompt: str | None = None


@dataclass
class MessageEndResult:
    """Replace a finalized message.  The role must not change."""

    message: Any = None


@dataclass
class ToolCallEventResult:
    """Block a tool call (optionally terminating the batch), or pass."""

    block: bool | None = None
    reason: str | None = None
    terminate: bool | None = None


@dataclass
class ToolResultEventResult:
    """Replace parts of a tool result before it enters the transcript."""

    content: Any = None
    details: Any = None
    is_error: bool | None = None


@dataclass
class SessionBeforeEventResult:
    """Cancel a pending session replacement."""

    cancel: bool | None = None


@dataclass
class CustomMessageSpec:
    """A message an extension wants injected into the conversation."""

    custom_type: str
    content: Any
    display: bool = True
    details: Any = None


# --------------------------------------------------------------------------------------
# Registration records
# --------------------------------------------------------------------------------------


@dataclass
class RegisteredCommand:
    name: str
    description: str
    handler: Callable[..., Any]
    source: str = ""


@dataclass
class RegisteredFlag:
    name: str
    description: str
    default: bool | str | None = None


@dataclass
class ProviderRegistration:
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass
class ExtensionError:
    """A handler raised.  Errors are captured, never propagated into the loop."""

    extension_path: str
    event: str
    error: str
    stack: str | None = None


ExtensionHandler = Callable[[Any, Any], Any]


__all__ = [
    "EMITTED_EVENTS",
    "AgentEndEvent",
    "AgentStartEvent",
    "BeforeAgentStartEvent",
    "BeforeAgentStartResult",
    "CustomMessageSpec",
    "ExtensionError",
    "ExtensionEvent",
    "ExtensionHandler",
    "InputEvent",
    "InputEventResult",
    "MessageEndEvent",
    "MessageEndResult",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "ModelSelectEvent",
    "ProviderRegistration",
    "RegisteredCommand",
    "RegisteredFlag",
    "SessionBeforeEventResult",
    "SessionBeforeForkEvent",
    "SessionBeforeSwitchEvent",
    "SessionInfoChangedEvent",
    "SessionShutdownEvent",
    "SessionStartEvent",
    "ThinkingLevelSelectEvent",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolResultEvent",
    "ToolResultEventResult",
    "TurnEndEvent",
    "TurnStartEvent",
]
