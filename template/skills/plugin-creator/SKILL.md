---
name: plugin-creator
description: 当用户想给 python-pi 增加一个"模型能调用的新能力"——查内部系统、调 HTTP API、算一个数、跑一段固定脚本——或说"帮我做个插件""能不能让它直接查 XX""把这个接口接进来"时使用。先判断该写 Skill 还是 Plugin，按五问访谈生成 plugin.json 与 setup(api)，自检后打包成可安装 zip。
---

# 插件工坊

目标：把用户的一句需求，变成一个**能装上、模型真的会调用、失败也可诊断**的插件。
你不是在写代码，你是在交付一个可安装的构件。

## 0. 先判断层级（不要跳过）

| 用户要的 | 用什么 | 为什么 |
|---|---|---|
| 改「怎么做/输出什么格式/按什么标准判断/话术」 | **Skill**（用 `skill-creator`） | 不需要新代码，提示词就够 |
| 查一个系统、调一个 API、算一个数、跑一段固定脚本 | **Plugin**（本技能） | 需要模型之外的真实执行 |
| 已经有工具/插件能做 | 直接用，**不要新建** | 重复工具会让模型选择困难 |

判断问句：**「模型现在只靠读写文件 + 现有工具，能不能完成？」** 能 → 不写插件。

## 1. 五问访谈（缺一个就停下来问，不要猜）

1. **什么时候该调用它？** 一句话场景（"用户问药品说明书时"）。
2. **输入是什么？** 字段、类型、必填、上限（如"检索词 ≤200 字"）。
3. **输出给模型看什么？** 文本 / 列表 / 表格；几条、每条多长。
4. **它调什么？** HTTP 地址 + 方法 / 数据库 / 本地文件 / 纯计算。
5. **失败怎么办？** 返回错误文本让模型换路，还是直接失败。

外加一个：**谁能用？** 所有人 / 某个应用 / 只先给自己试。

## 2. 生成目录

```
plugins/<id>/
├── plugin.json
├── main.py
└── README.md
```

`<id>`：小写字母/数字/连字符，≤41 字符，建议带业务前缀（`kb-search`、`drug-label`）。

## 3. manifest（照抄，只改值）

```json
{
  "id": "kb-search",
  "name": "知识库检索",
  "version": "0.1.0",
  "apiVersion": "2",
  "entry": "main.py",
  "description": "检索内部知识库，返回标题、摘要和链接。",
  "capabilities": ["tools"],
  "permissions": {
    "filesystem": "none",
    "network": true,
    "process": false,
    "secrets": ["KB_API_KEY"]
  },
  "config": {
    "type": "object",
    "properties": {
      "baseUrl": { "type": "string", "default": "https://kb.internal", "description": "接口根地址" },
      "apiKey":  { "type": "string", "secret": true, "env": "KB_API_KEY", "description": "访问令牌" }
    },
    "required": ["baseUrl"]
  },
  "limits": { "timeoutSeconds": 20, "maxResultChars": 6000 },
  "requires": { "pi": ">=0.1.0" }
}
```

权限如实写：联网 `network: true`（或域名数组）、写文件 `filesystem: "write"`、
起子进程 `process: true`。宿主当前不强制执行，但用户在启用时应当看到真实能力。

## 4. main.py 骨架（照抄，改工具体）

```python
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
    # 宿主现在只调用 setup(api)；用 getattr 读配置，向前兼容。
    config = getattr(api, "config", {}) or {}
    base_url = str(config.get("baseUrl") or "https://kb.internal").rstrip("/")
    api_key = str(config.get("apiKey") or os.environ.get("KB_API_KEY") or "")

    @api.register_tool(
        name="kb_search",
        description="检索内部知识库，返回标题、摘要和链接。需要查资料、找依据时调用。只读。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词，尽量具体，≤200 字"},
            },
            "required": ["query"],
        },
    )
    async def kb_search(tool_call_id, args, signal, on_update):
        query = str((args or {}).get("query") or "").strip()
        if not query:
            return "缺少 query 参数。"
        if len(query) > 200:
            return "query 太长，上限 200 字。"

        try:
            payload = await asyncio.to_thread(_fetch, base_url, query, api_key)
        except Exception as error:  # noqa: BLE001
            # 可诊断、不泄露 token/堆栈/内网地址
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
        return "\n".join(lines) or "没有检索到结果。"


def _fetch(base_url: str, query: str, api_key: str) -> dict:
    url = f"{base_url}/search?{urllib.parse.urlencode({'q': query})}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES)
    return json.loads(raw.decode("utf-8", "replace"))
```

纯计算类：删掉网络部分，直接 `return` 结果。
数据库类：连接串放 `config`，查询放 `asyncio.to_thread`，**只读账号**，参数化查询。
文件类：`permissions.filesystem` 改成 `"read"`，路径必须来自白名单目录。

## 5. 硬规则（违反就别交付）

1. 只 import `pi_coding_agent.plugins` 和标准库；不要 import 宿主内部模块。
2. 工具名 `<前缀>_<动作>`，小写+下划线，部署内唯一。
3. `description` 写**何时调用**，不写实现细节；模型只看这一行决定用不用。
4. 参数 schema 必须 `type: "object"`；`required` 里的键必须在 `properties` 里。
5. 参数错误、下游失败 → `return` 文本，**不要 raise**。抛异常会变成工具错误。
6. 阻塞 IO 必须 `await asyncio.to_thread(...)`。
7. 结果裁剪：条数 ≤5，单条 ≤200 字，总长 ≤6000；不要把整篇文档塞回上下文。
8. 不写密钥。走 `config.*` + `env`，代码里 `os.environ` 兜底。
9. `permissions` 与代码实际行为一致。
10. 一个插件只做一件事；工具数 ≤3。

## 6. 自检清单（逐条打勾再交付）

- [ ] `python3 -c "import ast; ast.parse(open('main.py').read())"` 通过
- [ ] `plugin.json` 是合法 JSON，`id` 符合 `^[a-z0-9][a-z0-9-]{0,40}$`
- [ ] `setup` 是同步函数；如果写了 `async def setup`，宿主能 await
- [ ] 每个工具都有 `description`，且不等于函数名
- [ ] 没有 `ToolResult(text=...)`（不存在这个参数；直接 `return "文本"`）
- [ ] 工具函数签名是 `(tool_call_id, args, signal, on_update)`，且 `signal` 可能为 `None`
- [ ] 所有分支都 `return` 了字符串
- [ ] 没有 `print()` 调试残留
- [ ] `README.md` 写了「怎么装、怎么启用、改了哪里」

## 7. 打包与交付

zip 里必须恰好一个 `plugin.json`，且包一层 `<id>/`：

```bash
python3 - <<'PY'
import zipfile, pathlib
plugin = pathlib.Path("plugins/kb-search")
with zipfile.ZipFile("kb-search.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(plugin.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            z.write(p, f"kb-search/{p.relative_to(plugin).as_posix()}")
print("ok")
PY
```

交付话术：

> 插件已生成在 `plugins/kb-search/`，打包为 `kb-search.zip`。
> 在「插件」页点「安装插件」上传，然后在目标应用上启用；下一条消息模型就能看到
> `kb_search` 工具。需要令牌的话，先在部署环境里设置 `KB_API_KEY`。
> 如果加载失败，把「插件」页上的错误发我。

## 8. 在本仓库 / 有 pikit 时

优先用确定性工具验收，不要自己"目测通过"：

```bash
python3 plugin-kit/pikit.py check plugins/kb-search
python3 plugin-kit/pikit.py dev   plugins/kb-search --tool kb_search --args '{"query": "乳腺癌"}'
python3 plugin-kit/pikit.py pack  plugins/kb-search
```

`check` 必须 0 error；`dev` 必须真的返回一段非空文本。把两段输出贴给用户。

## 9. 不要做的事

- 不要改宿主代码来"让插件跑通"；
- 不要为了让示例好看而吞掉错误（`except: return "ok"` 是最坏的写法）；
- 不要生成需要额外 `pip install` 的插件，除非用户确认运行环境里已有该包；
- 不要一次做 5 个工具；先做一个，跑通，再加。
