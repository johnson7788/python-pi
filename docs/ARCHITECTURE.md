# python-pi 架构与移植方案

本文是 `pi` 框架的调研结论与 Python 版（`python-pi`）的架构设计。所有数字与结论均来自对
上游源码的实测（shallow clone `earendil-works/pi` @ `f3c6722`，v0.85.1），逐层调研笔记见
`docs/research/`。

---

## 1. pi 是什么

`pi` 是一个**极简终端 coding harness**：核心保持很小，通过 TypeScript 扩展、skills、prompt
templates、themes、pi packages 向外生长。官方自我描述是 "minimal terminal coding harness"。

它不是一个单体程序，而是一个 11 包的 npm monorepo：

| 包 | npm 名 | 源码 LOC | 作用 |
|---|---|---:|---|
| `packages/ai` | `@earendil-works/pi-ai` | 24,384 | 统一多provider LLM API（OpenAI / Anthropic / Google / …） |
| `packages/agent` | `@earendil-works/pi-agent-core` | 25,305 | agent 运行时：工具调用循环与状态管理 |
| `packages/coding-agent` | `@earendil-works/pi-coding-agent` | 70,052 | 编码助手 CLI：会话、工具、资源、模式、扩展 |
| `packages/tui` | `@earendil-works/pi-tui` | 18,107 | 从零手写的终端差分渲染库 |
| `packages/chord` | `@earendil-works/chord` | 5,822 | 应用组合运行时（services / replicated state / plugins） |
| `packages/protocol` `client` `server` | `@earendil-works/pi-*` | 869 / 1,135 / 1,966 | CBOR 远程会话栈（实验性） |
| `packages/session-backends/sqlite-node` | `…-sqlite-node` | 1,973 + 121 SQL | SQLite 会话后端（可选） |
| `packages/telemetry` | `@earendil-works/pi-telemetry` | 935 | 厂商中立的遥测契约（有 no-op 兜底） |
| `packages/evals` | （私有） | 1,351 | 开发期评测，不进运行时图 |

构建顺序即依赖拓扑：`chord → tui → telemetry → ai → agent → sqlite → protocol → client → server → coding-agent`。

### 调研中最重要的三个结构性发现

1. **裸 agent 循环是自包含的。** `packages/agent/src/{types,agent-loop,agent,stream-fn}.ts` 约
   1.6k LOC，**对 `harness/` 零 import**。`harness/`（约 12k LOC，`lane.ts` 2012、
   `drive/structural.ts` 1222）是一个附加的持久化运行时（会话树 + 操作状态机 + 意图/结算 +
   hooks），不是最小移植的必需品。
2. **`chord` 的完整内核只在实验代码里用。** `coding-agent/src/experimental/**` 之外没有任何
   非实验代码 import chord；只有 `agent/src/harness/context.ts` 依赖它的 `./context`（121 LOC）。
3. **CBOR 远程栈与 `--mode rpc` 是两套协议。** 前者是实验性 devDependency，后者才是稳定的
   stdin/stdout JSONL 集成方式。

---

## 2. 上游关键机制速览

### 2.1 消息模型（`pi-ai`）

没有 `system` role —— system prompt 是 `Context.systemPrompt`，由各适配器自行注入。

- 基础消息：`UserMessage` / `AssistantMessage` / `ToolResultMessage`
- 内容块：`TextContent` / `ImageContent` / `ThinkingContent` / `ToolCall`
- 消息时间戳是**毫秒** Unix；会话 entry 的时间戳是 **ISO 字符串**
- `Usage` 带 token 计数与 `cost`（USD）

### 2.2 流式事件协议（12 种事件）

```
start → updates* → done        （成功）
start → updates* → error       （生成开始后失败）
error                          （setup 失败，无 start）
```

最容易搞错的一点：**`partial` 是所有事件共享的同一个 live `AssistantMessage` 对象，原地修改**，
不是每事件的快照。Python 版必须传同一引用。

### 2.3 agent 循环

外层循环处理 follow-up 重入，内层循环 `while hasMoreToolCalls or pendingMessages` 处理工具批与
steering。steering 只在三个点被 drain：循环开始、`prepareNextTurn` 之后（且仅当 pending 为空）、
`turn_end` 之后。`should_stop_after_turn` 在 `turn_end` 之后、**任何队列轮询之前**执行。

### 2.4 会话文件

JSONL，version 3，通过 `id`/`parentId` 构成**树**（原地分支，不新建文件）。9 种 entry 类型。
持久化有个必须复刻的怪癖：**第一条 assistant 消息落盘之前不创建文件**。

---

## 3. Python 版范围决策

已确认的范围（本仓库 v0.1）：

- **交付形态**：核心框架库 —— 对齐 `pi-ai` + `pi-agent-core` + `pi-coding-agent` 核心，
  可作为 SDK 嵌入，不做 TUI。
- **互通性**：会话文件格式与 pi 本身**互通**（同一 v3 JSONL，可读写
  `~/.pi/agent/sessions/`），消息 JSON 形状逐字段对齐。

明确**不在 v0.1**：

| 项 | 原因 |
|---|---|
| TUI（`pi-tui`，18k LOC） | 独立大工程；且 print/RPC 路径不需要它 |
| 扩展系统执行（jiti / TS 扩展 / pi packages） | 跨语言执行 TS 扩展不可行，Python 侧应另设计插件协议 |
| CBOR protocol/client/server | 实验性，且与 `--mode rpc` 重复 |
| sqlite session backend | 上游无人依赖、无配置入口，纯可选 |
| telemetry / evals | 一个可 no-op，一个不进运行时图 |
| harness 持久化运行时（lanes / checkpoint） | 崩溃恢复场景才需要，核心循环不依赖 |
| OAuth 登录、远程模型目录刷新 | 需要网络与交互，v0.1 走 API key + 内置目录 |

---

## 4. 包映射

```
packages/pi-ai/          →  packages/pi-ai/src/pi_ai/
  src/types.ts                types.py          消息/内容块/Usage/Model + JSON 互转
  src/utils/event-stream.ts   events.py         EventStream + 12 事件联合
  src/models.ts               stream.py         注册表、stream/streamSimple、thinking clamp
  src/api/simple-options.ts   stream.py         DEFAULT_THINKING_BUDGETS
  src/api/<api>.ts            providers/        anthropic_messages / openai_completions / faux
  src/api/transform-messages  transform.py      跨 provider 消息规范化
  src/auth/*                  auth.py           credential store / 解析顺序
  src/models.generated.ts     models.py + data/models.json
  src/utils/validation.ts     tools.py          validate_tool_arguments
                              abort.py          AbortSignal / AbortController
                              cost.py           calculate_cost（逐字复刻）

packages/pi-agent-core/  →  packages/pi-agent-core/src/pi_agent/
  src/types.ts                types.py          AgentTool / AgentContext / AgentLoopConfig / AgentEvent
  src/agent-loop.ts           loop.py           循环本体
  src/agent.ts                agent.py          Agent 类：状态归约、队列、生命周期
  src/stream-fn.ts            stream_fn.py      默认 stream 函数

packages/pi-coding-agent/ →  packages/pi-coding-agent/src/pi_coding_agent/
  src/core/session-manager.ts session_manager.py  JSONL v3 树 + 上下文重建 + 迁移
  src/core/messages.ts        messages.py         扩展消息 + convert_to_llm
  src/core/tools/*            tools/              read/write/edit/bash/grep/find/ls
  src/core/system-prompt.ts   system_prompt.py    系统提示词组装
  src/core/skills.ts          skills.py           SKILL.md 发现与注入
  src/core/resource-loader.ts resource_loader.py  资源发现与信任门控
  src/core/settings-manager.ts settings.py        settings.json 双作用域
  src/core/agent-session.ts   agent_session.py    会话编排（compaction/retry/持久化/运行时开关）
  src/core/agent-session-runtime.ts agent_session_runtime.py 会话替换（new/switch/fork/clone）
  src/core/extensions/*        extensions/         Python 原生扩展协议（types/api/runner/loader/rpc_ui）
  src/modes/print|json|rpc    modes/              CLI 模式（共用 modes/json_event.py 的事件投影）

packages/pi-server/      →  packages/pi-server/src/pi_server/   （无上游对应包）
  （新增）                    app.py              WebSocket 传输 + REST + 托管 SPA
                              cli.py              pi-py-web 入口
web/                     →  Vite + React + TS 单页应用（无上游对应物）
```

---

## 5. 必须保真的语义（易错清单）

这些是本移植中**刻意不"Python 化"**的地方，也是回归测试的重点：

1. **`partial` 共享可变对象**：事件携带同一个 `AssistantMessage`，原地修改；消费者不得保存引用。
2. **`EventStream` 是 push 队列**：完成事件本身仍要投递给迭代方，只有完成*之后*的事件被丢弃；
   `result()` 在首个 `done`/`error` 时即 resolve，不等 `end()`。
3. **两个相反的错误契约**：`stream()` 绝不因 setup 失败抛异常（编码成 `error` 事件），
   而 `stream_simple()` **可以同步抛**（首次配置检测依赖这一点）。
4. **`stop_reason == "length"`**：整批工具调用**都不执行**，全部报错让模型重新发起。
5. **`terminate`**：只有当批次内**每一个**结果都 `terminate is True` 时才终止循环。
6. **并行工具执行**：preflight 严格串行；允许调用一次性并发启动；
   `tool_execution_end` 按**完成顺序**，而 toolResult 消息按**源顺序**。
7. **`before_tool_call` 抛异常**变成 error tool result，**`after_tool_call` 抛异常**则丢弃结果 —— 不同路径。
8. **`AbortSignal` 是被观察的标志**，不是 `CancelledError`：中止时循环合成 "Operation aborted" 的工具结果并正常发事件。
9. **`clampThinkingLevel` 方向是"就高不就低"**：先往更强找，再往更弱找。
10. **`thinkingLevelMap` 区分"键不存在"与"显式 null"**：前者是 provider 默认，后者表示不支持；
    Python 必须用 `in` 判断而非真值判断。
11. **成本分档**基于 `input + cacheRead + cacheWrite`，用严格 `>`，命中最高阈值档；1h 缓存写入按 2× input 计价。
12. **会话文件首条 assistant 消息前不创建**；`appendMessage` 拒绝 compaction/branch summary 角色。
13. **上下文重建规则**：`[最新 compaction] + path[firstKeptEntryId..compactionIdx) + path[compactionIdx+1..]`。
14. **时间戳单位不同**：消息是毫秒整数，entry 是 ISO 字符串。
15. **bash 非零退出码抛异常**（消息里带输出与退出码），而不是静默返回输出。
16. **Agent 的历史只在 `message_end` 增长**；循环工作在消息列表的副本上，Agent 从事件流重建状态。

---

## 6. 与 pi 的互通契约

v0.1 承诺的互通面是**会话文件**：

- 路径：`<agent_dir>/sessions/--<cwd 处理后>--/<ISO 时间戳处理后>_<session_id>.jsonl`
  （去掉前导分隔符，`/`、`\`、`:` 替换为 `-`）
- 首行 header：`{"type":"session","version":3,...}`
- 9 种 entry，字段名与 `undefined` 省略语义严格对齐
- 消息 JSON：`role` / `content` / `usage` / `stopReason` 等 camelCase 字段逐一对齐
- 能读 v1/v2 并在写回时迁移到 v3

**尚未承诺**：`--mode rpc` 的 JSONL 协议（下一阶段）。

---

## 7. 路线与当前进度

按依赖顺序推进，每阶段结束都保持测试全绿：

- **P0 ✅**：`pi-ai` 核心类型 / 事件流 / 注册表 / 工具校验 / 成本 / 中止信号；
  `pi-agent-core` 循环与 `Agent`。
- **P1 ✅**：provider 适配器（Anthropic、OpenAI 兼容）、faux 测试 provider、凭据与模型目录、
  会话存储（JSONL v3 + 迁移）、内置工具（read/write/edit/bash/grep/find/ls）、
  系统提示词、skills、资源发现、settings。
- **P2 ✅**：`AgentSession` 编排（compaction、retry、队列、持久化）+ `--mode print` / `--mode json`
  + `pi-py` CLI 入口。
- **P3 ✅**：`--mode rpc`（与 pi 同构的 JSONL 命令协议，30 个命令）+ 会话替换层
  `AgentSessionRuntime`（解锁 `new_session` / `switch_session` / `fork` / `clone`）。
- **P4 ✅**：Python 原生扩展协议（`extensions/`：types / api / runner / loader / rpc_ui），
  21 个事件、工具与命令注册、扩展 UI 走 RPC 子协议。设计见 [`EXTENSIONS.md`](EXTENSIONS.md)。
- **P5 ✅（替代 TUI）**：浏览器 UI。`packages/pi-server` 把同一套命令协议架在 WebSocket 上并托管
  React 单页应用（`web/`）。上游 pi 没有浏览器 UI，这层是本移植版新增的；选择它而不是移植
  18k 行的终端 TUI，是因为它更易用、可远程访问，且不需要重写差分渲染器。

### 浏览器 UI 的架构

```
浏览器 (React SPA)
   │  WebSocket /api/ws —— 与 --mode rpc 完全相同的命令与事件
   ▼
pi_server.app.AgentServer        ── 一个进程一个共享会话
   │  emit(payload) 注入
   ▼
pi_coding_agent.modes.rpc_mode.RpcMode   ── 30 个命令、事件投影、扩展对话框
   │
   ▼
AgentSessionRuntime / AgentSession / 工具 / 会话
```

关键点是**没有第二套协议**：`RpcMode` 的 stdio 传输被换成一个 `emit` 回调与 `handle_line`，
其余（命令分发、preflight 语义、事件投影、扩展 UI 子协议）原样复用。因此为 pi RPC 写的客户端
改个地址就能连浏览器这层，且这里验证过的协议行为对 stdio 同样成立。

两个实现细节值得记住：

- **出站帧必须用与 stdio 相同的序列化器**（`serialize_json_line`，带 `default=str`）。
  用 `send_json` 会因严格编码而在遇到非纯 JSON 载荷时抛异常；同步的 `emit` 回调无法把这个异常
  传回去，发送任务会静默死掉，表现为"流到一半卡住"。
- **事件广播、对话框只发给一个标签页**。同一个 `confirm` 在三个标签页里同时弹出、再等最先作答的
  那个，比只问一次更糟；id 唯一，所以过期应答会被忽略。


### RPC 模式（`--mode rpc`）

stdin/stdout 上的 **严格 LF JSONL**：命令一行一条 JSON，响应与事件一行一条 JSON。

- **帧层**：只按 `\n` 切分，容忍尾随 `\r`。**不能**用 Python 的 `str.splitlines()` 或
  Node 的 `readline`——它们会把字符串内部的 U+2028/U+2029 当成换行，从而把一条记录撕成两半。
  实现见 `modes/jsonl.py`（原始字节读取 + 显式 UTF-8 解码）。
- **相关性**：命令可带 `id`，响应原样回带。
- **响应**：`{"type":"response","command":...,"success":true|false,"data"|"error"}`。
- **`prompt` 在 preflight 就响应**，语义是"已接受/已排队/已立即处理"，之后的失败通过事件流报告。
- **事件**与 JSON 模式共用同一投影（`modes/json_event.py`）：`message_update` 只发增量，
  但保留累计 `usage`，并在 `toolcall_start` 注入 `id`/`toolName`。
- 坏行产生 `command:"parse"` 的错误响应而**不会**终止会话；未知命令类型同样返回错误响应。

已支持 30 个命令（prompt / steer / follow_up / abort / clear_queue /
new_session / switch_session / fork / clone / get_state / get_messages /
set_model / cycle_model / get_available_models / thinking 三件套 / 队列模式两件套 /
compact / set_auto_compaction / set_auto_retry / abort_retry / get_session_stats /
get_entries / get_tree / get_last_assistant_text / get_fork_messages / set_session_name / get_commands）。
仍未支持的 3 个（`export_html` / `bash` / `abort_bash`）会返回**明确说明原因**的错误响应，
而不是静默挂起。完整清单与理由见 `DEVIATIONS.md`。

### 会话替换层（`AgentSessionRuntime`）

`AgentSession` 一旦建立就绑定在某个会话文件上——它的订阅、transcript 与 cwd 相关服务都指向那个会话。
所以"换会话"不是修改它，而是**重建**它。`agent_session_runtime.py` 负责这件事：

- `new_session()`：新建空会话（保持原有存储模式：持久化的仍持久化，内存的仍不落盘）。
- `switch_session(path)`：接管已有的会话文件，并从该文件重建 transcript。
- `fork(entry_id, position)`：把 root→entry 的路径复制进**新文件**（`position="before"` 需要锚点是
  用户消息，返回其文本以便预填编辑器；`position="at"` 保留该 entry）；`clone()` 等价于在叶子处 fork。
- `create_branched_session()` 会**丢弃 label entry 但重新链接父子关系**，并翻译 compaction 的
  `firstKeptEntryId`——因为 label 本身是真实树节点，后面的 entry 可能是它的子节点。

最容易被忽略的一点：替换后 `runtime.session` 是**另一个对象**。任何持有旧会话的东西（事件订阅、
UI 绑定）都在一个死对象上，必须通过 `set_rebind_session()` 回调重新挂载；RPC 模式内部就是这么做的。



### 验证方式

- 单元与集成测试离线运行，不需要网络与 API key。
- provider 适配器用 `httpx.MockTransport` 喂入固定 SSE 字节，断言到请求体字段级。
- agent 循环的黄金事件序列直接取自上游测试的断言。
- 会话存储有手工构造的 v3 JSONL 黄金文件；字节保真度曾用 node 的
  `JSON.stringify(JSON.parse(line))` 交叉验证。
- **CLI 端到端**：`tests/test_cli_end_to_end.py` 启动一个本地假 OpenAI 兼容服务（回环 HTTP），
  以子进程运行真实 `pi-py`，完整走通"加载配置 → 解析模型 → 读取凭据 → SSE 流式响应 →
  装配流式工具调用 → 执行真实工具 → 落盘 v3 会话来"；同一文件里还有一组通过真实管道
  驱动 `--mode rpc` 的子进程测试（命令/响应相关性、事件流、坏行恢复、EOF 退出）。
- **未做**：对真实 provider 的在线验证（开发环境没有任何 API key）。接入新 provider 时建议先用小 prompt 试跑。


---

## 8. 已知取舍

- **不做跨语言扩展兼容**：`ExtensionAPI` 的 renderer 返回组件、同进程 `AbortSignal`、
  `onUpdate` 与参数原地修改等语义无法跨 RPC 边界存活。Python 侧应设计原生插件协议。
- **grep/find 用纯 Python 实现**，不依赖 `ripgrep` / `fd` 二进制，因此语义是"接近"而非逐字节相同
  （`.gitignore` 支持为简化实现）。
- **edit 不做模糊匹配兜底**（上游有 `normalizeForFuzzyMatch`）。
- **模型目录是数据包**：上游 `src/providers/data/*.json` 不在快照中，目录由脚本生成。
  Python 版把它当作可再生的数据文件 `pi_ai/data/models.json`，不硬编码在代码里。
- **不移植 `compat.ts`**：它是上游明确计划删除的弃用 shim。
