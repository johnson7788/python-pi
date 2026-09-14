# 与上游 pi 的偏差登记

本文件逐模块记录 Python 版**刻意偏离**上游 TypeScript 实现的地方。判断标准是：

- 只要偏差是**可观察**的（影响模型看到的输入、会话文件内容、事件序列、退出码），就必须记录；
- 纯粹的 Python 惯用法差异（dataclass 代替 interface、`async def` 代替 Promise）不算偏差。

保真的部分见 `ARCHITECTURE.md` 第 5 节；这里只登记"我们明知不同"的地方。

---

## pi-ai

### provider 适配器

| 项 | 上游 | 本移植版 |
|---|---|---|
| Anthropic OAuth / Claude Code 身份 | 有 `sk-ant-oat` 检测分支，会强制 Claude Code system prompt 并重映射工具名大小写 | 不支持；只走 API key |
| Anthropic beta 头、`cache_control` 提示缓存 | 支持多种 wire 格式 | 未实现（会造成成本上升，但功能正确） |
| Anthropic 自适应 thinking / 会话中途切换 effort | 支持 | 只支持 `enabled(budget_tokens)` / `disabled` |
| OpenAI `thinkingFormat` 矩阵 | 11 种变体 | 一个精简的 detect/compat 开关集：developer vs system role、`max_completion_tokens` vs `max_tokens`、DeepSeek `reasoning_content` |
| 严格 JSON schema / grammar 约束采样 | 有 | 未实现 |
| `transform_messages` 的 `normalizeToolCallId` 钩子 | 有 | 无（保持固定两参数签名），改由各适配器自行净化 id（Anthropic 64 字符集、OpenAI 管道/40 字符 + JS 兼容短哈希） |
| faux provider 计费 | 上游 faux 是 stub，成本恒为 0 | 本移植版**会**按模型费率计算成本，便于离线集成测试 |
| faux 的异步行为 | — | 同步实现，只观察"已经中止"的信号 |

### 模型目录

- 上游 `src/providers/data/*.json` 是生成物、不在快照中，由脚本从各 provider 目录拉取生成。
  本移植版把它当作可再生的数据包 `pi_ai/data/models.json`，只收录 6 个 provider、约 24 个模型，
  **价格取自公开定价**。
- **不做远程目录刷新**（无 `refresh()`），因此也没有 `models-store.json` 缓存。

### 其他

- 不移植 `compat.ts`（上游明确计划删除的弃用 shim）。
- 凭证写盘用原子替换；上游用逐字段写回。并发写保护仅进程内。
- OAuth 登录流程未实现。

---

## pi-agent-core

- **不移植 `harness/`**（约 12k LOC：lanes、checkpoint、deferred response、effect gate、
  持久化 session repo）。裸循环对它零依赖，v0.1 直接省略。
- `EventStream` 增加 `fail()`：上游靠未处理的 Promise rejection，在 Python 里会让
  `await result()` 永久挂起，所以改成显式传播。
- `Agent` 增加同步/异步双支持：所有钩子既接受普通返回值也接受 awaitable（上游类型即 `T | Promise<T>`）。
- 上游 `agentLoop` 在循环抛异常时会泄漏一个永不结束的 stream；本移植版把异常送进 stream。
- 并行工具执行用 `asyncio.gather` 而非 `TaskGroup`（TaskGroup 会在异常时取消兄弟任务，
  而上游的 thunk 契约是各自吞掉异常）。

---

## pi-coding-agent

### 会话存储

- 跨进程文件锁（`proper-lockfile`）未实现，只有进程内 `asyncio.Lock`。
  因此所有写方法都是协程，必须 `await`。
- 迁移在加载时立即重写为 v3（上游行为），而不是"下次追加时"。
- 未知 **entry** 类型原样保留（`UnknownEntry`）；但已知 entry 上的未知多余键在迁移重写时会被丢弃
  （JS 会保留原始对象）。
- 会话 id 用自行实现的 UUIDv7；entry id 用 `uuid4()[:8]` 并做碰撞重试（上游用 `randomUUID()`，形状相同）。
- `json_dumps` 模拟 JS 数字格式化（`0.0` → `0`，非有限值 → `null`）。
  已知残留差异：小于 `1e-4` 的值 Python 用指数形式，JS 在 `1e-6` 以下才用；对 token/成本数值无影响。
- `is_persisted()` 语义是"是否启用持久化"，**不是**"文件是否已存在"——判断文件请查文件系统。

### 内置工具

- `grep` / `find` 是纯 Python 实现，**不依赖 `ripgrep` / `fd` 二进制**，因此：
  - `.gitignore` 匹配是简化版（最后一个匹配生效，无全局 excludes，无"被排除目录内的重新包含"）；
  - 结果排序以保证确定性（上游依赖 rg/fd 的输出顺序）。
- `edit` 的模糊匹配启发式是子集。
- `read` 增加了上游没有的**二进制文件保护**；图片直接转发、不缩放（无图像库依赖）。
  **注意**：`read` 的文本输出已按上游对齐——返回原始文本，不带行号，截断提示逐字符一致。
- `bash` 省略 `spawnHook` / `commandPrefix` / powershell 支持；完整输出临时文件在结束时一次性写入
  （上游流式写入）。
- 上游内置工具的 `operations` 可插拔注入钩子未移植。

### 资源与设置

- 无效的 skill 名称/描述会被**跳过并记诊断**；上游只警告但仍加载。
- 扩展只做**发现**（一层深度，`foo.ts`/`foo.py` 或 `foo/index.ts`/`foo/index.py`），不执行；
  无 npm/git 包解析、无 theme 加载、无 `.gitignore` 过滤、无 git worktree 影子上下文处理。
- settings 省略字段迁移（`queueMode`/`websockets`/`skills` 对象形态）与文件锁；
  用原子替换 + 单个 `asyncio.Lock` 代替写队列。
- `trust.json` 为单进程语义。
- 系统提示词中指向文档/示例的路径从包目录推导，支持 `PI_PACKAGE_DIR` 覆盖。
- `bashTimeoutMs` 上游无对应项，作为可选覆盖、默认 `None`。

### 未实现

- 终端 TUI（`pi-tui`，18k 行差分渲染器）、themes、keybindings —— 见下方"浏览器 UI"一节
- 会话 HTML 导出
- 图片处理（缩放、非视觉模型降级提示）

### 浏览器 UI（上游没有）

**pi 没有浏览器 UI。** 它的交互面是终端 TUI，加上给外部程序用的 `--mode rpc` / `--mode json`。
本移植版选择做浏览器 UI 而不是移植 TUI，因此这一层**没有上游对应物**，是本项目新增的设计：

- 服务端 `packages/pi-server`：把同一套 RPC 命令协议架在 WebSocket 上（`/api/ws`），
  另加少量只读 REST（`/api/health`、`/api/models`、`/api/sessions`、`/api/state`），并托管 React SPA。
- 前端 `web/`：Vite + React 18 + TypeScript，依赖只有 `react`、`react-dom`、`marked`、`dompurify`。
- **协议没有分叉**：浏览器说的就是 `--mode rpc` 那套命令与事件，只是换了传输层。
- 协议上有三处**新增**（不在 pi 的 RPC 协议里）：连接建立时服务端主动推送
  `{"type":"ready",version,cwd,warnings}` 与一个不带 `id` 的 `get_state` 响应（便于首屏立即渲染）；
  `/api/sessions` 的 `modified` 用 ISO 字符串而非 epoch 毫秒。客户端都应容忍它们缺失。

部署取舍：服务端**不鉴权**，默认只绑 `127.0.0.1`。要暴露到网络必须自己加认证/反代——
和 pi 一致，这不是一个带权限系统的产品。

### 扩展系统

Python 原生扩展协议已实现，**详细设计、事件表、合并语义与差异清单见
[`EXTENSIONS.md`](EXTENSIONS.md)**。要点：

- **不追求跨语言兼容**。pi 的扩展是 TypeScript，由 `jiti` 同进程加载，并返回活的 TUI 组件、
  依赖同进程 `AbortSignal` 与参数原地修改——这些语义无法跨语言边界存活。Python 侧是重新设计，
  但沿用相同的事件名、注册方法与合并语义，让 pi 的文档与示例仍然可读。
- **不做沙箱**，与 pi 一致：扩展以进程的完整权限运行。保留 pi 唯一的门控——项目本地扩展
  只在项目被信任后加载。
- 发出 **21 个事件**；pi 的 TUI 专属事件（`ui_prompt_*`、`user_bash`）与需要下沉到 provider 层的
  事件（`before_provider_request` 等）暂不发出。`EMITTED_EVENTS` 是权威来源。
- 不提供 TUI 渲染器注册（`registerMessageRenderer` / `registerShortcut` / `setWidget` 等）；
  `ctx.ui` 只保留无头可用的部分，在 RPC 模式下走 pi 的 `extension_ui_request` 子协议。
- 包管理用 Python 生态的 entry point（组 `pi_coding_agent.extensions`），不做安装器。

### RPC 模式（`--mode rpc`）

协议本身按上游实现（严格 LF JSONL、`id` 相关性、preflight 时机的 `prompt` 响应、
`message_update` 只发增量但保留累计 `usage`、坏行返回 `parse` 错误而不终止会话）。

**已支持 30 个命令**：`prompt` `steer` `follow_up` `abort` `clear_queue`
`new_session` `switch_session` `fork` `clone` `get_state`
`get_messages` `set_model` `cycle_model` `get_available_models` `set_thinking_level`
`cycle_thinking_level` `get_available_thinking_levels` `set_steering_mode`
`set_follow_up_mode` `compact` `set_auto_compaction` `set_auto_retry` `abort_retry`
`get_session_stats` `get_entries` `get_tree` `get_last_assistant_text` `get_fork_messages`
`set_session_name` `get_commands`。

**未支持 3 个**，均返回 `success:false` 并说明原因（而不是静默挂起）：

| 命令 | 原因 |
|---|---|
| `export_html` | HTML 导出未移植 |
| `bash` / `abort_bash` | 用户 `!` 命令执行未移植（agent 的 `bash` 工具不受影响） |

会话替换（`new_session` / `switch_session` / `fork` / `clone`）由
`agent_session_runtime.AgentSessionRuntime` 提供，语义对齐上游：替换会**重建** `AgentSession`
（而不是原地修改），因此 `runtime.session` 在替换后是**另一个对象**，任何持有旧会话的绑定
（事件订阅、UI）都必须重新挂载——RPC 模式内部通过 `set_rebind_session` 自动完成这件事。

其他差异：

- `cycle_model` 在"当前可用模型列表"上轮转（上游区分 scoped models）；返回的 `isScoped` 恒为 `false`。
- `extension_ui_request` / `extension_ui_response` 子协议未实现（没有扩展系统）。
- `session_before_switch` / `session_before_fork` / `session_shutdown` 扩展事件不发出，
  因此替换**不可被取消**；返回值仍保持 `{"cancelled": bool}` 形状以便调用方兼容。
- `importFromJsonl` 未实现（上游的会话导入）。
- 会话状态里的 `steeringMode` / `followUpMode` / `autoCompactionEnabled` / `autoRetryEnabled`
  是**进程内**开关（上游同样不落盘），本移植版通过 `AgentSession` 上的运行时开关实现。

---

## 影响评估

按"使用者是否会观察到"排序：

1. **无 ripgrep 依赖**（正面差异）：部署更简单，但搜索结果的字节级细节与 pi 不同。
2. **未实现提示缓存**：Anthropic/OpenAI 上实际成本会高于 pi，功能不受影响。
3. **未实现 RPC 模式**：若你需要像 pi 那样被 IDE 以子进程方式驱动，暂时不可用。
4. **无扩展系统**：无法复用 pi 生态的 TS 扩展；需要 Python 原生插件协议（路线 P4）。
5. **跨进程锁缺失**：两个进程同时写同一会话文件可能冲突；上游用 `proper-lockfile` 规避。
