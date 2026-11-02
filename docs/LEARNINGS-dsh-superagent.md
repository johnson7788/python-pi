# 从 dsh-superagent 原型能学到什么

> 阅读对象：python-pi。原文仓库：`/Users/admin/git/dsh-superagent-prototype`（约 1.1 万行 TS，9 篇编号文档，59 个 spec）。
> 约定：`src/`、`tests/`、`docs/`、`prompts/`、`cordis.patch.yml` 均指该原型仓库；提到 python-pi 时写全路径或包名。
> 结论先行：它最值得学的不是功能清单，而是**把"自主 agent"变成可运营系统的那层约束**——
> 授权可追溯、副作用可恢复、消息有归属、状态只有一个说法、人只在必须时被打扰。

---

## 0. 它是什么

一个装在 DSH 上的**垂直 agent 产品**：监听一个 GitHub 仓库（可选飞书），
按 Policy 决定"谁可以让它干活"，把授权过的请求变成 Ticket，
每个 Ticket 在**独立 Session + 独立目录**里执行，结果回到原会话（PR / 评论 / 飞书回复），
Web 面板只显示"需要人处理的事"。

它的形态本身就值得学：`cordis.patch.yml` + 一个 host 插件 + 一个 client 面板插件 + 两个 Source 插件
+ agent preset + locale + prompts/skills + 文档 + 测试，就是一个完整产品（`package.json` 的 `dsh.bundle`）。

---

## 1. 七个最值得学的设计

### 1.1 可持久化的副作用队列：命令只改库，副作用提交后再做

**做法**：所有"要做但可能失败"的外部动作（评估投递、启动 worker、投递输入、转发报告、发通知、发消息）
先写进 SQLite 的 `effect` 表，**在同一个事务里**；事务提交后才真正执行。每类 effect 有自己的
并发上限、是否保序、退避区间、是否上报健康。失败按 `2s` 起、翻倍到 `60s/300s` 重试；
"暂时做不了"抛 `Later`，**不计失败次数**，只是推迟。

**代码**：`src/app/effects.ts`（约 160 行，整个机制的精华）、`src/core/engine.ts` 的 `repo.atomic(...)`。

三个细节尤其值得抄：

- `enqueue` 用 `ON CONFLICT(kind, key) DO UPDATE`，**同一 kind/key 只保留一行**，重复入队合并 payload 且保持队列位置；
- 行上带 `generation`，运行中的旧尝试结束时不覆盖新 payload（`WHERE seq=? AND generation=?`）；
- 关停时**不删正在跑的 row**，下次启动继续重试；`drain()` 只等已启动的。

**为什么好**：python-pi 现在的排队消息是内存 `PendingMessageQueue`，worker 空闲回收即丢；
而这类"进程内队列 + 尽力而为"是自主 agent 最典型的隐性数据丢失点。

**对 python-pi**：`pi_server/db.py` 已经有 append-only migration 和 SQLite，加一张 `effect` 表即可。
先接三类：`relay`（把 worker 结果送回主会话）、`notice`、`reply`（IM/Webhook 出站）。
这比补十个工具更能提升"敢让它跑长任务"的信心。

---

### 1.2 消息所有权：delivered ≠ read ≠ handled

**做法**：一条人类消息进入 Ticket 后，状态被拆成四个可区分的事实：

| 事实 | 含义 | 谁记录 |
|---|---|---|
| `inputsDelivered` | 消息已放进 worker Session 的收件箱 | Executor |
| `inputsRead` | Session 已把它**取进某一步**（历史里出现 `user/message`） | Executor |
| `turnEnded` / `report` | 这一步之后有过报告或回合结束 | Executor |
| `handback` | worker 判定"超出本 Ticket 范围"，退回主会话重新评估 | Executor |

Ticket 结束（或崩溃重启）时，**只有"未读且未确认、未回复、未退回"的消息**才回到 Eval；
已读但没动作的（比如一句"谢谢"）不回。重启后"被 cancel 截断、还没进历史"的消息算未读，会重新投递。

**代码**：`src/core/assignments.ts`、`src/core/tickets.ts`、`src/core/model.ts` 的 `reached`/`followupOwners`，
文档 `docs/002-authorization.md` 的 "Handing back and releasing follow-ups"。

**为什么好**：多 agent 系统里"这条消息到底谁负责"是最容易扯皮的地方。它用**可重放的事实**回答，
而不是用状态机猜测。at-least-once 投递要成立，必须有这套 read/ack 语义。

**对 python-pi**：现在只有 `steering / follow-up` 队列，没有"已读/未处理"的概念。
做 subagent 之前先把这个语义定下来，否则子会话重启一定重复执行。

---

### 1.3 两层准入：确定性 Gate + 模型 Assessment + observe 影子模式

**做法**：

- **Gate（确定性）**：谁能发起、在哪个 scope、消息是否 @ 了 agent、是否 bot，产出一个 `strength`
  （`explicit` / `inferred` / `not admitted`）和 `basis`。纯代码，可单测。
- **Assessment（模型）**：对每条启用的规则**恰好判断一次**（`matched` + 理由），再给出
  `direction`（是否真的要求实现、目标是否已定、是否还缺设计决策）和 `intent_evidence`。
- **Accept mode**：`off` / `observe`（默认）/ `auto`。`observe` 下即使评估通过，也只记一条
  `would_accept` 的 ignore，**什么都不做**。团队看几天记录再切 `auto`。

**代码**：`src/policy.ts` 的 `assessmentOutcome`、`src/admission.ts`、`src/core/evals.ts`。

**为什么好**：这是"给 agent 放权"最稳妥的路径——先让它**说它会做什么**，再让它做。
同时把"LLM 判断"关进结构化笼子：规则全覆盖、方向三要素、证据必填，缺一项就不许 accept。

**对 python-pi**：你的 `policy/rules.py` 正好是 Gate 层；缺的是 Assessment 层和 observe 模式。
如果要做"主动 agent"（定时巡检、自动修 CI），先上 observe。

---

### 1.4 Mandate：把"授权"做成不可变对象，用 Policy 版本重校验

**做法**：accept 时生成一个 `Mandate`，记录 **Policy 版本、scope、strength、命中的规则、发送者在 Source 的身份**。
它是不可变的。只有三种事会改变它：人 stop（撤销）、Policy 变更导致重校验失败（撤销）、新 accept（新建）。
Ticket 结束**不会**结束 Mandate——人再回一句就能在**同一授权**下恢复，不需要重新评估。

Policy 变更时：running 的 Eval 直接作废重排；受影响的 live Ticket 收到一条 "policy changed" 通知，
但**不会**立刻吊销；等它下次恢复前再 revalidate。

**代码**：`src/core/model.ts` 的 `Mandate`/`MandateBasis`、`src/policy.ts` 的 `revalidate`、`policyEffects`。

**为什么好**：大多数系统把"权限"做成会话级布尔，规则一改就不知道在跑的活还合不合法。
这里把"为什么可以干"做成一等公民，审计和长任务续跑都自然成立。

**对 python-pi**：团队模式下每个用户/会话可以带一个"授权凭据"（模型、工具白名单、配额版本），
规则变更时发一条"策略已变"事件而不是直接杀会话。

---

### 1.5 单一 writer + 角色化接口 + 命令/事实命名

**做法**：

- 领域状态**只有一个写者**：Trigger/Thread/Decision/Ticket/Assignment 只能由 `Engine` 写；
  Source 只能写自己的读位和 Thread meta；设置只能由 `Settings` 写；回执只能由 `Outbox` 写。
  `HealthBoard` 甚至拒绝第二次 claim 同名健康项。
- 调用方拿到的不是整个 Engine，而是 `Pick<Engine, ...>`：`EngineForSources` /
  `EngineForMainSession` / `EngineForPeople` / `EngineForSettings` / `EngineForExecutor`。
- **命令用祈使句**（`stopTicket`），**适配器上报的事实用过去式**（`assignmentStarted`、`deliveryFailed`）。

**代码**：`src/core/engine.ts` 末尾的 5 个 `Pick` 类型、`docs/007-architecture.md` 的 "One writer per state" 表。

**为什么好**：这等于用类型系统实现了一套"谁能改什么"的架构约束，不需要 DI 框架。
python-pi 的 `AgentSession` 现在是"什么都能改"的上帝对象，加能力时会越来越难。

**对 python-pi**：先拆出 `SessionForWeb` / `SessionForTools` / `SessionForExtensions` 这类窄接口，
比引入一个完整 DI 容器便宜得多。

---

### 1.6 Stage 表：一个纯函数决定所有状态展示

**做法**：Ticket 只有四种 stage：`person`（等人工，细分 delivery/answer/review/blocked/failed）、
`working`（含 queued）、`idle`、`ended`。判定顺序写死在一个二十多行的纯函数里，**第一行命中即返回**；
面板、健康、重试按钮、通知全部读它。

**代码**：`src/core/stage.ts`；文档 `docs/003-execution.md` 的表格与函数逐行对应。

**为什么好**：状态散落是 UI 与业务永远对不上的根因。它把"这个工单现在卡在谁身上"
变成**一个可测的纯函数**，而不是七八个布尔量的组合。

**对 python-pi**：`SessionInfo` 现在有状态但没有"等谁"的语义。加一列 `stage`（person/working/idle/ended）
就能让侧栏、用量页、审计用同一套口径。

---

### 1.7 Prompt 的版本化覆盖：base 哈希 + "默认已更新"

**做法**：

- 团队可改三段（Communication / Ticket scope / Delivery requirements）、加 skills、覆盖任意内置段落；
- 每个覆盖记 `base`：它是从哪一版内置文本改的（文本哈希）；
- 升级后若内置默认变了，面板标 **Default updated**，并给出"旧默认 / 新默认 / 团队文本"三者 diff，
  让团队选"保留我的"还是"用新默认"；
- 每次保存是一个**不可变版本**（revision + predecessor + 时间 + 来源），会话可以查"我当时用的是哪一版"；
- 模板占位符（`{variables}` `{version}` `{mode}` `{rules}`）校验，缺失拒绝保存；
- Policy 段落**每次请求重新生成、且放在最后**，这样策略变化不会让前面的 prompt 缓存失效。

**代码**：`src/app/prompts.ts`、`src/app/versions.ts`、`docs/004-prompts.md`。

**为什么好**：这是"团队可定制"和"升级不冲突"的少见正解。大多数产品只会二选一：
要么不让改，要么一改就永远追不上新默认。

**对 python-pi**：团队模式完全用得上——把系统提示词拆成"内置段 + 团队段 + 生成段"，
团队段可改并记 base，升级时提示。顺带解决你现在"提示词硬编码在一个字符串里"的问题。

---

## 2. 次一级但很值钱的九个点

| # | 做法 | 代码/文档 | 对 python-pi 的用法 |
|---|---|---|---|
| 2.1 | **worker 槽位**：并发上限之外**永远多留 1 个槽给人类**；人插队优先于新任务；后台 job/subagent 未结束就继续占槽，最多 30 分钟；`ready/blocked/一问` 交还人工时立即释放槽；交还时 job 可留、subagent 必杀 | `docs/003-execution.md` "Background work and worker slots"、`src/executor/dsh.ts` 的 `WorkerSlots` | 你的 Docker sandbox 也应该有"人类优先的额外 slot"，否则同事排队等机器 |
| 2.2 | **健康板**：每个组件 claim 一个 health 条目，禁止重名；**连续 3 次失败**才报 error；`evals` 只在"最近 1 小时未解决且没有更新决定"时报错；`status` 脚本用退出码区分 **0 正常 / 1 重启可修 / 2 组件错 / 3 等配置** | `src/infra/health.ts`、`src/app/views/overview.ts`、`scripts/dsh-superagent` | 直接把 `/api/health` 升级成多条目 + 退出码，值班的人不用读日志 |
| 2.3 | **评估结果结构化**：每条启用规则必须有一条 `rule_assessments`（含 `matched=false` 的理由）；accept 还必须有 `intent_evidence` 与 `direction`；`directionUnsettled`/`noRuleMatched` 自动降级为 ignore | `src/policy.ts`、`prompts/admission.md` | 任何"LLM 当裁判"的地方都该这么约束，否则理由永远事后编 |
| 2.4 | **工具面裁剪**：worker preset 去掉 `ask_user_question` / `present` / `plugin_manager` / 计划 / 调度 / 网页抓取；前台命令 5 秒转后台，job wait 上限 5 秒；prompt 明说"不要 sleep、不要 poll" | `cordis.patch.yml`、`prompts/dispatch.md` | 你的"worker"不该有问用户的工具；长命令必须转后台，否则 HTTP 流会被拖死 |
| 2.5 | **幂等键贯穿**：trigger id 去重、accept 按 trigger id 幂等、消息 id 由 `requestId` 派生（`dsh-superagent:<requestId>`）、effect 按 `kind+key` 合并、重试沿用首次文本 | `src/core/intake.ts`、`src/app/delivery.ts` | 你的 RPC 重试、Web 断线重连、gRPC 式重投都需要这层 |
| 2.6 | **面板三问**：打开只回答"现在要我吗 / 系统健康吗 / 这件事的来龙去脉"；tab 按领域分（Overview/Source/Trigger/Eval/Execution/Settings）；一个概念只出现在一个地方；健康时不占空间；数字必须能点进等长列表 | `docs/005-panel.md`、`src/client/marks.ts` | 你的 Web UI 缺的正是这层"运营视图"，不是更多聊天功能 |
| 2.7 | **SETUP.md 是给 agent 的安装协议**：先跑一次 probe、一次只问一个问题、每题带推荐答案、禁止边问边做、收齐后给一份计划再执行、**永不在聊天里要密钥**（给操作员在自己终端跑的命令） | `SETUP.md` | 团队内推广 python-pi 时，这份文件比 10 页 README 有用 |
| 2.8 | **文档方法**：编号 000-008，每篇只依赖前面；**"Rejected concepts" 表**写明刻意不做的概念及原因；术语表双语；架构文档当代码索引（每个模块一句话）；加新 Source 有 cookbook 11 步 + checklist | `docs/001-domain.md`、`docs/007-architecture.md`、`docs/008-cookbook.md` | 直接解决你 `DEVIATIONS.md` 过期的问题：把"刻意不做"和"已做"分开维护 |
| 2.9 | **测试用内存仓储镜像 SQLite**：`MemoryRepositories` 复制 Map + 快照回滚，保持与 Store 相同的排序语义；engine 测试用假时钟 + 假 assessor/executor + `pump()` 推进世界 | `src/core/memory.ts`、`tests/core-engine.spec.ts` | 你已经有 616 个测试；把"领域规则"从"HTTP/DB 集成"里拆出来会更稳 |

---

## 3. 它踩过的坑 / 不要照抄

- **研究原型，不是产品**：README 自己写明可能有 bug、风格不一致；`docs/007` 末尾专门列了
  "GitHub/Feishu 硬编码进通用模块"的债务清单（Outbox、links、resources、cards、Runtime、Setup 都中招）。
  抄模式，不要抄它的耦合。
- **领域概念偏多**：Source / Thread / Trigger / Correlation / Channel / Policy / Eval / Mandate /
  Ticket / Assignment / Subscription 共 11 个。它是给"多来源、多工单、多执行器"准备的；
  python-pi 先借 **3 个**就够：Trigger（幂等事件）、Mandate（授权）、Ticket（有 stage 的工作单元）。
- **Prompt 极长**：`prompts/dispatch.md` 59 行、`work.md` 49 行、`admission.md` 68 行，全是硬规则。
  这是给强模型 + 固定领域的写法，弱模型会漂。python-pi 若做通用平台，应把规则**下沉到代码/工具校验**，
  prompt 只留"为什么"。
- **单仓库 / 单进程 / 单 SQLite**：进程锁 + 单库，明确不支持多租户横向扩展。
  python-pi 已经有多租户、配额、审计，这块不要回退。
- **依赖 DSH 的 `ctx.jobs`/`ctx.subagents`/session 事件**：它大量用 DSH 内部服务。
  Python 侧要先有自己的 seam，否则会变成"照着 TypeScript 抄一套半成品"。

---

## 4. 落到 python-pi 的最小路线

### P0：让"长任务"可信（约 1.5–2 周）

| # | 事项 | 依据 | 工时 |
|---|---|---|---|
| 1 | `effect` 表 + 提交后执行 + 每类限流/退避/`Later` | §1.1 | 4–5 d |
| 2 | 会话状态加 `stage`（person/working/idle/ended）+ 侧栏/审计统一口径 | §1.6 | 2 d |
| 3 | 健康板（多条目、3 连失败、单写者）+ `status` 退出码 0/1/2/3 | §2.2 | 2–3 d |
| 4 | RPC/WS 幂等：prompt id → message id，重连不重复投递 | §2.5 | 2 d |

### P1：让"多 agent"有地基（约 3–4 周）

| # | 事项 | 依据 | 工时 |
|---|---|---|---|
| 5 | 消息所有权语义：delivered / read / turnEnded / handback | §1.2 | 5–7 d |
| 6 | 窄接口拆分：`SessionForWeb` / `SessionForTools` / `SessionForExtensions` | §1.5 | 3–5 d |
| 7 | Gate + Assessment 两层 + `observe` 模式（先用于定时巡检/自动修复） | §1.3 | 5–7 d |
| 8 | 授权对象（Mandate）+ 规则版本 + 恢复前重校验 | §1.4 | 5–7 d |
| 9 | worker 槽位：并发上限 + 人类额外 1 槽 + 后台占位/超时 | §2.1 | 3–5 d |

### P2：产品化（按需）

- 团队可直接改的 Prompt 分区 + base 哈希 + 升级 diff（§1.7，5–7 d）
- 面板改成"三问 + 按域分 tab"，先做 Overview / Execution 两个（§2.6，5–7 d）
- `SETUP.md` 式 agent 安装协议（§2.7，1–2 d，文档为主）
- 加一个 Source（飞书或 GitHub）当第一个主动 agent 场景（按 cookbook 11 步）

---

## 5. 反过来看：要支撑这类产品，harness 需要哪些能力

这个原型恰好是一张"哪些平台能力是承重墙"的清单。它用到的 DSH 能力，几乎逐条对应
[GAP-ANALYSIS-vs-DSH.md](GAP-ANALYSIS-vs-DSH.md) 里 python-pi 缺的东西：

| 原型里的用法 | 依赖的 harness 能力 | python-pi 现状 |
|---|---|---|
| 每个 Ticket 一个 Session + 独立目录 | 会话创建/命名/持久化、per-session cwd | 有会话，但 cwd 固定 per-uid |
| worker 只保留干活工具 | **agent preset**（一个进程多套组合） | 无 preset，只有固定 7 工具 |
| 长命令转后台、结束唤醒 | **jobs** + 会话 inbox 事件 | 无后台 job |
| worker 派子任务 | **subagents** + `waitForChildren`/`drain` | 无 subagent |
| 主会话被报告/候选唤醒 | `steer` / `inject` / 持久 inbox | 有 steer/follow-up，队列在内存 |
| 面板挂在侧栏 | **client plugin + slots + locale** | 固定 SPA，无插件点 |
| 每小时自检（放在操作员 profile） | **schedule** | 无定时任务 |
| 飞书/GitHub 双向 | Source/Channel 抽象（自己写，但靠 CLI 与 session 事件） | 无 IM/GitHub 适配层 |
| 设置、健康、重启 | 管理 HTTP + 健康条目 + 进程生命周期 | 有 REST，健康只有一项 |
| 提示词分段、团队可改 | system-prompt section + skill 注册 | 单字符串 |

**顺序上的教训**：想做"主动 agent / 垂直 agent 产品"，先有 **preset + jobs + subagent + 持久队列**，
再谈具体场景。否则每加一个场景都要在核心循环里打补丁——这正是它用 1.1 万行换来、
而 python-pi 目前还缺的那层地基。

---

## 6. 如果只抄三件事

1. **副作用先入库、提交后再做**（`effects.ts`）。它把"agent 跑一半崩了"从灾难变成重试。
2. **一条消息的"已读/已处理"必须可区分**（`assignments.ts` + `002` 文档）。没有它，多 agent 一定重复干活。
3. **先 observe 再 auto**（`policy.ts` 的 accept mode）。给 agent 放权的正确顺序是"让它说、你看、再让它做"。

一个额外的、更抽象的感受：这个项目的每条设计都在回答同一个问题——
**"这一步花了谁的注意力，值不值？"**
对 python-pi 这种团队平台来说，这比"再补 60 个工具"更接近产品的核心。
