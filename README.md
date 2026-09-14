# python-pi

[`pi`](https://pi.dev/docs/latest) 是一个极简终端 coding agent harness（TypeScript，11 包，约 18 万行）。
本仓库是它的 **Python 移植版**，目标是提供一个可嵌入的 Python agent 框架，并且**与 pi 的会话文件互通**。

- 调研结论与架构设计：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- 逐层深度调研笔记：[`docs/research/`](docs/research/)（pi-ai / pi-agent / coding-agent core / modes & extensions / surrounding packages）

## 当前状态

已实现并可测试（`packages/` 下三个包，对应上游的 npm 包边界）：

| 包 | 对应上游 | 内容 |
|---|---|---|
| `pi-ai` | `@earendil-works/pi-ai` | 消息/内容块/Usage/Model 类型与 JSON 互转、12 种流式事件的 `EventStream`、provider 注册表、Anthropic 与 OpenAI 兼容适配器、faux 测试 provider、工具参数校验、凭据存储、模型目录、成本计算 |
| `pi-agent-core` | `@earendil-works/pi-agent-core` | agent 循环（含 steering / follow-up / 工具批 / 中止语义）、`Agent` 类（状态归约、队列、生命周期） |
| `pi-coding-agent` | `@earendil-works/pi-coding-agent` | JSONL v3 会话树、内置工具（read/write/edit/bash/grep/find/ls）、系统提示词组装、skills、资源发现、settings、`AgentSession` 编排、print/json/rpc 三种模式、扩展协议、会话替换 |
| `pi-server` | *（无对应上游包）* | 浏览器 UI 的服务端：把同一套命令协议架在 WebSocket 上，并托管 React 单页应用 |

**明确不在 v0.1**：终端 TUI、跨语言（TypeScript）扩展兼容、CBOR 协议栈、SQLite 会话后端、telemetry、evals。
理由见架构文档第 3 节。

## 扩展

扩展是普通 Python 模块，暴露 `setup(pi)` 工厂，可以注册工具、命令，或拦截事件：

```python
# <project>/.pi/extensions/guard.py
from pi_coding_agent.extensions import ToolCallEventResult

def setup(pi):
    pi.on("tool_call", guard)

def guard(event, ctx):
    if event.tool_name == "bash" and "rm -rf" in (event.input or {}).get("command", ""):
        return ToolCallEventResult(block=True, reason="refusing to run rm -rf")
```

发现位置与 pi 一致（项目 `.pi/extensions/` → 全局 `<agent_dir>/extensions/`，也支持
`-e/--extension` 与 pip entry point）。**项目本地扩展只在项目被信任后加载**——这是唯一的门控；
和 pi 一样，**没有沙箱**，扩展以你的完整权限运行。

完整设计、事件表与合并语义见 [`docs/EXTENSIONS.md`](docs/EXTENSIONS.md)。

## 运行模式

CLI 有三种模式，都通过 `pi-py` 调用：

```bash
pi-py "解释一下这个仓库"                 # print：把助手文本流式打到 stdout
pi-py --mode json "解释一下这个仓库"      # json：每行一个 JSON 事件
pi-py --mode rpc                         # rpc：在 stdin/stdout 上服务 JSONL 命令协议
```

`--mode rpc` 与 pi 同构，可以被 IDE 或其他进程当作 pi 的替换件驱动：

```bash
echo '{"id":"1","type":"get_state"}' | pi-py --mode rpc
echo '{"id":"2","type":"prompt","message":"列出当前目录"}' | pi-py --mode rpc
```

协议要点：严格 **LF** 分帧（不能按 Unicode 分隔符切分）、命令可带 `id` 且响应原样回带、
`prompt` 在 **preflight** 就响应（语义是"已接受/已排队"）、`message_update` 只发增量但保留累计
`usage`。30 个命令可用，其中包含会话替换的 `new_session` / `switch_session` / `fork` / `clone`
（由 `AgentSessionRuntime` 提供）。完整的命令与限制清单见 [`docs/DEVIATIONS.md`](docs/DEVIATIONS.md)。

## 浏览器 UI

首次准备：把 API key 放进 `.env`（模板见 `.env.example`，该文件已被 gitignore）。

```bash
cp .env.example .env
$EDITOR .env                     # 填 PI_API_KEY
```

一键启动：

```bash
./start.sh                      # agent 的工作目录 = 当前目录
./start.sh ~/git/my-project     # 指定工作目录
PORT=9000 ./start.sh            # 换端口
```

然后打开 <http://127.0.0.1:8787>。

`start.sh` 会：读 `.env` → 前端没构建过就构建一次 → 检查端口占用 → 启动服务。
没有 key 时它会直接告诉你怎么办，而不是启动到一个必然失败的进程。

### 模型配置

**1) key 放 `.env`**

```bash
cp .env.example .env      # 然后填入 PI_API_KEY
chmod 600 .env
```

`.env` 已在 `.gitignore` 里忽略，不会被提交（`.env.example` 是模板，会被提交）。
`start.sh` 读取它的顺序是：

```
命令行环境变量  >  .env  >  start.sh 里的默认值
```

所以临时换一把 key 不用改文件：

```bash
PI_API_KEY=sk-xxx ./start.sh      # 这一把覆盖 .env
```

`.env` 里也可以覆盖 `MODEL` / `HOST` / `PORT` / `PI_AGENT_DIR`（默认值见 `.env.example`）。

**2) 默认模型与端点**

默认用**阿里云百炼（DashScope）** 的 `deepseek/deepseek-v4-flash`。百炼是 OpenAI 兼容端点，
而 DeepSeek 官方域名不认百炼的 key（实测返回 401），所以地址在 `~/.pi/agent/models.json` 里覆盖：

```jsonc
{
  "providers": {
    "deepseek": {
      "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "models": [{
        "id": "deepseek-v4-flash",
        "cost": { "input": 0, "output": 0 },
        // 百炼接受的 reasoning_effort 与 DeepSeek 官方不同：它收
        // low/medium/high/xhigh/max，但拒绝 minimal。内置目录用的是官方元数据，
        // 会把 medium 和 xhigh 隐藏掉，所以按百炼的真实能力覆盖一遍。
        // 不要给 "off" 映射 null —— 那会让 off 从可选级别里消失；留空即可，
        // off 的语义是"不发送这个字段"。
        "thinkingLevelMap": {
          "minimal": null,
          "low": "low",
          "medium": "medium",
          "high": "high",
          "xhigh": "xhigh",
          "max": "max"
        }
      }]
    }
  }
}
```

内置目录里的其它 provider：`anthropic` / `openai` / `google` / `mistral` / `openrouter`。
`pi-py --list-models` 会列出全部模型 id，已配好凭据的前面有 `*`。

### 思考级别实测

接线是通的——改级别会真的改变发往模型的 `reasoning_effort`（`off` 表示整个字段不发送）。
但 `deepseek-v4-flash` 是**原生推理模型，任何档位都会输出思考**，而且百炼对各档位的响应差异
基本是噪声。同一道鸡兔同笼题，实测 `reasoning_tokens`：

| 档位 | off（不发送） | low | medium | high | xhigh | max |
|---|---:|---:|---:|---:|---:|---:|
| reasoning_tokens | 190 | 204 | 208 | 188 | **111** | **140** |

非单调——`xhigh`/`max` 反而更少。所以**在这个网关上不要指望用档位控制思考量**。
想要真正的"不思考"，得换模型（百炼上的 Qwen 系列用 `enable_thinking` 开关，本移植版还没实现
这个兼容位）；想要真正可调的思考预算，得换 provider（如 Anthropic 的 thinking budget）。

**3) 不用 `start.sh` 时**

`start.sh` 是把 key 以 `--api-key` 传给进程的，所以 key 只在这一次运行里有效。
如果你要直接跑 `pi-py`（或真实的 `pi`），把 key 导出到环境里即可：

```bash
set -a; source .env; set +a
pi-py --model deepseek/deepseek-v4-flash "你好"
```

### 界面

Markdown 渲染的对话流、实时流式输出、工具调用卡片（含运行中转圈与错误态）、思考块折叠、
斜杠命令自动补全、会话列表与切换/新建/克隆、模型与思考等级选择、扩展弹出的对话框
（**可在流式输出过程中作答**）、断线自动重连。

**它没有另造一套 API**：浏览器说的就是 `--mode rpc` 那套命令协议，只是把传输从 stdio 换成
WebSocket。所以任何为 pi 的 RPC 模式写的客户端，改一下地址就能用；反过来，这里验证过的协议行为
对 stdio 同样成立。**pi 本身没有浏览器 UI**（它只有终端 TUI + RPC/JSON 模式），这层是本移植版
新增的——见 [`docs/DEVIATIONS.md`](docs/DEVIATIONS.md)。

同一进程只有一个会话：多个标签页看到并操作**同一个对话**，`/api/state` 等 REST 端点因此能报告
真实状态。事件广播给所有标签页；需要用户作答的对话框只发给最近活跃的那个标签页。

### 没有 API key 也想看效果

附了一个假模型服务（**固定回放脚本，不看输入**）：

```bash
uv run python examples/demo_model_server.py --port 8791
```

配置方式见 `tests/conftest.py` 里的 `write_agent_config`。注意它只会回那两段固定内容，
不要误以为模型坏了。

## 团队部署（多租户）

同一套服务端也支持公司内网多人共用，用 `--auth-mode team` 打开：

```bash
PI_API_KEY=sk-xxx .venv/bin/python -m pi_server.cli \
  --auth-mode team --data-root /var/lib/pi --model deepseek/deepseek-v4-flash
# 首次启动会创建 superadmin，并把一次性密码打到 stderr
```

| | `single`（默认） | `team` |
|---|---|---|
| 账号 | 无 | 有；管理员建号、可禁用、可重置密码 |
| 会话 | 全进程一个 | 每个用户一个，互不可见 |
| 工作目录 | `--cwd` 指定的目录 | `<data-root>/workspaces/<uid>/` |
| 配置目录 | `--agent-dir` | `<data-root>/agents/<uid>/`，从 `<data-root>/template` 播种 |
| 凭据 | 命令行/环境 | 一把共用 key，按人记账并可设月度上限 |
| 工具调用 | 不干预 | 白名单 + 危险命令闸门 + 审计日志 |
| 页面 | 会话 | 会话 / 文件 / 用量 / 管理（成员、全局用量、审计） |

三个必须知道的约束：

- **单进程是硬约束**：会话状态在进程内存里，必须 `--workers 1`。横向扩展要先换掉注册表。
- **一期没有沙盒**：白名单与审批能防误操作，防不住恶意用户。真正的边界是二期把执行面搬进容器
  （`pi_coding_agent/sandbox/`，目前只有 `local`；设 `PI_SANDBOX_BACKEND=provisioner` 会**拒绝启动**，
  而不是悄悄退化成不隔离）。
- 用户自带的扩展在 team 模式下**不加载**：per-user 的 `extensions/` 等于给每个同事一个以服务进程
  权限运行的入口。

完整安装、systemd 硬化、Caddy、备份与排查见 [`docs/DEPLOY.md`](docs/DEPLOY.md)；
设计取舍见 [`docs/plans/2026-09-14-team-saas-design.md`](docs/plans/2026-09-14-team-saas-design.md)。

部署后自检（20 项，覆盖「是否真的在拦截」）：

```bash
python deploy/smoke.py https://pi.example.com --username admin --password '…'
```

## 快速开始

需要 Python 3.11+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

### 作为库使用

一个**完全离线、可直接运行**的完整示例（真实工具 + 真实会话落盘，但用确定性的 faux provider
替代 LLM，因此不需要网络和 API key）：

```bash
uv run python examples/offline_agent.py
```

下面是各层的最小用法。

```python
import asyncio
from pi_ai import AuthStorage, Context, StreamOptions, UserMessage, get_model, stream_simple

async def main() -> None:
    model = get_model("anthropic", "claude-sonnet-4-5")
    auth = AuthStorage()                      # 读取 ~/.pi/agent/auth.json 与环境变量
    options = StreamOptions(api_key=auth.resolve_api_key(model.provider))
    context = Context(
        system_prompt="You are a helpful assistant.",
        messages=[UserMessage(content="Say hi in five words.", timestamp=0)],
    )

    async for event in stream_simple(model, context, options):
        if event.type == "text_delta":
            print(event.delta, end="", flush=True)
    print()

asyncio.run(main())
```

直接驱动 agent 循环（自带工具）：

```python
from pi_agent import Agent, AgentContext, AgentTool, AgentToolResult
from pi_ai import TextContent

async def echo(tool_call_id, params, signal, on_update) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=f"echoed: {params['value']}")])

tool = AgentTool(
    name="echo",
    description="Echo a value",
    parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
    execute=echo,
)
```

### 运行测试

```bash
uv run pytest -q
```

全部测试离线运行，不访问网络，不需要 API key —— provider 层用 `httpx.MockTransport` 喂入固定的 SSE 字节，
agent 循环用脚本化的 faux stream 驱动。

### 验证强度与限制

本仓库的验证依赖三根支柱：

1. **上游对齐**：从上游源码逐段移植并保留语义，关键行为（事件顺序、截断提示文本、成本公式、
   会话文件形状）逐字对齐而不是"大致相同"。
2. **黄金用例**：例如 agent 循环的 12 事件序列直接取自上游测试的断言；
   会话存储有手工构造的 v3 JSONL 黄金文件，验证读取、上下文重建与再序列化。
3. **真实线格式**：provider 适配器的请求体与 SSE 解析由 MockTransport 断言到字段级。

**已知限制**：本仓库开发环境中没有任何 provider 的真实 API key，因此**没有对真实服务做过在线验证**。
首次接入时请先用一个小 prompt 验证所选 provider 的适配器。

## 设计要点

### 与 pi 互通

会话文件就是 pi 的格式：`<agent_dir>/sessions/--<cwd>--/<ts>_<id>.jsonl`，JSONL，version 3，
`id`/`parentId` 构成树（原地分支）。Python 版能读 pi 写的会话，也能写出 pi 能读的会话，
并会把 v1/v2 文件在写回时迁移到 v3。消息的 JSON 字段名（`stopReason`、`toolCallId`、`cacheRead` …）
逐一对齐。

### 刻意保真的语义

移植时**没有**"Python 化"这些行为，因为它们是可观察契约的一部分：

- `partial` 是所有流式事件共享的同一个可变对象；
- `EventStream` 是 push 队列，完成事件本身仍会投递给迭代方；
- `stream()` 把 setup 失败编码成 `error` 事件，而 `stream_simple()` 会同步抛异常；
- `stop_reason == "length"` 时整批工具调用都不执行；
- 并行工具执行按完成顺序发 `tool_execution_end`，但 toolResult 消息按源顺序；
- `AbortSignal` 是被观察的标志，不是 `CancelledError`；
- `clamp_thinking_level` 方向是"就高不就低"；
- 会话文件在第一条 assistant 消息之前不创建。

完整清单见架构文档第 5 节。

### 依赖极简

运行时只依赖 `httpx`（传输）与 `pyyaml`（frontmatter）。上游用到的 `typebox`、`diff`、
`ignore`、`partial-json`、`chalk`、`proper-lockfile` 等在 Python 侧分别用内联实现、
`difflib`、自写的 ignore 匹配、`json` 容错解析与标准库替代。

## 目录结构

```
packages/pi-ai/src/pi_ai/                 provider 抽象、消息类型、事件流、凭据、模型目录
packages/pi-agent-core/src/pi_agent/      agent 循环与 Agent 类
packages/pi-coding-agent/src/pi_coding_agent/
    session_manager.py                    JSONL v3 会话树
    messages.py                           扩展消息与 convert_to_llm
    tools/                                内置工具
    system_prompt.py / skills.py / resource_loader.py / settings.py
    agent_session.py                      编排层
    modes/                                print / json 模式
tests/                                    按模块划分的测试
docs/research/                            上游逐层调研笔记
```

## 已知取舍

- `grep` / `find` 是纯 Python 实现（不依赖 `ripgrep` / `fd`），`.gitignore` 支持为简化版；
- `edit` 不做上游的模糊匹配兜底之外的全部启发式；
- `read` 的图片输入不做缩放处理；
- 模型目录是可再生数据包 `pi_ai/data/models.json`，只收录了少量主流模型；
- 不实现跨语言的 TS 扩展兼容（同进程的 renderer / `AbortSignal` / 参数原地修改语义无法跨边界存活）。

## 许可

上游 `pi` 为 MIT。本移植版沿用 MIT。
