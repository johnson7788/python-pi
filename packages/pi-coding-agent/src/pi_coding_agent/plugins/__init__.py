"""Public SDK for python-pi plugins.

A plugin is a Python module with a ``setup(api)`` factory; this module re-exports the
same registration surface extensions use, under plugin-oriented names::

    from pi_coding_agent.plugins import PluginAPI

    def setup(api: PluginAPI) -> None:
        @api.register_tool(name="hello", description="Say hello")
        async def hello(tool_call_id, args, signal, on_update):
            return "hello"          # returning a string is the simplest result shape

The runtime loads a plugin only for workers whose tenant/app has it enabled; the code
still runs in-process with the server's permissions, so installation is an admin action.

Two things make in-process plugins safe to write against the workspace:

* ``api.cwd`` is the worker's workspace.  ``os.getcwd()`` is *process-wide*, shared by
  every tenant's worker, so it must not be used to find user files.
* :func:`workspace_path` resolves a relative path against ``api.cwd`` and refuses
  escapes (``..``, absolute paths outside the root, symlink walks).
"""

from __future__ import annotations

import os
from pathlib import Path

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


def workspace_path(api: PluginAPI, relative: str) -> Path:
    """Resolve ``relative`` inside the worker's workspace, or raise ``ValueError``.

    Use this instead of ``open("data.csv")``: a plugin runs in the server process, so a
    relative path would resolve against the process cwd — which belongs to whichever
    tenant's worker happened to start last.
    """
    root = Path(getattr(api, "cwd", "") or os.getcwd()).resolve()
    candidate = (root / str(relative)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes the workspace: {relative}")
    return candidate


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
    "workspace_path",
]
