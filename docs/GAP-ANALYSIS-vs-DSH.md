# python-pi ← deepseek-harness 功能差距分析

> 基准日：2026-10-30。只读对比，未改动任何代码。
> A = `/Users/admin/git/deepseek-harness`，B = 本仓库。
> 所有结论都带可核验的文件路径；DSH 文档里的 experimental 能力已单独标注。

---

## 0. 一页结论

**B 不是 A 的子集。** 在多租户、配额、审计、Docker 沙箱隔离、pi 会话格式互通这五件事上，B 已经领先 A。
真正的差距集中在三条主线，外加一个结构性问题：

1. **模型手上的能力面**：B 有 7 个工具（默认只激活 4 个），A 有 **71 个模型可见工具 / 35 个工具包**。
   缺的不是数量，而是"联网、MCP、LSP、后台任务、作业控制、todo/plan、子 agent"——
   这些让 agent 从"会改代码"变成"能独立干活"。
2. **多 agent 编排**：A 有 subagent（6 个 provider：spawn/fork 进程内 + dsh-sdk/ACP/Claude Code/Codex 进程外）、workflow（JS 脚本编排）、agent teams、ralph 四套。
   > **实施进展（2026-10-30）**：B 已落地 `spawn` / `fork` 两个进程内后端（`docs/SUBAGENTS.md`），
   > 仍缺控制面（`send_message` / `list_agents` / `interrupt_agent`）、进程外后端、workflow 与 agent team。
3. **平台外壳**：A 有可治理的插件生态（profile / bundle / patch / market）、面板化 Web GUI
   （59 个 `ui-*` 客户端包）、ACP / SDK / 桌面 / TUI、OTel / telemetry；
   B 是"单页会话 + 自带多租户"。
4. **结构性问题**：A 的每个能力都是一个 `ctx.*` service seam + provider，B 直接在 `AgentSession`
   里接线。结果是：B 加第 8 个文件工具很便宜，加 MCP / LSP / subagent 很贵——要先补缝。

另有两组**与 A 无关、但比缺功能更急**的问题：

- **可用性缺陷**：内置模型目录 24 个模型里有 **12 个选中即报错**（见 §2.10）；
- **安全默认形态**（不是缺功能），见 §2.5。

### 差距等级速查

| 等级 | 含义 | 代表项 |
|---|---|---|
| 🔴 结构性缺失 | 整块能力为零 | 多 agent、MCP、LSP、后台作业、插件治理 |
| 🟠 能力偏薄 | 有雏形但覆盖不足 | 工具面、会话检索、审批粒度、附件 |
| 🟡 体验缺口 | 不影响正确性但影响可用性 | diff 审阅、设置页、i18n、终端面板 |
| 🟢 B 领先 | 不要照抄 A | 多租户、配额、审计、Docker 沙箱、pi 互通 |

---

## 1. 规模基线

| 维度 | A：deepseek-harness | B：python-pi |
|---|---|---|
| 代码量 | 632,088 行 TS/TSX（8,866 文件，不含 node_modules/dist） | 25,314 行 Python + 6,665 行前端（web/src，含 CSS） |
| 包结构 | 58 个顶层包 + 5 个 app，单一能力一个包 | 4 个包 |
| 模型可见工具 | **71 个名字 / 35 个工具包**（`docs/tool-catalog.md`） | **7 个**，默认激活 **4 个**（`tools/__init__.py:38-41`） |
| 会话格式 | 自有事件日志 v4（v0→v4 迁移链，`packages/session/session-format*`） | pi 的 JSONL v3 树（互通是资产，`session_manager.py`） |
| 客户端 | 59 个 `ui-*` 包 + slots/client-modules 动态组合（`packages/client/`） | 25 个文件、6.6k 行固定 SPA（`web/src/`） |
| 多语言 | UI 8 语（`packages/client/*/locale/{de,en,es,fr,ja,pt,ru,zh}.json`），文档 2,102 个 `*.zh.md` | 全中文硬编码 |
| 扩展模型 | npm 包 + `dsh.*` 元数据 + profile/patch 层 + host/client 双端插件 | Python 模块 `setup(pi)` + 21 个事件 + pip entry point |
| 协议 | ACP（编辑器）、SDK stdio JSON-RPC、headless `--json`、web、TUI、Electron 桌面 | print / json / rpc（27 命令）+ WebSocket + 15 个 REST |
| 测试 | 各包自带 tests + benchmarks 场景 | 616 个 pytest，全离线确定性 |
| 可观测 | OTel、product telemetry、session telemetry、feedback、benchmarks | 用量/成本、审计日志、`/api/health` |

---

## 2. 差距清单

### 2.1 工具面：7 → 71（最高优先级）

A 的完整名单由生成器从真实启动的插件里读出，见 `docs/tool-catalog.md`（35 行表格 + 每包 JSON Schema）。
B 的清单见 `packages/pi-coding-agent/src/pi_coding_agent/tools/__init__.py`。

| 工具族 | A 提供 | B 现状 | 影响 |
|---|---|---|---|
| 文件读写 | `read` `write` `edit` `apply_patch` `str_replace_editor` `read_image` | `read` `write` `edit` | 🟠 无多文件补丁；`read_image` 受模型能力门控；图片只内联 Base64，无降采样/持久附件（`tools/read.py:93-104`） |
| 搜索发现 | `glob` `grep`（打包 ripgrep 二进制） | `grep` `find` `ls`，**默认关闭**，纯 Python 遍历 | 🟠 大仓库慢一个量级；默认只开 4 个工具意味着模型连搜索都没有 |
| 进程执行 | `bash` `pwsh` + 持久 PTY `bash`/`pwsh` + `terminal_*`（6 个） | `bash`（一次性，schema 仅 `command`/`timeout`） | 🔴 无后台执行、无交互式终端、无长驻 shell |
| 作业控制 | `job_list` `job_output` `job_kill` | 无 | 🔴 长命令只能阻塞等，Web 上会拖死请求 |
| 人机交互 | `ask_user_question` `present` `propose_action` | 无（只有扩展弹窗，模型无法主动问） | 🔴 模型不能确认需求、不能交付文件卡片 |
| 计划/任务 | `todo_write` `exit_plan_mode` `create_goal` `get_goal` `update_goal` | 无 | 🔴 长任务不可见、不可控 |
| 日程 | `schedule_create` `schedule_edit` `schedule_list` `schedule_view` | 无 | 🟡 团队"定时汇报/巡检"做不了 |
| 外部知识 | `web_search` `web_fetch`（provider seam） | 无（只能 bash+curl） | 🔴 无联网能力，knowledge 类任务全靠人工 |
| 协议接入 | MCP：`list_mcp_resources` `read_mcp_resource` `list_mcp_resource_templates` + 每 server 动态工具 | 无 | 🔴 无法接任何 MCP 生态 |
| 代码智能 | `lsp`（4 种操作，provider 可插） | 无 | 🟠 只能 grep，没有定义/引用/诊断 |
| 会话检索 | `session_search` `session_trace` `session_event_read/search/trace` | 无 | 🟠 历史只靠翻列表 |
| 多 agent | `subagent` `subagent_fork` `list_subagent_models` `send_message` `interrupt_agent` `list_agents` `spawn_teammate` `wait_agent` `team_task_*` `ralph` `workflow` `run_code`(PTC) | 无 | 🔴 见 §2.2 |
| 工作区 | `working_directory` `create_worktree` `load_workspace_dependencies` | 固定 per-uid cwd | 🟠 不能按项目切、不能一任务一 worktree |
| 生态自省 | `plugin_manager` `plugin_market` `cordis_inspect_*` | 无 | 🟡 自举/自动装插件做不了 |
| 浏览器/桌面 | `stagehand_*`（browser-use，experimental）、computer-use | 无 | 🟡 可选 |

> **一个容易被忽略的细节**：不只是"少几十个工具"。`AgentSession` 的默认工具集是
> `DEFAULT_ACTIVE_TOOL_NAMES = (read, bash, edit, write)`（`agent_session.py:426`），
> `cli.py` 与 `pi_server` **都没有覆盖它**，唯一的开启入口是扩展里的 `set_active_tools`。
> 也就是说，团队 Web 上模型**看不到 `grep`/`find`/`ls`**——每天都会影响体验，但不在任何
> 需求列表里。这一条改一行就能修。

**工具执行管线层面的差距**（比"少几个工具"更不容易补）：

| 管线能力 | A | B |
|---|---|---|
| 结果溢出（spill） | `ctx.spillStore` 落盘 + 可回读定位符（`docs/subsystems/spill.md`） | bash 把完整输出写临时文件（`tools/bash.py:268`）；`grep`/`find` 到 `DEFAULT_MAX_BYTES` 就**截断且不落盘**（`tools/grep.py:171-185`） |
| 工具级超时 | `@deepseek-ai/dsh-tool-call-timeout-policy`（合作式取消 + 结构化超时错误） | 只有 bash 的 `timeout` 参数 |
| 重复调用防护 | `@deepseek-ai/dsh-repeat-tool-reminder`（3/5/8 次提醒换思路） | 无 |
| 工具级沙箱 | 每次调用解析 file-effect policy，bash 走 `ctx.sandbox`，文件工具走 `ctx.fs` 根 | **只有 bash 走 backend**；文件工具在服务进程直接读写（`tools/__init__.py:59` 注释自认） |
| 工具权限声明 | 每个工具声明只读/写/网络等能力，审批据此触发 | 正则匹配命令字符串 |
| 工具结果 UI | 专用卡片（bash-sample / read-row / search-row / file-mutation-row） | 一律 JSON `<pre>` |

> **结论**：工具面的差距不是"再写 60 个函数"，而是缺 4 个可复用的横切能力：
> 输出溢出、超时/循环治理、工具级策略、结果渲染协议。这四件事做完，后面每加一个工具都是小成本。

> **实施进展（2026-10-30）**：下文表格记录的是对比时的状态。`spawn` / `fork` 两个进程内后端
> 已在本仓库落地并通过端到端验证，见 [`SUBAGENTS.md`](SUBAGENTS.md)；控制面与进程外后端仍未实现。

### 2.2 多 agent 与编排：B 为 0

| 能力 | A | B |
|---|---|---|
| 子会话 | `packages/subagent`：`spawn`/`fork`（进程内）+ `dsh-sdk`/`acp`/`claude-code`/`codex`（进程外），共 6 个 provider；`SubagentStartRequest` 有 cwd、preset、outputSchema、depthLimit | 无 |
| 控制面 | `send_message` / `interrupt_agent` / `list_agents`，子会话可续聊，父会话收到完成通知 | 无 |
| 脚本编排 | `workflow`：JS 脚本 fan-out（`pipeline`/`parallel`/`phase`），最多数十个子 agent | 无 |
| 团队 | `experimental/tool-agent-team`：9 个工具，共享任务 DAG、peer inbox | 无 |
| 循环 | `ralph`：固定工作流，每轮一个全新结构化子会话 | 无 |

对 B 的意义：这是一个**多用户团队平台**。用户最需要的不是"再快一点的文件搜索"，而是
"把一个大调研拆给 5 个子 agent 并行跑，我只看汇总"。这也是 token 花费和墙钟时间的最大杠杆。

### 2.3 上下文与外部世界

| 能力 | A | B | 差距 |
|---|---|---|---|
| MCP client | `packages/mcp/mcp-client`：stdio/HTTP、重连、超时、资源、server instructions | 无 | 🔴 |
| LSP | `packages/lsp`：stdio provider，4 种导航操作，结构化 `LSP_UNAVAILABLE` | 无 | 🟠 |
| Web | `ctx.web` seam + `tool-web`，provider 可换 | 无 | 🔴 |
| 附件 | `AttachmentStore`：持久图片、校验、按模型能力 gate、token 计费（`docs/subsystems/attachment.md`） | 无上传/粘贴；协议有 `ImageBlock` 但渲染落到 `block-unknown` | 🟠 |
| 跨会话引用 | `session-reference`：把另一个会话的消息作为上下文引用 | 无 | 🟡 |
| Skills | provider 注册表（本地/内置/远程）+ `skill` 工具按需加载 + badge/office 等打包 skill | `SKILL.md` 发现 + 校验 + 注入 system prompt + `/skill` 展开（`skills.py`） | 🟢 单文件场景已等价，缺多来源与市场 |
| 系统提示词 | 分 section、带 order、协作式组装（`docs/subsystems/system-prompt.md`） | 单一字符串拼接（`system_prompt.py`） | 🟠 影响缓存命中与可扩展性 |

### 2.4 会话与运行时

| 能力 | A | B | 差距 |
|---|---|---|---|
| 持久化 | JSONL + flush barrier + 撕裂尾部修复（`docs/subsystems/persistence.md`） | 逐条 append，无 fsync（`session_manager.py:1393`） | 🟠 |
| 崩溃恢复 | checkpoint policy：请求前/工具副作用前 flush，中断 turn 补 closers | 无 | 🔴 崩溃后工具副作用可能重放 |
| 队列持久化 | durable inbox（事件投影） | 内存 `PendingMessageQueue`（`agent.py:154`）；worker 空闲回收即丢 | 🔴 |
| 会话检索 | SQLite FTS + trace + lineage + 窗口读（`packages/session-query`） | `get_messages`/`get_tree`/`get_entries`；列表全量解析（`app.py:1141`） | 🔴 |
| 自动标题 | 折叠 + LLM 生成 + 用户 pin（`docs/subsystems/session-title.md`） | 仅手工 `set_session_name`，且 Web 无入口 | 🟠 |
| 投影/缓存 | `session-projection` + cache | 每次读全文（`session_manager.py:1002`） | 🟠 会话多了会卡 |
| working dir | `working-directory` 工具 + durable 变更 | 固定 per-uid | 🟠 |
| worktree | `create_worktree`（experimental） | 无 | 🟡 |
| 压缩 | 区域压缩 + tool-result pruner + image offload + 锁括号 | 阈值摘要 + keep_recent（`compaction.py`） | 🟡 |
| 协议 | ACP、SDK JSON-RPC、headless `--json` | print/json/rpc + WS | 🟠 |

> **不要误判**：B 的 JSONL v3 树、fork/clone、v1→v3 迁移、steering/follow-up、abort、
> auto-retry、auto-compaction、`get_session_stats` 都已实现且测试充分。
> `docs/DEVIATIONS.md` 里"未实现 RPC 模式/无扩展系统"两条已过期（见 §6）。

### 2.5 权限与安全：差距 + 一组独立风险

A 的三层结构：

| 层 | A | B |
|---|---|---|
| 审批契约 | `ctx.approval`：闭合四值 `allowed-once/rejected/cancelled/unavailable`，**fail-closed**；`ask`/`never` 策略；`approval/asked`+`decided` 成对审计（`docs/subsystems/approval.md`） | `policy/extension.py:45-63` 布尔 + 超时拒绝；无"一次性"语义、无永久允许 |
| 权限预设 | `sandbox` + `approval` 双旋钮打包成预设（`workspace-write`/`danger-full-access`/`custom`/`auto`），客户端一个下拉（`docs/subsystems/permission-presets.md`） | 进程级 env，无 per-session 切换 |
| 沙箱 | 真内核隔离：Linux bwrap/Landlock、macOS Seatbelt、Windows ACL restricted token；per-call file-effect policy；`full`/`partial` 如实上报（`docs/subsystems/sandbox.md`） | `local`（进程内）或 Docker provisioner（仅 bash） |

**B 的安全短板（与是否对标 A 无关，按可利用性排序）**：

1. **正则分类器可被解释器一行绕过**（高）。`policy/rules.py:41-71` 只看命令字符串：
   `python3 -c "import os;print(os.listdir(chr(46)*2))"` 不含 `/` 也不含 `..` → ALLOW。
   在 `local` 后端 + 多租户下，可读 `../<其他 uid>/sessions/*.jsonl`，零弹窗。
2. **默认形态是 `local`**（高）。`PI_SANDBOX_BACKEND` 默认 `local`；`app.py:834` 只在有 database
   （团队模式）时注入 policy 扩展。单机/误配置下 agent 就是宿主权限。
3. **write/edit 不审批、无读前写**（中高）。工作区内写入从不询问，可覆写 `AGENTS.md`、
   `settings.json`、未读过的源码；A 有 `fs-observation-policy`（`FS_NOT_OBSERVED`/`STALE_VERSION`），B 无。
4. **审批粒度粗**（中）。只有 bash 会问、摘要截 400 字符、无"始终允许" → 点击疲劳后
   容易被 `git push`/`chmod` 蒙过。
5. **无出口管控**（中）。容器默认 `network=bridge`，`local` 全出网；只拦 `nc -l` 与 `curl|sh`，
   `urllib` 即可外带工作区内容。

**B 已经做得好的地方**（别推翻）：fail-closed 审批、未知工具默认拒绝、`protected_roots` 挡跨用户读取、
`realpath` 解软链、团队模式禁用户扩展、容器 `cap_drop ALL`/`no-new-privileges`/pids+内存限额/
无 docker socket/单 bind mount/挂载探针、cwd 越界拒绝、子进程密钥清洗、scrypt + 服务端会话。

### 2.6 扩展与生态

| 能力 | A | B |
|---|---|---|
| 可治理单元 | npm 包 + `dsh.*` 元数据 + profile 清单 + patch 层 + `engines.dsh` 版本约束 + 回滚 | `setup(pi)` 模块 + pip entry point + 目录发现 + 项目信任门 |
| 安装/启停 | `plugin_manager`（装/卸/启停/更新/注册表回退/build 脚本审批）+ `plugin_market` | 无（pip 装完即生效，无法按会话启停） |
| 服务缝 | 762 行 `docs/capability-seams.md`：`ctx.llm/tools/slots/...` 可整体替换 | `ctx` 只有 session/ui/policy；`register_provider`（`api.py:283`）**无消费方**，等于空缝 |
| 事件 | `llm/stream` waterfall、`cordis/*` 等 | 21 个事件（`extensions/types.py:35`），不发 provider/compact 事件 |
| UI 插件 | client modules + React slots，Web 界面本身可插 | 固定 SPA，除 dialog 外无插件点 |
| Hooks | Claude Code hooks / Codex hooks 协议兼容（`packages/hooks/*`） | 无 |
| Agent presets | 一个进程多套 agent 组合（`docs/subsystems/` + `packages/preset`） | 一套组合 |

三个结构性短板：**没有可治理单元 → 多租户无法按用户/团队选能力集；没有服务缝 → MCP/LSP/subagent
只能写进核心；扩展止于无头单进程 → 团队平台最需要的"并行委派 + 可插界面"两条都缺。**

### 2.7 Web UI

B 的**流式会话核心是完整的**：Markdown+DOMPurify、thinking 折叠、工具卡流式结果、斜杠命令弹层、
模型/思考级别切换、steer/follow-up、fork、token/费用统计、扩展四型弹窗、Toast/断线重连、
自动压缩、文件页、用量页、管理台、登录/改密、主题跟随系统、900px 移动端抽屉。

缺的是**围绕会话的一整层**（A 对应 59 个 `ui-*` 包）：

| UI 能力 | A | B | 影响 |
|---|---|---|---|
| diff / deliverables 审阅 | `ui-deliverables/FileDiff.tsx`、`PresentedFileCard.tsx` + turn 前后 git 快照 | 无 | 🔴 用户看不到"改了什么"，只能自己翻文件 |
| 图片/附件 | `ui-attachment/ComposerAttachments.tsx` | `Composer.tsx` 无上传/粘贴；`Message.tsx` 把图片块渲染成 JSON | 🔴 |
| plan / todo / goal | `ui-plan/PlanModeControl.tsx`、`todo-row.tsx`、`ui-goal/GoalBar.tsx` | 无 | 🔴 |
| 终端 | `ui-sidebar-terminal/terminal.tsx` | 无 | 🟠 |
| 设置页 | `ui-settings-*` 10 个包 | 无 | 🟠 用户改模型/主题只能改文件 |
| 侧栏 | 搜索/重命名/置顶/归档/分组/多会话并行（`ui-workspace/rows/*`） | 列表 + fork；`useAgent.ts:1097` 有 `setSessionName` 但**没有 UI 入口** | 🟠 |
| 代码高亮 | shiki | `marked` + DOMPurify | 🟡 |
| 文件预览 | code/image/pdf/html/excel | 纯文本 | 🟡 |
| 面板 | jobs / schedule / subagent / workflow-run | 无 | 🟡 |
| 多语言 | 8 语 | 中文硬编码 | 🟡 |
| 快捷键 / @ 引用 / 语音 / 翻译 / LiveShare | 有（后三者 experimental） | 无 | 🟡 |

### 2.8 协议与集成

| 能力 | A | B |
|---|---|---|
| 编辑器集成 | ACP（`dsh acp`，Zed 等） | 无 |
| 嵌入式 SDK | `dsh sdk` stdio JSON-RPC + **Python 客户端 SDK**（`python/sdk`） | 只有 RPC 协议本身，无客户端库 |
| 无人值守 | `dsh headless --json --session-id --permission-mode --add-dir` | `pi-py` print/json 近似，但无 permission/session 参数 |
| Webhook | 认证投递 + 规则 + 自动建会话 | 无 |
| 桌面端 | Electron（`apps/desktop`） | 无 |
| TUI | `dsh tui` profile | 明确不做 |

> B 的 `--mode rpc` 与 pi 同构是**优势**（任何 pi 客户端改地址就能用），不要为了追 A 而改协议；
> 缺的是把已有 RPC 包一层 SDK/ACP，而不是新造协议。

### 2.9 可观测性与反馈

| 能力 | A | B |
|---|---|---|
| 分布式追踪 | OTel（`docs/subsystems/otel.md`） | 无 |
| 产品遥测 | `product-telemetry` / `session-telemetry`（脱敏规则） | 无 |
| 用户反馈 | 消息级 feedback + 版本化 sidecar | 无 |
| 运行诊断 | 诊断包/健康检查（`runtime-diagnostics` 目录为空，勿计） | `/api/health` |
| 用量/成本 | token-meter（不可变重放测量） | 逐消息 usage + 费用 + 配额（**B 更贴团队计费**） |
| 审计 | approval 成对事件 | `audit_records` + admin 页（**B 更完整**） |

### 2.10 模型与推理层（LLM）

| 能力 | A | B | 差距 |
|---|---|---|---|
| wire 协议 | 7 种（anthropic-messages、openai-completions、openai-responses、azure-openai-responses、openai-codex-responses、google-generative-ai、mistral-conversations、bedrock），`packages/llm/llm-pi-ai/src/catalog.ts` | **2 种**（`providers/__init__.py:12` 的 `BUILTIN_APIS`） | 🔴 目录里 12/24 模型（全部 openai-responses、google、mistral）指向未注册 api，选中后 `stream.py:204` 直接返回 `no API implementation` 错误流 |
| provider 覆盖 | 42 个端点/env 映射（`provider-data.generated.ts`） | 6 个 provider / 24 模型（静态 JSON） | 🟠 每加厂商都要改代码 |
| prompt caching | `cacheRetention` + 每协议 `cacheControlFormat`，Anthropic `cache_control` 实际发送 | **完全不发** `cache_control`，只解析返回的 cache 计数 | 🔴 系统提示词+工具定义每轮重复；缓存读约为输入价的 1/10，长会话成本明显更高 |
| 错误分类 | 稳定码 `RATE_LIMIT/QUOTA/TIMEOUT/TRANSPORT/EMPTY_RESPONSE/INVALID_CREDENTIAL`（`packages/llm/llm/src/error.ts`） | `ProviderError(status)` 只有 HTTP 码，上层靠正则匹配 message（`agent_session.py:1062`） | 🟠 无法按失败类别分流 |
| 重试 | 按 provider 策略、jitter、遵守 `Retry-After`（带上限）、重试状态持久化 | 全局指数退避、无 jitter、无 `Retry-After`、状态在内存（`agent_session.py:236`） | 🟠 限流时容易雪崩 |
| 超时 | 流空闲 watchdog（默认 300s）并归类 TIMEOUT | httpx 总超时 600s（`anthropic_messages.py:218`） | 🟡 挂死连接要等 10 分钟 |
| 空响应 | 归类 `EMPTY_RESPONSE` 且默认可重试 | 无检测 | 🟡 偶发空补全静默结束一轮 |
| 上下文压力 | `token-meter`：usage 锚点 + 增量复算 + 按路由图片计价 | `chars/4` 估算 + 最近 usage（`compaction.py:181`） | 🟡 压缩触发偏粗 |
| 压缩 | seam + `compactRegion` + 图片卸载 + 工具结果剪枝 | 阈值摘要 + keep_recent | 🟡 长会话可压缩范围更小 |
| OAuth 登录 | 设备码/OAuth URL 中继（`llm-pi-ai/src/login.ts`） | 只有 `OAuthCredential` 线格式，明确不刷新、无登录 | 🟠 只支持 OAuth 的 provider 接不进来（刻意省略） |
| 目录刷新 | `/models` 探测 + 缓存 | 静态 JSON，无 `refresh()`（刻意省略） | 🟡 新模型要改文件重启 |
| provider 热配置 | 每次请求重解析 profile/凭据，headers/retry/超时可覆盖 | `models.json` 改完要重启；`settings.py` 的 `modelOverrides` 解析后**无人消费** | 🟡 空配置面 |
| 原生能力 | DeepSeek Files API 上传、图像 token 精确计价、listing 缓存 | 全部走 openai-completions | 🟡 大附件只能内联 |

**最该先修的一条**：`get_available()` / `--list-models` 会列出 12 个不可用模型。要么补
`openai-responses`/`google-generative-ai`/`mistral-conversations` 适配器，要么把这些模型从目录/列表里过滤掉。
用户选中后只得到一条 error 事件，是最伤信任的缺陷，修复成本却最低（0.5–1 天）。

> **别误判**：B 的成本核算逐字复刻上游（分层计价、1h cache write 双倍，`cost.py`），
> 服务端还有 per-user 账本与月度配额——这块比 A 强（A 的 pi-ai 路由把 cost 清零）。
> 工具参数校验流水线、非视觉模型降级（`transform.py:80`）、OpenAI compat 矩阵（约 10 个开关）、
> thinking 回放、`auth.json` 互通与每次调用重解析，B 都已具备。

### 2.11 工程与文档

| 项 | A | B |
|---|---|---|
| 文档国际化 | `*.i18n.yaml` + 2,102 个 `*.zh.md` | 中文单语 |
| 生成式目录 | tool / cli / config / cordis catalog 自动生成 + `doc-sync` 校验新鲜度 | 手写，已有 3 处过期（§6） |
| 类型等价校验 | `verify-type-equiv`（文档里的类型声明与源码逐字一致） | 无 |
| 基准场景 | `benchmarks/`（长会话、终端 IO、流式重连等 8 个） | 无 |
| 测试 | 各包测试 + 快照/e2e | 616 个离线 pytest（覆盖扎实，是优势） |

---

## 3. 不建议照抄

| A 的东西 | 为什么 B 不该抄 |
|---|---|
| TUI（`pi-tui` 级别） | 18k 行独立工程，收益被 Web 覆盖 |
| Electron 桌面端 | 团队平台走浏览器即可 |
| Cordis 依赖注入 + 类型级 waterfall + HMR | 实现成本极高，B 只需要"注册表 + 接口"这一层 |
| npm 安装器 / SemVer 豁免 / build 脚本审批 | Python 侧 uv/pip 已解决，B 缺的是清单与白名单，不是安装器 |
| agent-team / ralph / forge / liveshare / 语音 / 翻译 | A 自己也在 experimental，B 应先做确定性的 RPC/审计/计费 |
| 71 个工具全量移植 | 先把"横切四件套"（spill/超时/策略/渲染）做出来，再按用户需求加工具 |

---

## 4. 建议路线

工时按"熟悉本仓库的一名 Python 工程师"估算，含测试。

### P0：让 agent 真正能干活（约 3 周）

| # | 事项 | 工时 | 理由 |
|---|---|---|---|
| 0 | 修模型目录坏链接：过滤或补齐未注册 api 的 12 个模型 | 0.5–1 d | 选中即报错，最低成本、最高信任收益 |
| 1 | `todo_write` + 前端 checklist 渲染 | 2–3 d | 长任务可见、零权限风险，用户感知最强 |
| 2 | 后台作业：`bash run_in_background` + `job_list/output/kill` | 4–5 d | 长命令不再拖死 Web 请求；也是后续 subagent 的基础 |
| 3 | `ask_user_question` | 1–2 d | 复用现成 dialog 通道，模型终于能确认需求 |
| 4 | 输出溢出 + `grep`/`find` 默认开启（可选接 ripgrep） | 3 d | 大输出不再丢；搜索默认可用 |
| 5 | 自动标题 + 侧栏搜索/重命名入口 | 3 d | 会话一多就不可用，现成 RPC 能力只差 UI |
| 6 | 图片附件：上传/粘贴 + `Message` 渲染 + `read_image` | 4 d | Web 用户最直观的"缺一块" |
| 7 | Anthropic `cache_control` + 稳定错误码 | 3–4 d | 直接降成本，且让重试策略有依据 |

### P1：补齐 agent 能力面（约 6 周）

| # | 事项 | 工时 | 理由 |
|---|---|---|---|
| 8 | **先补最小 seam**：`ToolProvider`/`CapabilityProvider` 注册表 + 工具策略声明 | 4–5 d | 否则下面每一项都是硬编码；这是所有 P1 的前置 |
| 9 | 本地 subagent：子会话 + `subagent`/`send_message`/`list_agents`/`interrupt_agent` | 8–10 d | 团队平台最大杠杆 |
| 10 | MCP client（stdio）+ 资源工具 | 5–7 d | 一次接入整个 MCP 生态 |
| 11 | 权限预设 + 审批契约（一次性/永久/never）+ 工具级触发 | 5–7 d | 同时缓解 §2.5 的点击疲劳与粒度问题 |
| 12 | `web_search`/`web_fetch`（provider 可插） | 3–5 d | 知识类任务 |
| 13 | 会话索引 + FTS + 搜索/追踪工具 | 4–5 d | 替掉 `app.py:1141` 的目录扫描 |
| 14 | plan mode（`exit_plan_mode` + 审批 UI） | 3–5 d | 复杂改动的"先看方案" |
| 15 | 按 provider 的重试/超时策略（jitter、`Retry-After`、空闲 watchdog） | 3–4 d | 限流稳定性 |

### P2：平台化（按需）

workflow/agent team（10–15 d）· LSP（5–7 d）· PTY 终端（7–10 d）· schedule/goal（7–10 d）·
ACP/SDK（7–10 d）· OTel（3–5 d）· 插件清单/市场（10 d+）· i18n（5 d）·
桌面端 / TUI / 语音 / 翻译（不建议）

### 安全项（与功能线并行，优先级高于 P1 多数条目）

- 把 `PI_SANDBOX_BACKEND=provisioner` 变成团队模式的**默认**，`local` 需显式 opt-in（1 d）
- 命令分类器降级为"辅助提示"，真正的拦截交给容器/只读挂载；或至少对 `python -c`/`node -e`/
  `bash -c` 强制确认（2 d）
- `write`/`edit` 纳入审批或引入读前写（`FS_NOT_OBSERVED`）（2–3 d）
- 单机模式也注入 policy 扩展（0.5 d）

---

## 5. 怎么验证差距真的补上了

1. **同题对照**：同一批任务（改 bug / 加测试 / 查资料 / 长命令）在 B 与 A 上各跑 5 次，
   记录完成率、工具调用数、token、墙钟。
2. **场景回放**：A 的 `benchmarks/`（长会话、terminal-io、conversation-fold、stream 重连）
   可以直接改造成 B 的 pytest 场景。
3. **能力快照测试**：把 `tools/__init__.py` 的默认工具集与 `docs/tool-catalog.md` 做差集，
   在 CI 里断言"计划内工具已注册"，避免文档与实现再次漂移。

---

## 6. 附：B 仓库里已过时的文档（顺手可修）

| 位置 | 现状 | 应为 |
|---|---|---|
| `docs/DEVIATIONS.md:167-175` | "无扩展系统" | 扩展系统已实现（P4） |
| `docs/DEVIATIONS.md:173` | "未实现 RPC 模式" | RPC 已实现 27 命令 |
| `docs/DEVIATIONS.md:111` 附近 | "服务端不鉴权" | `pi_server/auth.py` 已实现登录/团队鉴权 |
| `README.md` "已知限制" | 只说 provider 未在线验证 | 可补安全默认形态与沙箱边界 |

同时建议在 `DEVIATIONS.md` 增加**安全面**的偏差登记（目前整份文档没有安全条目），
把"默认 local、策略仅团队模式注入、文件工具不经沙箱"写成显式取舍，而不是留在代码注释里。
