"""Minimal python-pi plugin.

Install: copy this directory to ``<data_root>/plugins/hello-pi`` (team) and enable it
from the 插件 page for the app that should see the tool.

Validate locally before installing::

    python3 plugin-kit/pikit.py check examples/plugins/hello-pi
    python3 plugin-kit/pikit.py dev examples/plugins/hello-pi --args '{"name": "Ada"}'
"""

from __future__ import annotations

from pi_coding_agent.plugins import PluginAPI


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
        # Returning a string is the simplest correct shape: the runtime wraps it in a
        # TextContent result.  ``ToolResult`` is ``AgentToolResult``, whose fields are
        # ``content``/``details``/``usage``/``terminate``/``added_tool_names``; there is
        # no ``text=`` keyword (that raised TypeError until 2026-11).
        return f"Hello, {name}! (from hello-pi)"
