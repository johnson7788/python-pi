"""Agent-layer contracts.

``AgentMessage`` is intentionally generic here: the upstream TypeScript declares
``Message | CustomAgentMessages[keyof CustomAgentMessages]`` and lets downstream
packages augment the union by declaration merging.  Python has no such merging,
so the loop treats messages structurally and the coding-agent package supplies
the concrete union.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Union

from pi_ai import (
    AbortSignal,
    AssistantMessage,
    Context,
    EventStream,
    ImageContent,
    Model,
    StreamOptions,
    TextContent,
    ToolResultMessage,
    Usage,
)

#: A conversation message as seen by the agent loop.  Structurally any object
#: carrying ``role`` and the role-specific fields (see ``pi_ai.types``).
AgentMessage = Any

ToolResultContent = Union[TextContent, ImageContent]


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


@dataclass
class AgentToolResult:
    """What a tool returns to the loop."""

    content: list[ToolResultContent] = field(default_factory=list)
    details: Any = None
    usage: Usage | None = None
    #: When every result in a tool batch sets this, the loop stops after the turn.
    terminate: bool | None = None
    added_tool_names: list[str] | None = None


#: Streaming progress callback passed to ``execute``.
ToolUpdateCallback = Callable[[AgentToolResult], None]

#: ``(tool_call_id, args, signal, on_update) -> result``
ToolExecuteFn = Callable[[str, Any, AbortSignal | None, ToolUpdateCallback], Awaitable[AgentToolResult]]


@dataclass
class AgentTool:
    """A tool the model may call."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    execute: ToolExecuteFn | None = None
    label: str | None = None
    #: One-line summary injected into the system prompt when the tool is active.
    prompt_snippet: str | None = None
    #: Extra guideline bullets injected into the system prompt.
    prompt_guidelines: list[str] | None = None
    #: ``"sequential"`` forces the whole batch to run one call at a time.
    execution_mode: Literal["parallel", "sequential"] = "parallel"
    #: Optional hook to normalise raw arguments before validation.
    prepare_arguments: Callable[[dict[str, Any]], dict[str, Any]] | None = None


# --------------------------------------------------------------------------------------
# Context / config
# --------------------------------------------------------------------------------------


@dataclass
class AgentContext:
    messages: list[AgentMessage]
    system_prompt: str | None = None
    tools: list[AgentTool] = field(default_factory=list)


@dataclass
class BeforeToolCallContext:
    assistant_message: Any
    tool_call: Any
    args: Any
    context: AgentContext


@dataclass
class BeforeToolCallResult:
    block: bool | None = None
    reason: str | None = None
    terminate: bool | None = None


@dataclass
class AfterToolCallContext:
    assistant_message: Any
    tool_call: Any
    args: Any
    result: AgentToolResult
    is_error: bool
    context: AgentContext


@dataclass
class AfterToolCallResult:
    content: list[ToolResultContent] | None = None
    details: Any = None
    usage: Usage | None = None
    is_error: bool | None = None
    terminate: bool | None = None


@dataclass
class PrepareNextTurnContext:
    """Snapshot of a completed turn, handed to ``prepare_next_turn`` / ``should_stop_after_turn``."""

    message: Any
    tool_results: list[ToolResultMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


@dataclass
class AgentLoopTurnUpdate:
    context: AgentContext | None = None
    model: Model | None = None
    thinking_level: str | None = None


#: ``(messages, signal) -> messages``
TransformContextFn = Callable[[list[AgentMessage], AbortSignal | None], Awaitable[list[AgentMessage]]]

#: Stream function: the boundary where ``AgentMessage[]`` becomes ``Message[]``.
StreamFn = Callable[[Model, Context, StreamOptions], EventStream]


@dataclass
class AgentLoopConfig:
    """Everything the loop needs that is not the conversation itself.

    ``convert_to_llm`` must not raise: return a safe fallback instead, because an
    exception here bypasses the normal event sequence.
    """

    model: Model
    convert_to_llm: Callable[[list[AgentMessage]], Awaitable[list[Any]]]
    api_key: str | None = None
    get_api_key: Callable[[str], Awaitable[str | None]] | None = None
    reasoning: str | None = None
    transform_context: TransformContextFn | None = None
    before_tool_call: Callable[[BeforeToolCallContext, AbortSignal | None], Awaitable[BeforeToolCallResult | None]] | None = None
    after_tool_call: Callable[[AfterToolCallContext, AbortSignal | None], Awaitable[AfterToolCallResult | None]] | None = None
    get_steering_messages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    get_follow_up_messages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    prepare_next_turn: Callable[[PrepareNextTurnContext], Awaitable[AgentLoopTurnUpdate | None]] | None = None
    should_stop_after_turn: Callable[[PrepareNextTurnContext], Awaitable[bool]] | None = None
    tool_execution: Literal["parallel", "sequential"] = "parallel"
    session_id: str | None = None
    stream_fn: StreamFn | None = None
    stream_options: StreamOptions | None = None


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


@dataclass
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass
class AgentEndEvent:
    messages: list[AgentMessage] = field(default_factory=list)
    type: Literal["agent_end"] = "agent_end"


@dataclass
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass
class TurnEndEvent:
    message: Any = None
    tool_results: list[ToolResultMessage] = field(default_factory=list)
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
    partial_result: AgentToolResult | None = None
    type: Literal["tool_execution_update"] = "tool_execution_update"


@dataclass
class ToolExecutionEndEvent:
    tool_call_id: str = ""
    tool_name: str = ""
    result: AgentToolResult | None = None
    is_error: bool = False
    type: Literal["tool_execution_end"] = "tool_execution_end"


AgentEvent = Union[
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
]

#: Signature of a consumer of agent events.  Sync or async; the loop awaits
#: awaitable results so async listeners cannot be silently interleaved.
AgentEventSink = Callable[[AgentEvent], Any]


__all__ = [
    "AfterToolCallContext",
    "AfterToolCallResult",
    "AgentContext",
    "AgentEndEvent",
    "AgentEvent",
    "AgentEventSink",
    "AgentLoopConfig",
    "AgentLoopTurnUpdate",
    "AgentMessage",
    "AgentStartEvent",
    "AgentTool",
    "AgentToolResult",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PrepareNextTurnContext",
    "StreamFn",
    "ToolEndEvent",
    "ToolExecuteFn",
    "ToolExecutionEndEvent",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "ToolResultContent",
    "ToolUpdateCallback",
    "TransformContextFn",
    "TurnEndEvent",
    "TurnStartEvent",
]

# Backwards-friendly alias used by some call sites.
ToolEndEvent = ToolExecutionEndEvent
