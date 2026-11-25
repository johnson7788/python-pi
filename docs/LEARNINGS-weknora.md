# 从 WeKnora 学什么：pi-agent 可借鉴清单

> 基线：`Tencent/WeKnora` `main@71c32af`（v0.9.1，2026-11-24）对比本仓库当前工作区。
> 全部结论来自源码阅读，行号可核对。本文只谈**能落到 pi-agent 的机制**，不评价 WeKnora 的产品功能。

---

## 0. 一句话结论

WeKnora 值得抄的不是"知识库产品"，而是**长任务 agent 的工程机制**：上下文压缩的完整闭环、失败后的收尾、可恢复的流式、统一权限判定、以及"每个不变量一个测试文件"的纪律。

但两个项目不是同类，所以要先立三条准则：

1. **只抄有对应失败面的机制。** WeKnora 的 4264 行 `modelcontext` 是为了让**知识库 chunk** 可引用；pi-agent 今天没有检索面，整体移植只会多一层编解码。有检索面之后再抄（见 §4.1、§4.2）。
2. **抄机制，不抄体量。** WeKnora 有 1935 个测试文件、138 对迁移、19 个服务的 compose，这些是 450k 行服务的产物。pi-agent 3.4 万行，抄它们等于自杀。
3. **能自己量出来再调。** WeKnora 的压缩参数（保留 16k、最少腾出 5%）是拿生产流量调的。pi-agent 先补测量，再决定阈值。

**差距一句话**：pi-agent 的**骨架**（循环、会话树、subagent、插件、策略）比 WeKnora 干净；pi-agent 的**长跑与运维**（压缩闭环、失败收尾、流式恢复、可观测、CI）基本是空的，而这恰好是 WeKnora 最厚的地方。

---

## 1. 先看清差距

| 维度 | WeKnora | pi-agent | 含义 |
|---|---|---|---|
| 代码量 | 452k Go（非测试）+ 387k 测试 | 34.5k Python | 不是同类，别比功能 |
| 测试 | 1935 个 `_test.go`（internal/cmd），测试:源码 ≈ 0.86 | 57 文件 / 796 个 test 函数 | 数量不差，差在**结构**与**自动化** |
| CI | `.github/workflows/app.yml` 一整套门禁 | **无 `.github/`** | 796 个测试没人跑 |
| 版本 | `VERSION` + ldflags 单一来源 | 根 `pyproject` = `0.0.0`，四个包 = `0.1.0` | 发版前必须修 |
| API 层 | 90 个路由按域拆 `routes_*.go` | `app.py` 3877 行，`create_app` 占 2670 行 | 最大结构债 |
| 前端 | 1168 文件，composable 各自带测试 | `Workbench.tsx` 6216 行，**0 前端测试** | 流式 reducer 无法回归 |
| 压缩 | 1528 行（非测试）闭环：切点/fit/降级/checkpoint/溢出 | 538 行，docstring 自认 4 项 deferral | 直接可补 |
| 循环保护 | 迭代上限 + 空回答/重复/连续截断/工具失败连击守卫 | `while True:`，**无上限** | 会跑飞 |
| 可观测 | Langfuse span 贯穿 LLM/工具/队列 | 只有 usage + audit 行 | 排障靠猜 |

---

## 2. P0：两周内做，收益确定

### 2.1 压缩：把 pi-agent 自己列的 deferral 补上

`packages/pi-coding-agent/src/pi_coding_agent/compaction.py:18-27` 明确写了 v0.1 没做的四件事。WeKnora 全做了，而且是为同一个失败面做的：

| pi-agent 的 deferral | WeKnora 的实现 | 为什么必须补 |
|---|---|---|
| file-operation tracking | `internal/agent/compaction/fileops.go:1-14` | **最高价值**。压缩丢掉"我写过 `/workspace/output/deck.html`"之后，模型会重写已存在的文件；大文件分块写入时这是**不终止的循环** |
| split-turn summaries | `cutpoint.go:98-107` + `compactor.go:176-198` | 切点落在轮中时，被保留的尾部失去了发起它的请求，后续工具调用失去上下文 |
| UPDATE 增量摘要 | `prepare.go:54-176`（`PreviousSummary` 原地更新） | 不更新就会变成"摘要的摘要的摘要" |
| 重试 wrapper | `compactor.go:28` `maxSummarizationAttempts=2` + `fallback.go:16` | 摘要模型失败时，pi-agent 会把失败当成功写进会话 |

除上述四项，还有五个机制值得照抄（同一包内，都是纯函数 + 少量状态）：

1. **切点预算当上限，不当下限。** `cutpoint.go:50-61` 的注释说得很直白：一条检索结果就一万 token，"保留最近 16k"会还回 25k。pi-agent 现在是 `accumulated >= keep_recent_tokens` 就停（`compaction.py:303-313`），把超预算的那条也留下了。
2. **摘要输入自己也要 fit。** `compactor.go:357-373` + `prepare.go:110-134`：算 prompt+回复预算，从最旧的消息开始丢，并明确告诉模型"有 N 条更早的消息未展示，不要猜"。pi-agent 没有上限，摘要请求本身就会超窗。
3. **腾不出空间就别压。** `compactor.go:41` `ErrNothingToCompact` + `observe.go:27-30` `minFreedFraction=20`（至少腾 5%）。否则每轮都花一次摘要调用换 240 token。
4. **工具结果裁剪做兜底。** `observe.go:197-332`：预算 = clamp(window/5, 8k, 32k)，先把所有工具结果换成占位，再从新到旧按 head(1/4)/tail(3/4) 二分回填。这类结果在"保留区"里，切点够不到。
5. **溢出识别 + 一次 compact-retry。** `overflow.go:15-72` 收了 24 家 provider 的报错正则（含 DashScope/Qwen、Kimi、MiniMax、OpenRouter），`engine.go` 用一个 `overflowRecovered` 标志保证只重试一次。pi-agent 在 `agent_session.py:24-27` 自认没做——**provider 一旦拒绝请求，整轮就以错误结束**。

**落地**：`compaction.py` 里加 `_file_ops(messages) -> (reads, writes)`、`_fit_summarizer_input()`、`_tokens_after` 字段；`agent_session._run_auto_compaction` 加 5% 门槛与 `_compaction_exhausted_at`。溢出分类单独 `overflow.py`（不是 FastAPI 那个）。

### 2.2 循环守卫与迭代上限

pi-agent `packages/pi-agent-core/src/pi_agent/loop.py:265-268` 是裸 `while True:`，没有轮数上限，也没有空回答/复读检测；`stop_reason` 只判 `error|aborted`（`loop.py:303-306`）。

WeKnora 的 `loopGuards`（`engine.go:560-618`）把失败模式列成了状态机：空回答重试、逐字复读、连续被长度截断、**同一工具调用连续失败 3 次就注入一条"换策略"提示**（`engine.go:1400-1410`，契约被 `failed_tool_loop_test.go:44-72` 钉住），外加 `MaxIterations` 默认 20 后转入收尾合成。

**落地**：`pi_agent/guards.py`，`TurnGuards.record_response(msg) -> Optional[Guard]`，在 `_run_loop` 的 `_process_events(turn_end)` 之前调用；触发后走 §2.3 的收尾路径而不是抛错。

### 2.3 失败时收尾合成，而不是留一条空错误消息

pi-agent `agent.py:398-413`：失败时构造一条 `stop_reason="error"`、正文为空的 assistant 消息，**收集到的工具结果全部丢弃**。

WeKnora `finalize.go:19-202`：失败/耗尽时用已收集的证据再做一次 `ToolChoice:"none"`、思考关闭的合成（一次溢出重试，空正文硬拒绝）。用户拿到的是"基于已查到的东西的回答 + 说明"，而不是一张白纸。

**落地**：`AgentSession._handle_post_agent_run` 里，不可重试的失败且 `len(messages) > 0` 时，带 `tools=[]` 调一次模型，把结果作为该轮 assistant 消息落盘。

### 2.4 类型化错误 + 统一出口

pi-agent `app.py` 有 **125 处 `HTTPException`**，客户端只能靠字符串匹配；WS 侧的错误形状又是另一套。

WeKnora `internal/errors/errors.go:9-24` 用**数字分段**表示子系统（1000 通用 / 2000 租户 / 2100 agent / 2200 向量库 / 2300 模型 / 2400 导入前置），`AppError{Code, Message, Details, HTTPCode}` 把 HTTP 状态带在错误上，处理器只 `c.Error(err)`，唯一映射点在 `middleware/error_handler.go:13-58`，未知错误统一收敛成 500 且不外泄内部信息。

**落地**：`pi_server/errors.py`：`AppErrorCode(str, Enum)` + `AppError(code, status, message, details)` + 一个 FastAPI exception handler + `to_ws_error()`。改法是逐处替换，不用一次改完。

### 2.5 拆 `app.py`：按域路由 + 依赖注入

- `create_app` 从 1121 行到 3793 行，86 个路由全是闭包；
- 认证中间件只判"有没有登录"（`app.py:1526-1535`），身份/角色由各路由自己取，90 条路由里只有 **7 处 `Depends`**；
- 结果：加一条路由要改一个 2600 行的函数，权限策略散落在业务代码里。

WeKnora 的做法：`internal/router/routes_{chat,agent,knowledge,automation,auth_tenant,infra,mcp_endpoint}.go` 按域拆；中间件顺序显式且有注释（`router.go:143-147` `RequestID → Language → Logger → Recovery → ErrorHandler`，之后才是 `Auth`，`langfuse.GinMiddleware()` 在认证之后）；能力校验集中成路由 → 策略表，**未登记的路由默认拒绝**（`middleware/api_key_gate.go:111-161`）。

**落地**（机械操作，先不改行为）：
1. `routers/{auth,admin,usage,sessions,workspace,office,apps,market,plugins,schedules,artifacts,stream}.py`，各自 `APIRouter(prefix=...)`；
2. `deps.py` 复用已有的 `auth.get_current_user/get_required_user/get_admin_user`（`auth.py:288-334` 已经写好了，只是没接）；
3. 一张 `(method, path_template) -> capability` 表，默认拒绝；
4. `app.py` 只剩 lifespan、中间件顺序、`include_router`、SPA mount；
5. WS 端点搬到 `routers/stream.py`，并且**每帧重新校验会话**，不要只在握手时校验（现在 `app.py:3717-3722` 只在握手判一次，登出/改角色不影响已连接的 socket）。

### 2.6 流式可恢复：run log + offset

pi-agent 的 `Hub`（`app.py:102-150`）只把事件推给**当前连着**的 socket；`Connection.queue` 随 socket 生死。刷新页面就丢掉进行中那一轮的 token，只能靠 `get_state` 拿到已落盘的记录。多标签页也没有"谁是 live run"的概念。

WeKnora：每条 (session, message) 一条 append-only 事件列表，消费端从 offset 0 重放再 tail（`internal/handler/session/stream.go:118`，chunk 合并 + 100ms 轮询 + 10s 心跳）；live-run 标记用 `SetNX` 防抢占、`ClaimLiveRun` 做交接、**Lua CAS 清理**保证只清自己那条（`internal/stream/redis_manager.go:391-507`）；控制类事件（steer）走**独立子列表**，不污染用户可见流。

**落地**：不要 Redis。用 SQLite 一张 `run_events(run_id, seq, type, payload, created_at)` + 进程内 dict 缓存；WS `ready` 里带 `since`，服务端先补发再 tail；`live_runs` 表 + CAS 清理。约 200 行，解决刷新丢流 + 双标签页。

### 2.7 CI、lint、版本号

- 没有 `.github/`：**796 个测试没有任何自动化在跑**；
- 没有 lint 配置：ruff 已在 dev 依赖里但未固定规则；
- 版本号分裂：根 `0.0.0` vs 包 `0.1.0`。

WeKnora 的对应物：一个 `make check` 串起 format/lint/test，CI 跑 `go vet` + `go test` + `-race` + 跨平台构建，**generated 数据有 `--check` 漂移门禁**（`Makefile:122`），集成测试用 `WEKNORA_REQUIRE_INTEGRATION=1` 把"静默跳过"变成失败（`app.yml:208`）。

**落地（半天）**：`.github/workflows/ci.yml`（`uv sync --frozen` → `ruff format --check` → `ruff check` → `pytest -q`）+ `pyproject.toml` 里 `[tool.ruff] line-length=120` + 修根版本号 + `Makefile` 的 `check/test/lint/fmt` 四个 target。

---

## 3. P1：一个月内做，需要一点设计

### 3.1 权限收敛成一个决策点

pi-agent 的 `policy/rules.py` 已经不错（deny 列表、confirm 列表、workspace 边界、symlink 解析、默认拒绝）。问题是它**只管工具调用前的放行**，而"这个 run 该看到哪些工具""批准一次能否记住""谁在什么时候改了什么"分散在别处。

WeKnora 的 `internal/agent/policy/policy.go` 只有 130 行，四个概念解决全部问题：

- `Risk`（read / probe / write / destructive）+ `Action{Risk, Always}` + `Rules{Level}` → `Decide()` 三值（Allow/Ask/Deny）；
- `Offered(level, risk)`：只读 run **不把用不了的写工具递给模型**（省 token，也省误导）；
- `Grantable` / `Pregrantable` / `Covers`：批准一次能否覆盖后续；**授权永不覆盖更高风险**；
- `Record()`：所有非 read 决策落审计行（谁、什么风险、什么级别、什么裁决、代理了谁）。

未知风险默认按 `destructive` 处理（`policy_test.go` 钉住）。

**落地**：`policy/rules.py` 之上套一层 `Risk` 分类与 `decide(action) -> Verdict`，把现有 confirm/deny 变成风险映射的产物；`Offered` 用于每轮过滤工具定义；`Record` 接现有 audit sink。

### 3.2 一份 run log，派生事件总线

pi-agent 现在没有事件总线：`_emit` 是唯一出口，观察者靠 `SessionWorker.__init__` 塞进来的 5 个回调（`on_warning/on_session_file/on_audit/on_audit_finish/on_usage`）。每加一个观察者就要改 `_observe`。

WeKnora 有 `event.EventBus`（约 40 个类型化事件，async 模式下**每个 handler 拿事件副本**，因为中间件会写 `Metadata["duration_ms"]`，共享 map 并发写是 recover 抓不到的 fatal panic——`event.go:197-202`），也有 stream manager 和 notice inbox。

**注意**：不要把 WeKnora 的三套机制一起搬。它们的 triplication 是历史包袱。pi-agent 应该**只有一份 append-only run log**（§2.6 那张表），事件总线是它的内存订阅视图，通知注入是它的一个投影。这样 WS 重放、审计、用量、通知天然一致。

### 3.3 后台结果回注（notice inbox）

pi-agent 有后台能力（subagent、schedules、notifications 表，`schedules.py:179-190` 会写通知），但那是**写给人看的 UI 行**，模型在下一轮看不到；`ask_user` 在无人可答时会失败退出（这是对的），但子 agent 完成、定时任务跑完，父会话并无"待汇报"概念。

WeKnora 的设计值得逐条抄：

- **每次后台工作都是租约**（`ActivityRegistry`：Add/Renew/Remove，不续约就消失，per-kind 上限）；
- **notice 由终态写入**，`Finish` 里**先 push notice 再移除注册**（顺序反了会出现"注册表和图收件箱都不知道它"的窗口，`lifecycle.go:78-95`）；
- **注入时机**是两个固定边界（轮开始 / 最终回答前），`MarkSeen/TakeSeen` 保证已经通过工具展示过的结果不重复说；
- **ack 在携带它的 assistant 消息落盘之后**（`run_notices.go:14-21`），否则崩溃会重复播报。

**落地**：复用 `db.py` 的通知表加一列 `delivered_to_model`；`SessionWorker` 在每轮开始前取未投递 notice，拼成 `<runtime_event>` 注入，落盘后 ack。约 150 行，换来"定时任务结果真的会回到对话里"。

### 3.4 可观测性：先有 span，再谈 Langfuse

pi-agent 只有 usage 行与 audit 行，出问题只能猜。

WeKnora 的形状（`internal/tracing/langfuse/`）：一个请求一条 trace（`METHOD route`，抽取上游 W3C `traceparent`），子 span 分 `span`（工具/步骤）与 `generation`（模型调用，带 `usage_details`、`completion_start_time` 算 TTFT），队列任务通过 `traceparent` 串回入队请求。

最值得抄的是**工具 span 的字段设计**（`internal/agent/act.go:79-113`）：同时记 `model_arguments`（模型实际吐出的、带临时句柄的原样）与 `resolved_arguments`（真正执行用的持久值），外加 `argument_resolution`；敏感工具（如 raw SQL）**只记参数 key**，不记值。

**落地**：`pi_server/tracing.py`，`contextvars` 承载 trace_id（单进程免费），根 span 每轮一条，工具 span 记 name/耗时/参数 key/结果 head-tail，先落 JSONL；WS 记录里带 `trace_id`，前端能显示。Langfuse 以后接。

### 3.5 token 估算：tiktoken + 自校准

pi-agent 用 `chars/4`（`compaction.py:181-215`）加图片固定 4800 字符——对中文与 reasoning 块都不准。

WeKnora 两层：`internal/agent/token/estimator.go` 用 cl100k BPE + 每消息/每工具调用/每条工具定义的固定开销 + 图片 1200 token；`internal/agent/calibration.go:45-105` 用**相邻两次请求的 provider prompt 增量 ÷ 中间新增消息的估算**得到一个比例（0.3–3.0 之外丢弃，样本不足 256 token 不采信，含图片的样本跳过），夹在 [0.5, 1.5]，写回本轮 usage 供下一轮用。

它自己起的名字很准确：**这是"学习 provider 怎么数 token"，不是"猜 tokenizer"**。

**落地**：估算函数加 `scale` 参数，存在 compaction entry / 会话 header 上；每轮根据 `usage.input_tokens` 差值更新。对中文医学文献场景收益明显（cl100k 对中文是 1 字≈1 token，Qwen/DeepSeek 约 0.6）。

### 3.6 suspend / resume 的快照只存持久值

pi-agent 有 `ask_user` 与 pending 语义，但没有"跑到一半停下、人答完继续"的完整恢复。

WeKnora `RunSnapshot`（`suspend.go:34-70`）的关键不在字段多，而在**边界纪律**：快照只存持久值——用户原话、已完成的 step、被阻塞的调用索引；**请求级句柄（`chunk://N`、`res://`、`msN`）一律不进快照**，恢复时重新渲染。恢复时校验 blocked index 与 prompt↔call id，再按原边界重放注入（`resume.go:94-109`）。

**落地**：`agent_session` 已有 session 文件，`suspend` 可以只是一条 `pending_prompt` entry + 恢复时从该 entry 重建；难点是"注入边界"要和 §3.3 的 notice 共用同一套重放逻辑，别做两套。

### 3.7 出网安全

pi-agent 的 `bash` 是全权 shell（`tools/bash.py:202-213`），`sandbox/provisioner.py:87-95` 用 `httpx` 连一个操作者给的 URL 且**不校验**；插件的 `plugin.json` 声明了 `capabilities` 但**没人执行**。

WeKnora 把这件事拆成三个可复用的小件：

- `internal/ipclass/ipclass.go:22-60`：IP 分类，**`Invalid` 单独一类，让忘记检查的调用方 fail closed**；覆盖 CGNAT、link-local（云元数据）、已废弃的 `fec0::/10`、以及 6to4/Teredo/NAT64 这些"外层看起来是公网"的转换地址。
- `sandbox/url_guard.go:80-180`：scheme 白名单 + 字面 IP 直接判 + 域名**解析后的每个地址**都判 + **拨号时**再判（只做字符串校验挡不住 DNS rebinding）。
- 命令审批请求里带**证据**：`Targets`（路径/类型/大小/条目数，不含内容）、`Scripts`（agent 自己写过的、即将被执行的脚本内容）、`ScriptsIncomplete`（不可展示就禁止批准）、`WriteContent`/`EditDiff`（`command_approval.go:7-45`）。

**落地**：`pi_server/net_guard.py`（IP 分类 + `validate_outbound_url()`），先接 `provisioner`，再有 fetch 工具时接拨号；`plugins` 的 `capabilities` 在加载时强制，未声明即拒绝；`bash` 若在多租户部署里跑，就把"bash === 主机权限"写进文档，不要假装有沙箱。

---

## 4. P2：按需，别提前做

### 4.1 上下文句柄（modelcontext）

这是 WeKnora 最有辨识度的设计：模型只看得到 `doc://3`、`res://0007` 这种**请求内短句柄**，执行前解开成持久 ID；未知句柄**执行前 fail closed**；流式输出时用 `streamHold` 把可能拼成句柄的尾巴压住，保证句柄不会被劈开发出去；每个模型调用点扫一遍**泄漏报告**（区分"编解码漏了"与"生产者没注册"）。

它解决的是真实失败：UUID 太长会烧 token、模型会改写 UUID 导致交叉链接失效、跨轮编号漂移、重放历史被二次压缩。

**但对今天的 pi-agent，整体移植不值得**（4264 行非测试代码，且 pi-agent 没有"外部证据 + 持久 ID"的表面）。只有三个小口子值得抄：

- `out://N`：`bash` 已经把完整输出落盘并把路径写进通知（`tools/bash.py:267-272`），那个绝对路径就是"又长又会被模型改写"的典型；
- `f://N`：`read` 输出的行号提示让模型自己做算术（`tools/read.py:156-172`）；
- 子 agent 的 `child://N` / `sess://N`（`subagents/manager.py:497-499` 把 uuid 抄进父对话）。

判断准则：只给**又长、模型会复述、服务端能解析**的身份做句柄；`tool_call.id` 绝对不要句柄化（provider 生成、已被 `transform.py:248-301` 修复配对）。

### 4.2 文献/证据检索（如果 lit、evidence 应用要继续做）

可复用的（都是小而独立的）：

- `internal/reranking/mmr.go:17-84`：MMR，相关性 vs 词面重复度，带倒排表与缓存，200 候选池只花 O(tokens)。医学查询"给我 5 篇不重叠的 RCT"就是这个形状。
- `internal/reranking/passage.go:61-164`：rerank 前的 passage 清洗（去 Markdown/URL/图片语法，保留 `$$…$$` 的 LaTeX 正文——剂量、公式需要）。同文件里有个实测结论值得直接采用：**同一 chunk 裸着打分 0.002，前面加上文献标题后 0.45**。
- `internal/searchutil/snippet.go:11-50`：关键词命中截取（60 字上下文、100 字上限）——比裸截断好得多。
- `internal/searchutil/normalize.go:12-108`：关键词分数按 p5/p95 百分位归一，避免一个离群值把整个量表压平（融合 BM25 与向量时必需）。
- `internal/searchutil/textutil.go:40-158`：CJK 友好的 tokenize / Jaccard。

不值得的：`imageinfo*.go`（把 OCR 文本对回 `<img>` 标签，只在 WeKnora 自己掌控分块时成立）、`image_evidence.go` / `reranking/image.go`（需要"chunk 本身就是图片 + 独立向量"的数据模型）、`model_output.go` 那套 982 行的 `<retrieval>/<document>/<chunk>` XML（它的存在意义是让一个 chunk 可引用；一行 `PMID + 标题 + 期刊` 对 pi-agent 足够）。

### 4.3 前端：把流式 reducer 抽出来

pi-agent 的 `web/src/workbench/Workbench.tsx` 6216 行，**没有任何前端测试**。

WeKnora 把流处理放在 composable 里，并且**按事件类别分了 6 个测试文件**：`useChatStreamHandler.{collaboration,dataChange,pageButton,secureForm,suspend,thinkBlocks}.test.ts`。目录本身就是风险清单。

**落地**：把 `Workbench.tsx` 里的流事件归约抽成 `web/src/lib/streamReducer.ts`（纯函数：`(state, event) -> state`），配 vitest；先覆盖"断线重连补发""工具执行中→完成""思考块合并""suspend/恢复"四类。没有这层，§2.6 的补发逻辑无法验证。

### 4.4 文档与发布纪律

- `docs/README.md` 索引：pi-agent 有 **61 个 md**（含 `research/`、`plans/`、截图），没有入口；
- 模块级 README：WeKnora 的 `internal/modelcontext/README.md` 是范本（文件→职责表、身份规则、请求生命周期 1-5、可观测性、自由文本里的句柄）；pi-agent 的 `db.py:1-24` 已经是这个体例的雏形，只差搬成 README；
- `CHANGELOG.md` 带 `### Breaking` / `### Upgrade Notes`（WeKnora 的升级须知会写到"迁移号 134→137，降级会丢凭据"这个粒度）；
- `SECURITY.md`（有凭据存储，就该有）；
- 发布核对记录：`docs/release-v0.9.1-audit.md` 用 commit/file 清单 + "每条公开说法对应哪个 PR"的方式对账——这对你给老板汇报 AI 板块其实同一套方法。

### 4.5 测试结构（不是数量）

pi-agent 的 796 个测试已经不少，缺的是 WeKnora 的三种**类型**：

1. **按不变量命名**：`failed_tool_loop_test.go`、`oauth_redirect_security_test.go`、`leaks_test.go`、`tool_goroutine_panic_test.go`。读目录等于读风险登记表。
2. **wire 格式 golden**：`internal/models/api/*/golden_test.go` 把厂商文档里的响应原样嵌入，**注释里写文档 URL 与日期**。pi-agent 的 v3 会话 fixture 现在是 `test_session_manager.py:621` 里的字符串字面量，应搬成 `tests/testdata/session/v3-golden.jsonl`。
3. **红队测试**：`oauth_redirect_security_test.go` 用表格列 10 种恶意重定向（`//evil.example`、`/%2f%2fevil.com`、`javascript:`、`/\n`…）断言全拒。pi-agent 最该有这种文件的地方是**插件沙箱**（逃逸、符号链接、env 泄漏、资源上限）——现在没有。

另外补两件小而硬的：**迁移等价性测试**（全新库 == 迁移后的库；`db.py:363` 的 `MIGRATIONS` 已具备条件），以及 **RPC/WS 协议 golden**（30 个命令一个都没被钉住；WeKnora 对应的做法是 `cli/acceptance/testdata/wire/*.json` + swagger 同步测试）。

---

## 5. 明确不要抄

| 不要抄 | 原因 |
|---|---|
| Redis 承载流状态 | 你是单进程 + SQLite；Redis 那套 TTL/`touchLiveRun`/CAS 是为了多副本共享 |
| 四级租户 RBAC + `X-Tenant-ID` 切换 | 一个客户一套部署、三种角色，抄来等于给每张表加 tenant 列 |
| API key capability 矩阵（10 个 scope） | 还没有机器客户端；抄**默认拒绝的路由策略**这个模式，别抄表 |
| 事件总线 + stream manager + notice inbox 三件套 | 那是历史包袱；一份 run log 派生即可 |
| 命令审批的 reviewer model（`hostreview/`） | 再调一次 LLM 去批准 agent 自己的动作，贵且不可审计；确定性策略更好 |
| 协作邮箱（`collaboration/`） | 子会话 + notice 注入已经覆盖；它会给恢复路径增加一倍分支 |
| `CommandApprovalRequest` 全套证据字段 | 为托管审阅模型设计；pi-agent 的 confirm/roots 已够 |
| 19 服务 dev compose、helm、4 语 README、小程序、Wails 桌面 | 体量产物 |
| 400 个"一个文件一个测试"的粒度 | 值得学的是命名，不是拆法 |
| 53KB `.env.example` | 值得学的是"标注风险/默认/失效模式"，不是长度 |
| swagger 生成 + 同步测试 | 你没有 OpenAPI 面；等价物是 RPC/WS 协议文档 |

---

## 6. 落地顺序

**第一周（每天几小时，确定性收益）**
1. `.github/workflows/ci.yml` + ruff 规则固定 + `Makefile` 的 `check`
2. 修版本号（`0.0.0` → `0.1.0`）+ `SECURITY.md`
3. `docs/README.md` 索引 61 个 md
4. 循环守卫 + 迭代上限（§2.2）
5. 失败收尾合成（§2.3）

**第一月**
6. 压缩补齐：file-ops、切点上限、fit、5% 门槛、`ErrNothingToCompact`、溢出分类（§2.1）
7. 类型化错误 + 统一出口（§2.4）
8. `app.py` 拆 routers + 能力表（§2.5）
9. run log + offset 补发 + live-run 标记（§2.6）
10. 权限收敛成单一 `decide()` + 审计（§3.1）
11. notice 回注 + ack-on-persist（§3.3）
12. 协议/迁移/插件安全三类测试（§4.5）

**第一季度**
13. 前端 streamReducer 抽取 + 测试（§4.3）
14. tracing（JSONL 先行）+ 工具 span 字段纪律（§3.4）
15. token 自校准（§3.5）
16. suspend/resume 快照（§3.6）
17. net_guard + 插件 capabilities 强制（§3.7）
18. 检索面出现时再引 MMR/passage/snippet（§4.2）

---

## 附录：证据索引

| 主题 | WeKnora | pi-agent |
|---|---|---|
| 压缩闭环 | `internal/agent/compaction/{cutpoint,fileops,prepare,fallback,overflow,compactor}.go` | `packages/pi-coding-agent/src/pi_coding_agent/compaction.py:18-27` |
| 工具结果裁剪 | `internal/agent/observe.go:197-332` | 无 |
| 循环守卫 | `internal/agent/engine.go:560-618,1400-1410` | `packages/pi-agent-core/src/pi_agent/loop.py:265-268` |
| 失败收尾 | `internal/agent/finalize.go:19-202` | `packages/pi-agent-core/src/pi_agent/agent.py:398-413` |
| 权限判定 | `internal/agent/policy/policy.go:44-130` | `packages/pi-coding-agent/src/pi_coding_agent/policy/rules.py` |
| 类型化错误 | `internal/errors/errors.go:9-69`；`middleware/error_handler.go:13-58` | `app.py` 125 处 `HTTPException` |
| 路由/依赖 | `internal/router/routes_*.go`；`router.go:143-147` | `app.py:1121-3793`（`create_app`） |
| 流式恢复 | `internal/stream/redis_manager.go:391-507`；`handler/session/stream.go:118` | `app.py:102-150`（`Hub`） |
| 通知注入 | `internal/background/run_notices.go:14-150` | `schedules.py:179-190`（只写 UI 通知） |
| 可观测 | `internal/tracing/langfuse/*`；`internal/agent/act.go:79-113` | 无 |
| token 估算 | `internal/agent/token/estimator.go`；`agent/calibration.go:45-105` | `compaction.py:181-215`（chars/4） |
| 句柄边界 | `internal/modelcontext/*`（含 README） | 无 |
| 出网安全 | `internal/ipclass/ipclass.go:22-60`；`sandbox/url_guard.go:80-180` | `sandbox/provisioner.py:87-95` |
| 前端流处理 | `frontend/src/composables/useChatStreamHandler*.test.ts` | `web/src/workbench/Workbench.tsx`（6216 行） |
| 测试纪律 | `internal/mcp/oauth_redirect_security_test.go`；`internal/modelcontext/leaks_test.go` | `tests/`（57 文件 / 796 函数，无 CI） |
| 文档纪律 | `internal/modelcontext/README.md`；`docs/release-v0.9.1-audit.md` | `docs/`（61 md，无索引） |

---

## 落地进度（2026-11-25）

| 建议 | 状态 | 落在哪里 |
|---|---|---|
| 压缩闭环（§2.1） | 已落地 | `compaction.py`：切点上限、split-turn、file-ops 保留、previous-summary 传递、摘要输入 fit、5% 腾出门槛、降级 fallback；`overflow.py` 溢出分类 + 一次 compact-retry |
| 循环守卫与迭代上限（§2.2） | 已落地 | `pi_agent/guards.py`；`AgentLoopConfig.turn_guards`；`AgentSession` 默认 `max_turns=20` |
| 失败收尾合成（§2.3） | 已落地 | `AgentSession._finalize_after_stop`：无工具的收尾调用，空结果丢弃 |
| 类型化错误（§2.4） | 未做 | 仍是 `HTTPException`；留给下一轮 |
| 拆 `app.py`（§2.5） | 未做 | 仍是单 `create_app` |
| 流式可恢复（§2.6） | 已落地 | `pi_server/runlog.py`、`ready.seq/lastPersistedSeq`、`?since=` 重放、客户端 `serverSeqRef` |
| CI / lint / 版本（§2.7） | 已落地 | `.github/workflows/ci.yml`、`Makefile`、ruff 规则、版本统一 0.1.0 |
| 权限收敛（§3.1） | 已落地 | `policy/rules.py`：Risk / SessionGrants / offered_tools；扩展记录 risk 与 granted_by |
| 事件总线（§3.2） | 决定不做 | run log 已是唯一事实来源；理由见 `docs/plans/2026-11-25-p1-hardening.md` |
| notice 回注（§3.3） | 已存在（subagent） | `subagents/manager.py` 已回注；定时任务为一次性会话，UI 通知是正确通道 |
| 可观测性（§3.4） | 已落地 | `pi_server/tracing.py`，JSONL span，参数白名单与脱敏 |
| token 自校准（§3.5） | 已落地 | `token_scale.py` + 压缩链路 scale |
| suspend/resume（§3.6） | 不引入快照 | `fold_questions` 从 transcript 折叠，已补回放测试 |
| 出网安全（§3.7） | 已落地 | `net_guard.py` + provisioner 接入 + 插件清单一致性 |
| 前端 reducer（§4.3） | 已落地 | `web/src/lib/streamReducer.ts` + `verify-stream-reducer.mjs`（30 项） |
| 测试结构（§4.5） | 已落地 | 会话 v3 golden 文件化、RPC 协议 golden、插件红队、迁移等价性；Docker/office 测试改为可跳过/自带夹具 |
