# P1 加固：现状核查、设计与取舍

> 基线：`dev@82d27d2`（2026-11-25）。本文是 `LEARNINGS-weknora.md` §3 的落地前分析。
> 每一条都先给**现状证据**，再给结论——其中有两条与原始建议不同：实现前先核对，
> 避免把已经存在的东西再写一遍。

| 项 | 现状 | 结论 | 动作 |
|---|---|---|---|
| 3.1 权限收敛 | 已有单一决策点 `classify_tool_call`，但无风险等级/会话授权/工具可见性 | **部分成立** | 补 Risk、会话授权、Offered、审计字段 |
| 3.2 run log 派生事件总线 | run log 已有（`runlog.py`），观察者已通过 `_emit` 单点接入 | **不成立** | 书面说明不做 |
| 3.3 notice 回注 | subagent 已用 `send_custom_message(deliver_as="steer")` + `continue_turn` 回注 | **大部分已存在** | 定时任务侧说明为何不适用 |
| 3.4 可观测性 | 无 tracing，只有 usage/audit 行 | **成立** | JSONL span + 工具 span 字段纪律 |
| 3.5 token 自校准 | `estimate_tokens` 是 `chars/4`，无 scale | **成立** | scale + 相邻请求差分标定 |
| 3.6 suspend/resume | `ask_user` 已实现"挂起→pending→续跑/迟答" | **设计已覆盖** | 补一个迟答回放测试，不引入快照机 |
| 3.7 出网安全 | provisioner 直接用配置 URL；插件 `permissions` 无人校验 | **成立（范围需修正）** | `net_guard` + 清单一致性校验 |

---

## 3.1 权限：缺的是"等级"和"记住"，不是"决策点"

**现状证据**

- `policy/rules.py:281-311` `classify_tool_call` 已经是唯一决策点：`Decision.ALLOW/CONFIRM/DENY`
  （`rules.py:119-130`），`extension.py:24-70` 是唯一的执行适配层，`_record` 负责审计。
- 判定依据是**工具名 + 路径是否越界 + 命令模式**（`rules.py:171-275`），没有"这次操作的等级"这个概念。
- `auto_approve` 是部署级全开（`rules.py:163-165`），没有"本会话内记住"。
- `allow_tools` 里没列的工具会被 `DENY`（`rules.py:287-288`），但它们**仍然出现在模型看到的工具列表里**——
  模型会调用一个必然被拒的工具，浪费一轮。

**设计**

1. `Risk`：`read / probe / write / destructive`，由 `classify_risk(tool, args, verdict)` 给出。
   它是**附加轴**，不替代现有路径/命令判定：`DENY` 依然由现有规则产出，`Risk` 只用于分级授权与审计。
2. `Decide(action, rules)`：沿用 WeKnora 的矩阵（read/probe→allow；write→confirm/auto；destructive→confirm），
   但只有在现有判定为 `ALLOW` 或 `CONFIRM` 时才生效——**现有规则仍然是一票否决**。
3. `SessionGrants`：进程内 per-session 的 `set[Risk]`；`covers(granted, risk)` 永不放宽（`risk <= granted`）。
   `PolicyConfig.grant_after_confirm`（默认 False）开启时，确认文案明确写"本会话内同类操作不再询问"。
4. `Offered`：`PolicyConfig.offered_tools(names)` 过滤掉既不在 `allow_tools` 也不在 `confirm_tools` 的工具，
   `AgentSession` 用它裁剪 `active_tool_names`（省 token，也避免"调了必拒"）。
5. 审计行增加 `risk` 与 `granted_by`（`confirm` / `session` / `auto`），让"谁批准的"可查。

**风险**：现有 `tests/test_policy.py` 的 34 条不变量必须全绿；`Risk` 判定错误只会影响"是否记住授权"，
不会放宽 DENY。

**验收**：新增 `tests/test_policy_risk.py` 覆盖矩阵、授权不放宽、Offered 过滤、审计字段。

---

## 3.2 事件总线：不做，写清理由

run log（`pi_server/runlog.py`）已经是唯一的下行事件事实来源，`SessionWorker._emit` 是唯一出口，
审计/用量/WS 分发都在这里（`app.py:_emit`）。再引入一个进程内总线只会得到**第二套**
订阅与顺序语义，而这正是 WeKnora 三套机制并存后被诟病的地方。

**结论**：不做。若将来出现第三方观察者需求，从 run log 派生 `subscribe(seq)` 即可，不需要新总线。

---

## 3.3 notice 回注：subagent 已有，定时任务不适用

**现状证据**

- `subagents/manager.py:353-392`：子 agent 完成时 `parent.send_custom_message(text, deliver_as="steer")`；
  父正在跑就搭 steering 队列，空闲则 `continue_turn()`；用户抢跑时消息留在 transcript 里，下一轮自然可见。
- `schedules.py:112-190`：每次运行 `registry.acquire_scheduled(...)` 拿一个**一次性 worker**，
  跑完 `worker.shutdown(...)`，会话文件随后落库。整条链路里**没有"待汇报的父对话"**。

**结论**

- 子 agent 侧：已满足 WeKnora 的"终态写入 + 边界投递"，不需要改。
- 定时任务侧：它产出的是**新会话**，把它塞进某个旧对话的上下文反而是错的。UI 通知（`create_notification`）
  是正确通道。若将来要"从某次对话创建定时任务并回报到该对话"，需要 `schedules.source_session_file`
  与一次迁移——那是产品功能，不是加固项。

**动作**：在 `schedules.py` 顶部写清这个边界，并在设计文档里登记为"有意不做"。

---

## 3.4 可观测性：只补 span，不引 OTel

**现状证据**：`packages/*/src` 无 trace/span/otel 依赖；只有 `usage` 行（`app.py:_record_usage`）
与 `audit` 行（`_observe_tool_end`）。排障只能看会话 JSONL。

**设计**

- `pi_server/tracing.py`：`Tracer(path)` 追加 JSONL；`trace_id` 用 `contextvars`，每条记录自带
  `trace_id/span_id/parent_id/kind/name/start/end`。
- span 种类：`turn`（一次 prompt）、`llm`（每次 message_end 带 usage）、`tool`（`tool_execution_start/end`）、
  `compaction`、`subagent`。
- 工具 span 字段纪律（抄 WeKnora 的思路）：只记 `args_keys` 与**白名单键的值**（`path`/`command`/`pattern`
  截断到 200 字符），其余只记类型；结果只记长度与前 200 字符；**永不记录文件内容、env、密钥**。
- 失败静默：tracing 任何异常都不能影响会话（写盘失败降级为 no-op）。
- 开关：`PI_TRACING=1`（或 `ServerConfig`），默认关——不给所有部署凭空多写盘。

**验收**：`tests/test_tracing.py`：span 落盘、父子关系、脱敏（写入 token/密码不出现）、异常不影响主流程。

---

## 3.5 token 自校准：只标定"聊天消息"，不标定工具 schema

**现状证据**：`compaction.estimate_tokens` 是 `chars/4`（`compaction.py:181-215`），图片按 4800 字符计；
`estimate_context_tokens` 用最后一次 usage + 其后消息估算（`compaction.py:232-244`）。

**设计**（与 WeKnora 的差别：只学一个标量，且只用于对话消息）

- 相邻两次请求共享 system prompt 与工具定义，其 usage 增量 **只**对应新增消息；因此
  `scale = Σ(provider_delta) / Σ(estimate_delta)`，比整个请求的比值更干净。
- 采样门槛：至少 256 估算 token；单样本比值落在 [0.3, 3.0] 之外丢弃（provider 只计非缓存输入等）；
  含图片的样本跳过（按 tile 计费，与文本无关）。
- 夹在 [0.5, 1.5]；`scale` 存在 `AgentSession` 上，传给 `compact_session(scale=...)`。
- 只影响"何时压缩/压缩保留多少"，不影响正确性；估算偏差只造成压缩早晚。

**验收**：`tests/test_token_scale.py`：差分学习、越界丢弃、图片跳过、上下界、无样本保持 1.0。

---

## 3.6 suspend/resume：现有语义已覆盖，不引入快照机

**现状证据**

- `user_questions.py:5-21`：工具调用停在 future 上，客户端渲染卡片；超时返回 `{"pending": true, "callId": ...}`
  且**问题仍可回答**；迟到的答案作为消息进入会话（`_Pending` 的 `continued` 分支，`user_questions.py:403-445`）。
- UI 侧：`AgentStore.questions` 明确支持"从 transcript 重放的、仍可回答的问题"（`useAgent.ts` 注释）。

**与 WeKnora 的差别**：WeKnora 需要"从被阻塞的那一步精确续跑"（`RunSnapshot.PendingRound.BlockedIndex`），
因为它的工具调用是**长事务**（沙箱命令、审批）。pi-agent 的阻塞点是"问人"，语义上等价于
"这一轮以 pending 结束，答案作为下一条输入"——不需要重放半个轮次。

**结论**：不引入快照机。补一条测试证明"进程内迟答"与"重放后迟答"都能到达模型即可。

---

## 3.7 出网安全：范围修正

**现状证据**

- `sandbox/provisioner.py:73,96`：`self.url` 直接交给 `httpx.Client(base_url=...)`，无 scheme/host 校验。
  这个 URL 来自**运维配置**，不是用户输入，但错误配置会把 token 发到任意地址。
- `plugins.py:77-86`：`capabilities` 与 `permissions` 都被解析、存进 `PluginInfo`，**没有任何校验或执行**。
  实测：四个插件声明 `capabilities: ["tools"]`，却在 `permissions.network` 里写 `true` 或主机白名单——
  两份声明互相矛盾。
- 插件在**进程内**执行（`plugins.py` 顶部已注明），所以"强制 network 权限"在没有进程/沙箱隔离时
  无法真正生效。

**设计（可做且诚实的部分）**

1. `pi_coding_agent/net_guard.py`：IP 分类（loopback/private/link-local/CGNAT/6to4/Teredo/IPv4-mapped/保留）、
   `validate_outbound_url(url, allow_private=False)`、`resolve_and_validate(host)`；拨号前校验（缓解 DNS rebinding）。
2. provisioner 接入：scheme 必须 http(s)；默认允许私网（部署常在私有网络），但 `PI_SANDBOX_ALLOW_PUBLIC_URL=1`
   才允许公网地址——反向默认（默认只允许私网）更安全，且不破坏现有部署（它们本来就是私网）。
3. 插件清单一致性校验：`permissions.network` 非 false 的插件必须声明 `network` capability，否则拒绝加载；
   声明的主机白名单写进 `PluginInfo` 并在 admin API 暴露——**让审批者看见**，不假装执行。
4. 明确写入文档：进程内插件仍以服务器全权运行；真正的强制需要子进程 + capability 过滤，属下一阶段。

**验收**：`tests/test_net_guard.py`（分类表、URL 校验、拨号前解析）、`tests/test_plugin_permissions.py`（矛盾清单被拒、白名单暴露）。

---

## 实施顺序

1. **3.7 net_guard + 清单校验**（独立、无行为风险）
2. **3.5 token 标定**（纯函数 + 一个字段）
3. **3.4 tracing**（旁路，默认关）
4. **3.1 Risk + 会话授权 + Offered**（唯一动到权限路径的项，测试最厚）
5. **3.6 迟答回放测试**（只加测试）
6. 3.2 / 3.3 按上文登记为"有意不做"

---

## 落地结果（同日）

| 项 | 落地内容 | 测试 |
|---|---|---|
| 3.1 | `rules.py` 新增 `Risk` / `risk_of` / `SessionGrants` / `PolicyConfig.offered_tools`；`extension.py` 记录 `risk` / `granted_by` / `granted_for_session`，按 `grant_after_confirm` 记住写入授权；`AgentSession._sync_offered_tools` 在每轮开始前裁剪工具与 prompt | `tests/test_policy_risk.py` 29 项；原 `test_policy.py` 49 项不变 |
| 3.2 | 不做（run log 已是唯一事实来源） | — |
| 3.3 | subagent 已具备；schedules 说明为何不适用 | — |
| 3.4 | `pi_server/tracing.py`（`Tracer` / `EventTracer` / `NullTracer`），`PI_TRACING=1` 开启，落在 `<data_root>/traces/YYYY-MM-DD.jsonl`；参数白名单 + 密钥脱敏 + 未结束工具 span 标记 incomplete | `tests/test_tracing.py` 8 项 |
| 3.5 | `token_scale.py`（有界标量 + 样本过滤）；`compaction` 全链路接受 `scale`；`AgentSession` 用相邻请求差分标定 | `tests/test_token_scale.py` 10 项 |
| 3.6 | 补 `fold_questions` 回放测试：pending / open / settled / 迟答 custom message / 重载后仍可答 | `tests/test_question_replay.py` 5 项 |
| 3.7 | `net_guard.py`（地址分类 + URL 校验 + 启动期与拨号期两段）；provisioner 接入；插件清单 `permissions` 与 `capabilities` 一致性校验，矛盾即拒绝，主机白名单进 admin API | `tests/test_net_guard.py` 40 项、`tests/test_plugin_permissions.py` 19 项 |

顺带修掉两个真实缺口：`dd if=… of=/dev/sda` 因为 `_WRITE_TARGET` 只匹配 `dd of=` 相邻写法而被当成普通读路径（现在按越界写处理）；
七个内置插件的清单声明与 `permissions` 不一致（原来 admin 面板只报 `tools`）。
