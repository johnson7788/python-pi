"""__PLUGIN_NAME__

__PLUGIN_DESC__

改这个文件里的 setup()，然后：

    python3 plugin-kit/pikit.py check .
    python3 plugin-kit/pikit.py dev . --args '{"name": "Ada"}'
    python3 plugin-kit/pikit.py pack .

工具函数可以直接 return 字符串；返回 AgentToolResult 也可以。
"""

from __future__ import annotations

from pi_coding_agent.plugins import PluginAPI


def setup(api: PluginAPI) -> None:
    # 宿主当前只调用 setup(api)，还没有注入配置；用 getattr 读，向前兼容。
    config = getattr(api, "config", {}) or {}
    prefix = str(config.get("greeting") or "你好")

    @api.register_tool(
        name="hello",
        description="向指定的人问好。用户要求打招呼、问候某人时调用。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "要问候的人名"},
            },
            "required": ["name"],
        },
    )
    async def hello(tool_call_id, args, signal, on_update):
        name = str((args or {}).get("name") or "").strip()
        if not name:
            # 参数错误要返回可诊断的文本，不要抛异常：模型会把它当工具结果继续处理。
            return "缺少 name 参数。"
        return f"{prefix}，{name}！"
