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
| 历史思考回传 | 同模型的历史轮次会带上 `thinking`/`reasoning_content` | **不回传**：历史轮次只在带工具调用时保留（工具结果必须与提出调用的推理配对），其余丢弃；只有「续写时的末尾 assistant」保留本轮思考与签名。可观察：请求体变小，模型看不到自己过去的推理 |
| 续写（prefill） | 上游 `continue()` 拒绝 assistant 尾巴 | 支持：末尾 assistant 视为 prefill 发出（Chat Completions 加 `prefix: true` 并走 `/beta/chat/completions`）；空 prefill（无正文且无思考）直接丢弃 |
| 服务端工具与来源展示 | 无 | DeepSeek 扩展：`compat.serverTools` 声明 `deepseek_web_search` / `deepseek_read_url` 后，响应里的 `annotations` / `server_tool_calls` 存进 `AssistantMessage` 并随消息下发到 UI。这些调用在服务端执行过，**不进入本地工具循环** |

### 消息模型

- `AssistantMessage` 新增两个可选字段 `annotations`（引用，带字符偏移）与 `server_tool_calls`（服务端搜索/抓取记录）。
  它们只用于展示，回传时被忽略；会话文件里写为 `annotations` / `serverToolCalls`。
  上游 pi 读到未知键会忽略，因此互通不受影响；反向（用中文档打开上游写出的会话）字段缺失即为"无引用"。

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
- **本移植版新增单轮迭代上限与"守卫"**（上游没有）。`TurnGuards`（`pi_agent/guards.py`）在
  `AgentLoopConfig.turn_guards` 上挂载，默认 `max_turns=20`（库的默认值是 `None`，即上游行为；
  `AgentSession` 开启）。可观察差异：
  - 达到上限时循环正常结束（不抛错），并在 `agent_end` 前不再发起新的一轮；
  - 空回复 / 逐字复读会先注入**一条** `<runtime_event type="...">` 用户消息要求继续，第二次才停止；
  - 连续 3 轮全部工具调用失败时注入一次"换策略"提示，之后再失败即停止；
  - 守卫停止后不投递已排队的 follow-up，避免把"为什么停"藏在下一轮里。


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

### 风控与授权（上游没有）

- 调用除了"允许/确认/拒绝"之外还带一个**风险等级**（read / probe / write / destructive），
  用于审计行与会话授权，不改变原有判定：`DENY` 仍由路径/命令规则一票否决。
- `PI_POLICY_SESSION_GRANTS=1` 时，用户在对话框里批准一次**写**操作后，本会话内同类写操作不再询问；
  可撤销性差的 destructive 操作永远不会被记住，`dd of=/dev/sda` 这类写设备的目标也会被当作越界写。
- 挂了策略的会话只把"策略允许或会询问"的工具交给模型：既不在 `allow_tools` 也不在 `confirm_tools`
  的工具会从工具列表里消失（原来仍会出现在列表里，调用必被拒）。
- 审计行新增 `risk` / `granted_by`（policy / session / confirm / auto）/ `granted_for_session`。

### 可观测性（上游没有）

- `PI_TRACING=1` 时把 span 以 JSONL 写在 `<data_root>/traces/YYYY-MM-DD.jsonl`：
  每轮一个 `turn` span，每次模型回复一个 `llm` span（含 usage/成本），每次工具调用一个 `tool` span。
- 工具参数只记录 key 与白名单键（path/command/pattern/url 等，截断 200 字符），其余记类型；
  含 token/secret/password 的键记 `<redacted>`；结果只记长度与前 200 字符。
- 没有结束的工具调用（中止/崩溃）会以 `incomplete=true` 落盘，而不是消失。
- 默认关闭；tracing 的任何异常都不会影响会话。

### token 估算自校准（上游没有）

- 用相邻两次请求的 provider prompt 增量除以中间新增消息的估算，学习一个标量并夹在 [0.5, 1.5]；
  仅用于决定何时压缩、压缩保留多少。样本阈值 256 token，单样本比值越界丢弃，含图片的样本跳过。
- 可观察：同一会话的压缩触发点会随中文/英文比例变化，但语义不变。

### 上下文压缩（相对上游 v0.1 补全）

上游 TS 版有、本移植版 v0.1 曾标注为 deferral 的机制现已补上。可观察差异：

- **切点预算按上限处理**：一条超出 `keepRecentTokens` 的工具结果不再被"连同后面一起留下"，
  而是在它之前切。边界仍会吸附到合法的切点（不会把工具调用和结果拆开），必要时多留一轮。
- **split-turn**：切点落在某一轮中间时，被丢掉的这一轮开头会被单独摘要一次
  （第二次 LLM 调用），拼成 `## Current turn so far`；失败时降级为同一份摘要。
- **`<read-files>` / `<modified-files>`**：从 read/write/edit 的工具调用里机械提取路径，追加到每份摘要，
  并从上一份摘要继承（上限 50 条）。摘要模型失败时这层信息仍然存在。
- **上一份摘要不再被"再摘要一次"**：作为 `<previous-summary>` 传给摘要模型，且从待摘要消息里排除。
  这会改变摘要请求的 prompt 内容。
- **摘要请求自身会 fit**：超出窗口时丢弃最旧的消息，并在 prompt 里写明"有 N 条更早消息未展示，不要猜"。
- **摘要预算**同时受 `0.8 × reserveTokens` 与 `keepRecentTokens` 限制。
- **腾不出空间就不写回**：压缩后回收不足 5% 时丢弃结果并记录"该消息数下压不动"，
  直到会话继续增长才重试（避免每轮白花一次摘要调用）。
- **溢出修复**：provider 返回上下文超限（识别 20 余种真实报错文本）时压缩一次并继续，
  每轮只允许一次；其它错误仍走原有重试路径。
- **降级 fallback**：摘要调用重试一次仍失败时，用有界的原始归档代替摘要（`degraded=true`
  记录在 compaction entry 的 `details` 里），而不是放弃压缩。

### 失败时的收尾回答

上游在出错时留在会话里的是一条 `stop_reason="error"` 的**空** assistant 消息，已收集的工具结果被丢弃。
本移植版在"守卫停止"（不是 error/aborted）后会再用一次 **无工具** 的模型调用，把已有证据写成最终回答：

- 只有守卫停止才触发（`max_turns` / 空回复 / 复读 / 连续截断 / 工具连续失败）；
- 请求里 `tools=[]`，不会再开第二轮工具循环；
- 合成为空或失败时直接丢弃，不写入会话；
- 合成消息与普通 assistant 消息一样落盘并广播，UI 看到的是一条完整回答而不是空白。

### 流式恢复（WebSocket）

浏览器断线/刷新后可以续上看不懂的半截回答：

- 服务端给每条下行记录一个单调 `seq`，按 session 保留最近 4000 条（`pi_server/runlog.py`）；
- `ready` 增加 `seq` 与 `lastPersistedSeq` 两个字段；
- 客户端在 `?since=<seq>` 里报自己已应用到哪一条，服务端只补发 `max(since, lastPersistedSeq)` 之后的记录；
- 只有"有消息已开始流出但尚未落盘"时才补发，避免把已完成的 `turn_end` / `agent_end` 重放一遍。
- 不落盘：进程重启后的恢复仍靠会话文件 + `get_state`，不维护第二份磁盘事件流。

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

### 浏览器 UI（上游没有）

**pi 没有浏览器 UI。** 它的交互面是终端 TUI，加上给外部程序用的 `--mode rpc` / `--mode json`。
本移植版选择做浏览器 UI 而不是移植 TUI，因此这一层**没有上游对应物**，是本项目新增的设计：

- 服务端 `packages/pi-server`：把同一套 RPC 命令协议架在 WebSocket 上（`/api/ws`），
  另加少量只读 REST（`/api/health`、`/api/models`、`/api/sessions`、`/api/state`），并托管 React SPA。
- 前端 `web/`：Vite + React 18 + TypeScript，依赖只有 `react`、`react-dom`、`marked`、`dompurify`。
- 图片：输入框支持选图 / 粘贴 / 拖入，浏览器先缩到长边 1568 并转 JPEG，再以 inline base64
  经 `prompt.images` 发送。服务端只接受 inline base64（不接受 path / URL），单张 ≤10 MiB、
  一次 ≤8 张；非视觉模型会在 provider 请求前把图片降级为占位文本。`read` 读到的图片不缩放。
- **协议没有分叉**：浏览器说的就是 `--mode rpc` 那套命令与事件，只是换了传输层。
- 协议上有三处**新增**（不在 pi 的 RPC 协议里）：连接建立时服务端主动推送
  `{"type":"ready",version,cwd,warnings}` 与一个不带 `id` 的 `get_state` 响应（便于首屏立即渲染）；
  `/api/sessions` 的 `modified` 用 ISO 字符串而非 epoch 毫秒。客户端都应容忍它们缺失。

部署取舍：服务端**不鉴权**，默认只绑 `127.0.0.1`。要暴露到网络必须自己加认证/反代——
和 pi 一致，这不是一个带权限系统的产品。

### 后台 subagent（上游没有）

上游 pi 只能由用户手动开新会话；本移植版新增了模型可调用的后台委派：`subagent`（spawn）与
`subagent_fork`（fork）两个工具，设计参照 DeepSeek Harness 的 subagent seam，只实现进程内后端。

- 子 agent 是独立 Session：同一 sessions 目录、header `parentSession` 指向父会话；
- fork 的种子截到父"最后一条 user 消息之前"，即只含已完成轮次；
- 完成通知是持久化的 `custom` message（`customType: "subagent"`），父在流式中走 steering，
  空闲时自动续一轮；
- 深度/并发上限快速失败（`SUBAGENT_DEPTH_LIMIT` / `SUBAGENT_CAPACITY`），不排队。

完整语义与限制见 [`SUBAGENTS.md`](SUBAGENTS.md)。

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

**已支持 31 个命令**：`prompt` `steer` `follow_up` `abort` `clear_queue`
`new_session` `switch_session` `fork` `clone` `get_state`
`get_messages` `set_model` `cycle_model` `get_available_models` `set_thinking_level`
`cycle_thinking_level` `get_available_thinking_levels` `set_steering_mode`
`set_follow_up_mode` `compact` `set_auto_compaction` `set_auto_retry` `abort_retry`
`get_session_stats` `get_entries` `get_tree` `get_last_assistant_text` `get_fork_messages`
`get_branch_points` `set_session_name` `get_commands`。

**未支持 3 个**，均返回 `success:false` 并说明原因（而不是静默挂起）：

| 命令 | 原因 |
|---|---|
| `export_html` | HTML 导出未移植 |
| `bash` / `abort_bash` | 用户 `!` 命令执行未移植（agent 的 `bash` 工具不受影响） |

会话替换（`new_session` / `switch_session` / `fork` / `clone`）由
`agent_session_runtime.AgentSessionRuntime` 提供，语义对齐上游：替换会**重建** `AgentSession`
（而不是原地修改），因此 `runtime.session` 在替换后是**另一个对象**，任何持有旧会话的绑定
（事件订阅、UI）都必须重新挂载——RPC 模式内部通过 `set_rebind_session` 自动完成这件事。

**两处协议扩展**（上游没有，纯增量，旧客户端不受影响）：

| 扩展 | 说明 |
|---|---|
| `fork` 的 `position` | 缺省 `before`（上游语义：在用户提问之前分叉，返回该提问文本供预填）；`at` 保留该条目本身，用于「从这条回答分叉」。非法值返回 `success:false` |
| `get_branch_points` | 返回当前路径上**每一条消息**的 `{entryId, role, timestamp, text}`。`get_fork_messages` 只给用户消息（上游的 `/fork` 选择器），而界面要支持「从某条回答分支」，需要 assistant 条目和「消息 → 条目 id」的对应关系 |

两者都是为 web 工作台的「创建分支 / 在新对话中分支」服务的：分支即
`SessionManager.create_branched_session`，新会话文件用 `parentSession` 指回原文件，原会话不受影响。

其他差异：

- `cycle_model` 在"当前可用模型列表"上轮转（上游区分 scoped models）；返回的 `isScoped` 恒为 `false`。
- `extension_ui_request` / `extension_ui_response` 子协议未实现（没有扩展系统）。
- `session_before_switch` / `session_before_fork` / `session_shutdown` 扩展事件不发出，
  因此替换**不可被取消**；返回值仍保持 `{"cancelled": bool}` 形状以便调用方兼容。
- `importFromJsonl` 未实现（上游的会话导入）。
- 会话状态里的 `steeringMode` / `followUpMode` / `autoCompactionEnabled` / `autoRetryEnabled`
  是**进程内**开关（上游同样不落盘），本移植版通过 `AgentSession` 上的运行时开关实现。

---

## pi-server

### 插件清单一致性（对旧清单是破坏性变化）

- `permissions` 里声明的 `network` / `filesystem` / `process` / `secrets` 必须同时出现在 `capabilities`，
  否则插件在加载时被跳过（此前两份声明可以互相矛盾，admin 面板只看到 `tools`）。
- 七个内置插件的清单已按真实权限补齐；`permissions.network` 的主机白名单通过
  `GET /api/plugins` 的 `networkHosts` 暴露，供审批时查看。
- 进程内插件仍然以服务器全权运行：这是声明与审批层面的诚实化，不是隔离。

### 出网地址校验

- 沙箱 provisioner 的 URL 在启动时校验 scheme/凭据/字面 IP，第一次拨号前解析并校验**全部**解析结果；
  默认允许私网、公网需 `PI_SANDBOX_ALLOW_PUBLIC_URL=1`，链路本地（云元数据）永不放过。
- 注入自有 httpx client 的调用方（测试、嵌入方）不在此列：传输层归调用方所有。

## 影响评估

按"使用者是否会观察到"排序：

1. **无 ripgrep 依赖**（正面差异）：部署更简单，但搜索结果的字节级细节与 pi 不同。
2. **未实现提示缓存**：Anthropic/OpenAI 上实际成本会高于 pi，功能不受影响。
3. **未实现 RPC 模式**：若你需要像 pi 那样被 IDE 以子进程方式驱动，暂时不可用。
4. **无扩展系统**：无法复用 pi 生态的 TS 扩展；需要 Python 原生插件协议（路线 P4）。
5. **跨进程锁缺失**：两个进程同时写同一会话文件可能冲突；上游用 `proper-lockfile` 规避。
