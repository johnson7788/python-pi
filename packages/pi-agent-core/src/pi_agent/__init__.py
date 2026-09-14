"""pi-agent-core — the agent runtime.

Python port of ``@earendil-works/pi-agent-core``.  Layering mirrors upstream:

* :mod:`pi_agent.loop` is the bare, storage-free loop and depends only on pi-ai.
* :mod:`pi_agent.agent` adds state reduction, queueing and lifecycle.

The upstream ``harness/`` subtree (durable lanes, checkpoints, session storage) is
deliberately **not** ported in v0.1: the core loop has no dependency on it.
"""

from __future__ import annotations

from .agent import Agent, AgentState, PendingMessageQueue, QueueMode
from .loop import agent_loop, agent_loop_continue, run_agent_loop, run_agent_loop_continue
from .stream_fn import default_stream_fn, get_default_stream_fn
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentEventSink,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentStartEvent,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PrepareNextTurnContext,
    StreamFn,
    ToolEndEvent,
    ToolExecuteFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    ToolResultContent,
    ToolUpdateCallback,
    TransformContextFn,
    TurnEndEvent,
    TurnStartEvent,
)

__version__ = "0.1.0"

__all__ = [
    "AfterToolCallContext",
    "AfterToolCallResult",
    "Agent",
    "AgentContext",
    "AgentEndEvent",
    "AgentEvent",
    "AgentEventSink",
    "AgentLoopConfig",
    "AgentLoopTurnUpdate",
    "AgentMessage",
    "AgentStartEvent",
    "AgentState",
    "AgentTool",
    "AgentToolResult",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PendingMessageQueue",
    "PrepareNextTurnContext",
    "QueueMode",
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
    "agent_loop",
    "agent_loop_continue",
    "default_stream_fn",
    "get_default_stream_fn",
    "run_agent_loop",
    "run_agent_loop_continue",
]
