"""__PLUGIN_NAME__

__PLUGIN_DESC__

一个只读 HTTP 工具的骨架：超时、响应大小上限、结果裁剪、错误文本化、以及
`asyncio.to_thread`（不要让阻塞 IO 卡住 agent 的事件循环）。

    export __PLUGIN_ENV__=your-token
    python3 plugin-kit/pikit.py check .
    python3 plugin-kit/pikit.py dev . --tool http_lookup --args '{"query": "乳腺癌"}'
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request

from pi_coding_agent.plugins import PluginAPI

MAX_RESPONSE_BYTES = 200_000
REQUEST_TIMEOUT = 15
MAX_RESULTS = 5


def setup(api: PluginAPI) -> None:
    config = getattr(api, "config", {}) or {}
    base_url = str(config.get("baseUrl") or "https://api.example.com").rstrip("/")
    api_key = str(config.get("apiKey") or os.environ.get("__PLUGIN_ENV__") or "")

    @api.register_tool(
        name="http_lookup",
        description=(
            "在内部知识库中检索资料，返回标题、摘要和链接。"
            "需要查资料、找依据、确认某个说法时调用。只读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词，尽量具体，不超过 200 字",
                },
            },
            "required": ["query"],
        },
    )
    async def http_lookup(tool_call_id, args, signal, on_update):
        query = str((args or {}).get("query") or "").strip()
        if not query:
            return "缺少 query 参数。"
        if len(query) > 200:
            return "query 太长，上限 200 字。"

        try:
            # 阻塞的 urllib 放到线程里，避免卡住整个 worker。
            payload = await asyncio.to_thread(_fetch, base_url, query, api_key)
        except Exception as error:  # noqa: BLE001 - 下游什么都可能抛
            # 只回可诊断信息：不要泄露 token、堆栈和内部地址。
            return f"检索失败：{type(error).__name__}: {error}"

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return "没有检索到结果。"

        lines: list[str] = []
        for item in items[:MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            summary = str(item.get("summary") or "").strip()[:200]
            head = f"- {title}" if title else "- （无标题）"
            if url:
                head += f"（{url}）"
            lines.append(head)
            if summary:
                lines.append(f"  {summary}")
        return "\n".join(lines) if lines else "没有检索到结果。"


def _fetch(base_url: str, query: str, api_key: str) -> dict:
    url = f"{base_url}/search?{urllib.parse.urlencode({'q': query})}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))
