"""Minimal python-pi plugin.

Install: copy this directory to ``<data_root>/plugins/hello-pi`` (team) and enable it
from the 插件 page for the app that should see the tool.
"""

from __future__ import annotations

from pi_coding_agent.plugins import PluginAPI, ToolResult


def setup(api: PluginAPI) -> None:
    @api.register_tool(
        name="hello_plugin",
        description="Return a greeting from the hello-pi plugin.",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Who to greet"}},
            "required": [],
        },
    )
    async def hello_plugin(tool_call_id, args, signal, on_update):
        name = str((args or {}).get("name") or "world")
        return ToolResult(text=f"Hello, {name}! (from hello-pi)")
