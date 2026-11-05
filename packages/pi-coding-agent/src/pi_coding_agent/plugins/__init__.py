"""Public SDK for python-pi plugins.

A plugin is a Python module with a ``setup(api)`` factory; this module re-exports the
same registration surface extensions use, under plugin-oriented names::

    from pi_coding_agent.plugins import PluginAPI, ToolResult

    def setup(api: PluginAPI) -> None:
        @api.register_tool(name="hello", description="Say hello")
        async def hello(tool_call_id, args, signal, on_update):
            return ToolResult(text="hello")

The runtime loads a plugin only for workers whose tenant/app has it enabled; the code
still runs in-process with the server's permissions, so installation is an admin action.
"""

from __future__ import annotations

from pi_agent import AgentTool, AgentToolResult as ToolResult

from ..extensions import (
    BeforeAgentStartEvent,
    BeforeAgentStartResult,
    ExtensionAPI as PluginAPI,
    ExtensionContext,
    InputEvent,
    InputEventResult,
    ToolCallEvent,
    ToolCallEventResult,
    ToolResultEvent,
    ToolResultEventResult,
)

__all__ = [
    "AgentTool",
    "BeforeAgentStartEvent",
    "BeforeAgentStartResult",
    "ExtensionContext",
    "InputEvent",
    "InputEventResult",
    "PluginAPI",
    "ToolCallEvent",
    "ToolCallEventResult",
    "ToolResult",
    "ToolResultEvent",
    "ToolResultEventResult",
]
