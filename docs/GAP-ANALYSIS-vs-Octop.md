# python-pi ← Octop Harness / Octop 借鉴分析

> 基准日：2026-10-30。只读对比，未改动任何代码。
> **A** = `/Users/admin/git/octop-harness`（腾讯，agent 运行时**库**）
> **B** = `/Users/admin/git/Octop`（腾讯，自托管**平台**，依赖 `octop-harness[all]>=1.0.5`）
> **C** = 本仓库（pi 的 Python 移植 + 自带多租户 Web 服务）
>
> A 与 B 是**上下游**，不是两个竞品：A 是"运行时该有什么"，B 是"平台该有什么"。
> 本文与 `GAP-ANALYSIS-vs-DSH.md` 有交集的部分只做交叉引用，不重复展开——那份文档已经
> 用 DSH 论证过"要加 todo / 作业控制 / ask_user / MCP / web"。本文只回答一个新问题：
> **同样的缺口，A/B 给出的实现路径是什么，哪一个更便宜、更适合 C。**

---

## 0. 一页结论

1. **三者不在同一层。** C 同时干了 A 和 B 的活：自己的 agent 循环（A 的活）+ 自己的多租户
   Web 平台（B 的活）。所以"比功能数量"没有意义，要比的是**同一件事谁的做法更省**。
2. **C 的骨架比两者都干净。** 零框架依赖、pi 会话格式互通、全离线确定性测试
   （30 文件 / 1.48 万行）——这三样 A 和 B 都没有。A 的会话在 LangGraph checkpoint 里、
   成本口径跟着 LangChain 的 usage 元数据走；B 的会话在控制平面 DB 里。两者都换不来
   C 的"会话文件可以被 pi 直接打开"。
3. **C 输在"骨架之外的工程件"，不是架构。** 最扎眼的一条：**C 没有 CI、没有 ruff、没有 mypy**，
   而 A 有 `make all`（format+lint+`mypy --strict`+test）、6 个 GitHub workflow、PyPI 发布流水线。
   这是**半天到两天**的差距，也是本文里性价比最高的一条。
4. **安全方面 A/B 有 C 缺的一整层"默认形态"**：声明式 `SecurityPolicy`（把 `interrupt_on` /
   filesystem permissions / PII 一次性展开）、规则即数据（YAML + 严重级 + 威胁分类 + 修复建议）、
   **粘性放行**（`ask` / `allow_all` / `allow_tools`，且永不跳过 `ask_user_question`）、
   PII 脱敏、SSRF 守卫。这些都不需要引入 LangChain，抄的是**设计**不是代码。
5. **产品面缺三块，而且 B 有现成答案**：分层记忆（`USER.md` / `MEMORY.md` 会话快照）、
   知识库 RAG（per-KB SQLite sidecar + `search_knowledge` + 引用标记）、定时任务
   （`cron:` / `interval:` / `date:` DSL + `text` vs `agent` 两种任务类型）。
6. **最该抄的一个"设计"是 A/B 对工具结果的处理**：把**数据面**（大 payload 给 UI 和历史）与
   **控制面**（摘要给 LLM）在执行后、进 state 前拆开。这正好补上 DSH 分析里点名的
   `spill` 缺口，而 B 的 `docs/octop-ui-payload-offload.md` 是一份经过三方评审的完整方案。

**一句话**：C 不需要变得更像 A/B，需要把 A/B 已经踩过的坑做成 C 自己的工程件。

---

## 1. 规模与定位基线

| 维度 | A：octop-harness | B：Octop | C：python-pi |
|---|---|---|---|
| 定位 | agent 运行时库 / SDK | 自托管多用户 agent 平台 | pi 移植 + 自带多租户 Web |
| 底座 | LangChain + LangGraph + deepagents | FastAPI + A + octop-gateway + octop-memory | **自研 loop**（仅 httpx + pyyaml） |
| Python | 3.12+ | 3.12+ | **3.11+** |
| 源码量 | 216 文件 / 43,999 行 | 706 文件 / 164,408 行 | 70 文件 / 25,314 行（+ web 6,665 行） |
| 测试 | 127 文件 / 31,961 行 | 538 文件 / 107,795 行 | 30 文件 / 14,800 行 |
| 前端 | 无（库） | React 18 + AntD，1,060 个 ts/tsx | 25 文件固定 SPA |
| 会话状态 | LangGraph checkpoint（SQLite） | 控制平面 DB + 可选 `history_v2.sqlite` | **JSONL v3 树（pi 互通）** |
| 模型可见工具 | ~20+（deepagents 内置 + 10 个自建工具模块） | ~20+（+ 知识库 / cron / 记忆 / 终端） | **7 个，默认激活 4 个** |
| 扩展单元 | 中间件（27 个模块） | `plugin.yaml` + `setup(ctx)` + 可选 UI bundle | `setup(pi)` + 21 个事件 |
| 分发 | PyPI + 6 个 CI workflow + CodeQL + dependabot | PyPI + 桌面端 + FnOS 包 | **无 CI、无 lint、无类型检查** |
| 多租户 | 无（宿主自己管） | 有（JWT + expert + 通道 + 配额视图） | 有（cookie 会话 + 配额 + 审计 + 沙箱） |

三个数字值得单独记住：

- A 的 `Makefile` 里 `all` = `format + lint + typecheck + test`，`typecheck` 是 `mypy --strict src`；
- B 的路由是 **60 个模块**（`src/octop/api/routers/`），C 是 21 个 REST 路由 + 1 个 WebSocket；
- C 的 588 个测试函数**全离线**，A 的 3.2 万行测试里含 bwrap / docker 集成（CI 上还得跳过 bwrap）。

---

## 2. 三条结构性差异

### 2.1 谁拥有 agent 循环——C 最大的资产，也是最大的代价

| | A / B | C |
|---|---|---|
| 循环归属 | LangGraph `create_agent` / `create_deep_agent` | `packages/pi-agent-core`，自己写 |
| 好处 | 生态：checkpointer、MCP adapter、HITL middleware、LangSmith、中间件链 | 完全可控：事件顺序、成本公式、截断提示、会话文件形状都可逐字复刻 |
| 代价 | 框架版本迁移、魔法行为、"为什么这轮多了一次模型调用" | 每加一个横切能力都要先补缝（DSH 分析 §2 已论证） |

A 的 `agent.py` 是 2,037 行的门面（`manager.py` 497 行），真正的能力全在 **27 个中间件模块**里：
`model_router` / `model_retry` / `model_settings` / `tools_filter` / `tool_search` /
`skill_filter` / `media_offload` / `context_usage` / `pii` / `mcp_tools` / `filesystem_guard` /
`tool_guard` / `memory_recall` / `peer` / `checkpoint_ts` …

**这给了 C 一个重要的反面结论**：中间件链是"框架替你调用"的产物。C 已经有 **21 个扩展事件**
（`docs/EXTENSIONS.md`），再叠一套中间件就是两套机制做同一件事——这是本文明确**不建议**抄的一条（见 §5）。

但 A 的中间件**清单本身**很值钱：它是一份"agent 运行时需要考虑哪些横切关注点"的检查表。
C 可以直接拿这张表对照自己的 21 个事件，看哪些点没有发事件（例如 provider 请求前 / 压缩前后）。

### 2.2 状态放在哪里

```
A:  LangGraph checkpoint(SQLite)  +  octop-memory(L0→L3)  +  工作区 md 文件
B:  控制平面 DB(users/agents/sessions/thread_messages/usage)  +  history_v2.sqlite  +  per-KB index.sqlite
C:  JSONL v3 会话树  +  控制平面 DB(users/auth_sessions/usage/audit/session_pointers)
```

C 的选择是**有意为之**（README「与 pi 互通」），而且**不要改**：

- A 的会话在 checkpoint 里，只有 `aget_history()` 能读出来，**换一个 runtime 就读不了**；
- B 为了补这个短板写了 `docs/versioned-history.md`（v2 分段归档 + 内容哈希共享正文），
  一份 60+ 行的"怎么启用/验证/回退"文档，本质上是在给 checkpoint 模型打补丁；
- C 的 JSONL v3 是**外部可读、可 fork、可被 pi 打开**的资产。B 为了同样的能力要付出一整个历史层。

**可借鉴的是 B 的 `history_v2` 里两个小点子，不是它的整体方案**：
按内容哈希共享大字段（相同工具结果只存一份）、以及"每次只提交变化的消息、超 1024 字符按固定边界分块"。
C 的 `session_manager` 现在是逐条 append，长会话重复工具输出会线性膨胀——这一条可以低成本借鉴。

### 2.3 扩展点长什么样

| | A | B | C |
|---|---|---|---|
| 形态 | Python 中间件类 | 目录 + `plugin.yaml` + `main.py:setup(ctx)` + 可选 UI | Python 模块 + `setup(pi)` |
| 元数据 | 无（代码即注册） | `id/version/name/icon/kind/entry/ui` | 无 |
| 种类 | 中间件 | `tool` / `skill` / `hook` | 工具 / 命令 / 事件拦截 |
| 治理 | 无 | 管理端开关、内置 seed 默认关、卸载不重装 | 项目信任门 + `-e` + entry point |
| UI 扩展 | 无 | `ui.entry` → 预构建 js，前端渲染工具结果卡片 | 只有 dialog |

**这是 B 里最"便宜且立刻有用"的一块**：C 的扩展已经能注册工具和拦截事件，缺的只是
**一份清单**（谁能被列出、能不能在管理台按用户开关）和**一个 UI 出口**（工具结果能不能渲染成卡片）。
`plugins/demo-ui-card/plugin.yaml` 全文只有 9 行。

---

## 3. C 已经领先，别被规模带偏

| C 的东西 | 为什么 A/B 给不了 |
|---|---|
| **pi 会话格式互通** | A/B 的会话是自己的形状；C 的 `.jsonl` 能被 pi 打开，也能读 pi 写的。这是一个**不可替代**的位置 |
| **零框架依赖** | A 的 `pyproject.toml` 依赖 deepagents/langchain/langgraph/octop-memory/octop-browser；B 再叠一层。C 的运行时只有 httpx + pyyaml，安装体积与升级风险差一个量级 |
| **逐字复刻的成本核算** | A 的 `usage.py` 只做 usage 归一化，cost 口径跟着 LangChain / 模型元数据；C 的 `cost.py` 逐字复刻 pi 的分层计价（1h cache write 双倍），服务端还有 per-user 账本与月度配额 |
| **审计口径更贴执行面** | B 的 `audit_log` 记的是**管理动作**（actor/action/target/payload）；C 的 `audit_records` 记的是**每一次工具调用**（decision/reason/input_preview/duration_ms/exit_code），配管理台——回答的是“这个 agent 到底跑了什么” |
| **Docker 沙箱的执行面** | A 有 8 种 backend（local/bwrap/docker/opensandbox/s3/cos/oss/postgres），是个**库**的正确答案；C 只需要一种，且做透了（容器消失自愈、内外路径统一、可中止的流） |
| **测试全离线确定性** | A 的 CI 得跳过 bwrap；C 的 588 个测试在任何机器上结果一致 |

**结论**：C 不需要为了"看起来像大项目"去补 backend 矩阵、TUI、桌面端、远程桌面、MBTI 人格。

---

## 4. 值得借鉴的清单（按性价比排序）

| # | 项 | 来源 | 工时 | 等级 | 一句话理由 |
|---|---|---|---|---|---|
| 1 | 工程卫生：ruff + `mypy` + CI + pre-commit | A | 0.5–2 d | 🟢 立刻 | 零设计成本，直接防回归 |
| 2 | 安全默认形态（声明式策略 / 规则即数据 / 粘性放行） | A + B | 3–5 d | 🔴 高 | 同时修 DSH 分析 §2.5 的 5 条短板 |
| 3 | PII 脱敏 + SSRF 守卫 | A | 1–2 d | 🔴 高 | 多租户共享 key 下，这是数据外泄面 |
| 4 | 三个小工具：`ask_user_question` / `send_file`(present) / `current_time` | A | 2–3 d | 🟠 中高 | 复用现成 dialog 通道，感知强 |
| 5 | 工具结果「数据面 / 控制面」分离 | B | 3–5 d | 🔴 高 | 正对 `spill` 缺口，且有大输出场景 |
| 6 | 分层记忆：`USER.md` / `MEMORY.md` 会话快照 | A | 3–5 d | 🔴 高 | 团队助手最缺的"记得这个人" |
| 7 | MCP 接入（含 4 个坑） | A | 5–7 d | 🔴 高 | 一次接整个生态 |
| 8 | 知识库 RAG | B | 8–12 d | 🟠 中高 | 私有资料问答，团队刚需 |
| 9 | 定时任务 DSL | B | 5–7 d | 🟠 中 | "每天早报/巡检"是团队场景 |
| 10 | 插件清单 + 管理端开关 | B | 2–3 d | 🟠 中 | 已有 `setup(pi)`，只缺清单 |
| 11 | 模型 preset + fallback + 每轮覆盖 | A | 3–5 d | 🟡 中低 | 多 provider 时省事 |
| 12 | 上下文用量与媒体卸载 | A | 2–3 d | 🟡 中低 | 长会话成本可见 |
| 13 | IM 通道（先飞书） | B | 5–8 d/平台 | 🟡 视需要 | 团队入口，但成本高 |

### 4.1 工程卫生（0.5–2 天，最便宜的差距）

A 的 `pyproject.toml` 里有 `ruff`（E/W/F/I/B/UP/SIM/RUF/N/PL）+ `mypy>=1.11`，
`Makefile` 的 `all` 串起 format→lint→`mypy --strict src`→test，`.github/workflows/` 有
`ci.yml`（PR 触发 + concurrency 取消）`release.yml` `codeql.yml` `docs.yml`
`auto-tag-on-release.yml`，外加 `dependabot.yml` 和 `.githooks/`。

C 现状：**一个都没有**（仓库根没有 `.github/`，`pyproject.toml` 只有 pytest 配置）。

落地形状：

1. `ruff` 用 A 那套 select 列表起步，先只对 `packages/` 跑格式检查，避免一次性几千行 diff；
2. `mypy` **不要**一上来 `--strict`（C 有 2.5 万行无注解代码）；先 `--ignore-missing-imports` +
   逐包 `disallow_untyped_defs`，用 `per-module` 配置按 `pi-ai → pi-agent-core → pi-coding-agent → pi-server` 的顺序推进；
3. CI 只要一个 workflow：`uv sync` → `ruff check` → `ruff format --check` → `pytest -q`；
4. 顺手补 `CHANGELOG.md` / `CONTRIBUTING.md` / `SECURITY.md`（A 都有，C 都缺）；
5. 把 README 里"单进程是硬约束""沙盒只覆盖命令执行"这两条写成 `docs/adr/` 式的短文档——
   B 的 `docs/adr/001-single-process-model.md` 就是同一件事的标准写法（含 trade-off 表）。

**为什么排第一**：C 的测试已经很强（588 个测试函数），但没有 CI 意味着它们**只在作者本机跑**。
补 CI 的收益是"已有资产开始自动生效"，不是"新增能力"。

### 4.2 安全默认形态（3–5 天，修的是已知短板）

DSH 分析 §2.5 给 C 列了 5 条：正则分类器可被解释器绕过、默认 `local` 后端、写不审批、
审批粒度粗、无出口管控。A/B 的答案不是"更强的正则"，而是**换一层**：

**(a) 规则即数据**（A：`security/tool_guard/rules/dangerous_shell_commands.yaml`，20 条）

```yaml
- id: TOOL_CMD_FS_DESTRUCTION
  tools: [bash, execute]
  params: [command]
  category: command_injection
  severity: CRITICAL
  patterns: ["\\bmkfs(\\.[a-zA-Z0-9_]+)?\\b", "\\bdd\\s+.*of=\\/dev\\/"]
  description: "Detects low-level disk formatting or wiping commands"
  remediation: "Block operation. Agents should not format or overwrite raw block devices."
```

比 C 的 `policy/rules.py` 多三样：**severity 分级**、**exclude_patterns**（避免 `# rm` 注释误报）、
**remediation**（给人和模型看的处置建议）。B 更进一步把这份 YAML 存进 DB
（`infra/agents/security/tool_guard_rules.py`），管理台可编辑——于是"加一条规则"不需要发版。

**(b) 粘性放行**（B：`infra/agents/security/hitl_session.py`，60 行）

```python
HitlSessionMode = Literal["ask", "allow_all", "allow_tools"]
# allows(): allow_all → True；allow_tools → tool_name in tools
# ask_user_question 永不被放行——问人这件事不能被"全部允许"吞掉
```

这正对 C"审批粒度粗、无永久允许 → 点击疲劳"的问题。C 现在的 `policy/extension.py` 只有
布尔 + 超时拒绝；加上 `allow_tools`（记住"本会话允许 bash 之外的某几个工具"）就能把
对话框数量降一个量级，而**不降低安全性**（危险命令仍由规则命中）。

**(c) 声明式策略对象**（A：`security/models.py`）

`SecurityPolicy` 一个数据类，`apply_to_config()` 一次性展开成 `interrupt_on` /
`FilesystemPermission` / PII 开关，`DEFAULT_HITL_TOOLS = (bash, execute, write_file, edit_file, delete)`、
`DEFAULT_SENSITIVE_PATHS = (/etc/**, /root/**, /**/.env, /**/id_rsa, ...)`。
C 的对应物是环境变量 + `PolicyConfig`，缺的是**一个可序列化、可存 DB、可按用户下发的对象**。
这一点对 B 那种"每个用户不同安全档位"的平台是必需的。

**(d) 把正则降级为"提示"**：A 的注释写得很清楚——
`HIGH/CRITICAL = 标记为不安全；真正的拦截由容器/审批流执行`。
C 应该照抄这个定位：**正则负责"值得问你一句"，容器负责"拦得住"**。

### 4.3 PII 脱敏 + SSRF 守卫（1–2 天）

**PII**（A：`middleware/pii.py`）：`PIIMiddleware` 自带的检测器**没有 API key**，A 补了一个
`detect_pii`，覆盖 `sk-` / `sk-ant-` / `AKIA` / `AIzaSy` / `hf_` / 中文手机号 / 18 位身份证（带 MOD 11-2 校验）。
配套策略是 `strategy="mask"`：留前缀和末 4 位。

对 C 的意义：团队模式是**一把共用 key**（README 写明），用户粘贴报错日志时把 key 带进
会话 → 写进 JSONL → 下一轮再发给模型。C 现在**完全没有这一层**（`packages/` 里 grep 不到 PII 脱敏或 SSRF 的实现，命中的 `redacted` 全是 thinking 块）。

**SSRF**（A：`security/ssrf.py`）：屏蔽 `localhost` / `*.local` / `*.internal` / `metadata.google.internal` /
私有网段 / link-local / 保留段。现在 C 没有 `web_fetch`，所以不急；**但一旦加 `web_fetch`
（DSH 分析 P1 第 12 项），这个文件必须先有**——否则 agent 能借它读云主机 metadata。

### 4.4 三个小工具（2–3 天）

| 工具 | 来源 | 要点 |
|---|---|---|
| `ask_user_question` | A：`builtin/tools/ask_user.py` | 最多 4 问 / 4 选项；**无 HITL 通道时返回一句"别再问、说明假设继续"**，绝不阻塞。C 已有扩展 dialog 通道，直接复用 |
| `send_file_to_user`（≈ DSH 的 `present`） | A：`builtin/tools/send_file.py` | 返回 `{type: image/audio/video/file, path, source.url}`；路径错误当**可恢复的 ToolException**，不许崩掉整轮 |
| `current_time` | A：`builtin/tools/current_time.py` | 模型没有时钟。C 现在没有任何时间工具，问"今天几号"它会编 |

`send_file` 这一个工具同时补上 DSH 分析里的"`present` 交付文件卡片"和"附件"两个缺口的前半段
（后半段是 UI 渲染）。

### 4.5 工具结果的「数据面 / 控制面」分离（3–5 天）

这是本文最值得抄的**设计**，出自 B 的 `docs/octop-ui-payload-offload.md`
（三方评审 APPROVE WITH CHANGES，带运行时 PoC）。

**问题**：一个 tool result 字符串同时服务四个消费者，而它们需求对立。

| 消费者 | 需求 |
|---|---|
| LLM 上下文 | 越小越好（会进 state，每轮全量携带） |
| 历史持久化 | 要完整（回放） |
| Dashboard 实时渲染 | 要完整（卡片要全部数据） |
| 插件 UI | 要完整（`props.data`） |

**方案**：在「工具执行后、`ToolMessage` 进 state 前」把 payload 剥离——控制面（摘要 + 渲染提示 + 定位符）
进 state 和历史索引，数据面落到 artifact 存储，UI 按定位符取。

对 C 的意义：DSH 分析 §2.1 的"结果溢出（spill）"缺口——B 这份文档就是**现成设计**，
而且 C 的场景更简单（单进程 + 已经能把 bash 输出落临时文件）。

**注意收敛**：这一件事在两个项目里已经有三种叫法——B 的 `octop_ui` 剥离约定、DSH 的 `ctx.spillStore`
（见 `GAP-ANALYSIS-vs-DSH.md` §2.1）、以及 A 的 `MediaOffloadMiddleware`。C 应该只选**定位符**一种
（`file://` 或 `spill://<id>`），让 spill / 媒体卸载 / 附件共用同一条回读路径，不要三套。

### 4.6 分层记忆：`USER.md` / `MEMORY.md` 会话快照（3–5 天）

A 的记忆模型有三个值得看的决定（`backends/workspace.py` + `middleware/memory*.py`）：

1. **记忆是工作区里的 md 文件**，不是数据库：`AGENTS.md`（项目规则）/ `MEMORY.md`（强记忆）/
   `USER.md`（用户画像）/ `SOUL.md`（人格）。随工作区一起走，用户可以直接看、直接改。
2. **每会话快照一次**：会话开始时把这些文件读进 system prompt 的缓存段，之后不再刷新；
   编辑只对新会话生效。这样 system prompt 稳定 → **prompt cache 能命中**（C 现在完全不发 `cache_control`，
   DSH 分析 §2.10 已指出）。
3. **结构化记忆开关**：打开时 agent 的文件工具对这两个文件**只读**，写入必须走 `memory_save` 工具，
   于是每次记忆写入可审计、可冲突检测（"proven conflicts retire the old atom"）。

C 的现状：只有 `AGENTS.md` / `CLAUDE.md` 上下文文件（`resource_loader.py`），
**没有用户画像、没有记忆写入通道**。

**建议只抄前两条 + 一个 `memory_save` 工具**，不要抄 octop-memory 的 L0→L3 蒸馏
（L0 原始事件 → L2 atom 卡片 → L3 实体页，还要异步 worker、冲突裁决、recall 快照）——
那是独立开源库的工作量（`octop-memory>=1.0.2`），而且 C 是 coding agent，不是长期生活助手。

落地形状：`<data-root>/agents/<uid>/USER.md` + `MEMORY.md`，注入 system prompt 的一个 section，
加 `memory_save` 工具（写文件 + 追加审计行），阈值到了截断。**一天能跑通，一周能打磨好。**

### 4.7 MCP 接入（5–7 天，附 4 个坑）

A 的 `mcp.py` + `mcp_client.py` 不长，但把该踩的坑都踩了。抄设计，不要抄 `langchain-mcp-adapters`：

| 坑 | A 的处理 | 对 C |
|---|---|---|
| 工具名不合规 | `sanitize_llm_tool_name`：`^[a-zA-Z0-9_-]+$`，其余替换成 `_` | 必抄 |
| JSON Schema → 参数模型 | 动态建 Pydantic 模型，**避开保留字段名**、数字开头前缀 `arg_`、去重尾部加 `_` | C 用的是手写 JSON Schema 校验，需要一层"远程 schema → 本地校验器"的适配；保留字冲突是真实 bug 源 |
| 共享 / 独占配置 | `merge_mcp_server_configs(shared, agent)`，**agent 覆盖 manager**；`mcp_default_servers` 只允许引用已配置的 server（启动即报错，不静默） | C 现在是 per-user 配置目录，等价物是"全局 + 用户级"两份 JSON 合并 |
| 会话必须在同一个 task 里 | `ManagedMCPClient`：**一个专用 task 拥有一个 session**，其他 task 通过队列投递（AnyIO 传输必须同 task 开关） | 这是最容易写出"偶发 hang"的地方；C 的 asyncio 结构一样会撞上 |

另外 A 把 MCP 工具错误单独做了中间件（`mcp_tool_error.py`），保证 MCP server 挂掉
不会让整轮失败——C 应该把这条写进工具执行管线的错误分类里。

### 4.8 知识库 RAG（8–12 天）

B 的实现（`src/octop/infra/knowledge/`，16 个模块）是这个规模下最省事的做法：

- **索引是 per-KB 的 SQLite sidecar**（`<kb>/index.sqlite`），一个查询一个连接，
  没有向量数据库、没有 pgvector 依赖。chunk 固定 800 字符 / 120 重叠，k=8，上下文预算 6000 字符——全部可配
  （`params.py`，带上下界校验）。
- **embedding 可换后端**：本地 ONNX 或任意 OpenAI 兼容端点（`embed.py`，远程按 ≤20 条分批保序）。
- **检索是"每轮注入目录 + 工具按需查"两段式**：`search_knowledge` 工具的 description 里
  动态拼上"本次挂载了哪些知识库 + 240 字符简介"（`tools.py`），模型先看到有什么，再决定搜不搜。
- **引用用 HTML 注释标记**：`<!--octop-kb-citations:[{kb_id,doc_id,filename,path}]-->` 附在 tool result 末尾，
  展示时用正则剥掉，前端单独渲染来源。**机器可读、人不可见、不需要改协议**——这个 trick 很值得抄。
- 文档解析覆盖 pdf/docx/pptx/xlsx/图片 OCR（依赖 `pypdf` / `python-docx` / `python-pptx` / `openpyxl` / OCR）。

对 C 的意义：团队内网部署 + 私有资料问答是"团队助手"最自然的需求，而 C 现在完全是 0。
成本主要在文档解析和权限（B 有 `MAX_DOCS_PER_KB = 100` / `MAX_BASES_PER_OWNER = 20` /
owner-or-shared 可见性；`retrieve.py` 还有"不可用或检索失败时静默跳过"的降级，不让知识库故障
拖垮整轮对话）。

**如果要砍**：先只做 md/txt/pdf，本地 ONNX embedding，不做 OCR、不做分享——能到 3–4 天。

### 4.9 定时任务 DSL（5–7 天）

B 的 `infra/cron/` 有三个设计决定值得抄：

1. **触发串是一种小 DSL**，模型直接写字符串：
   `cron:0 9 * * *` / `interval:3600` / `date:<ISO8601>` / `agently:<connector-id>`（外部事件源，永不挂钟）。
   解析在 `trigger.py`，还顺手把 Unix 数字星期翻译成名字（APScheduler 3 的坑）。
2. **任务类型二分**：`text`（prompt 原样推送，用于"喝水提醒/日报"）vs `agent`（每轮真跑 agent，
   用于"每天汇总新数据"）。这个区分省掉大量"提醒为什么又调了一次模型"的困惑。
3. **misfire 语义**：周期任务只容忍 60 秒停机；一次性任务容忍 24 小时，再判 miss（`RECURRING_MISFIRE_GRACE_SECONDS`）。

对 C：`schedule_create` 在 DSH 分析里是 P2（"团队定时汇报/巡检"）。抄 B 的话，
**解析器 + 两种任务类型 + misfire 语义**是核心，APScheduler 本身可以用，也可以自己写 100 行。

### 4.10 插件清单 + 管理端开关（2–3 天）

B 的 `plugin.yaml`（`plugins/demo-ui-card/plugin.yaml` 全文 9 行）：

```yaml
id: demo-ui-card
version: 0.1.0
name: Demo UI Card
description: Dual plugin — backend tool returns octop_ui JSON; frontend renders an interactive card
icon: "🃏"
kind: tool
entry: main.py
ui:
  entry: ui/dist/index.js
  manifest: ui/dist/manifest.json
```

三个特点：
- `kind` 限定能力面（`tool` / `skill` / `hook`），`setup(ctx)` 里用对应 API；
- `ui` 可选，**预构建 js**（安装时不跑 npm）；
- 内置插件随 wheel 发，`octop init` 时 seed 到 `~/.octop/plugins/` 并**默认全局关闭**；卸载过的 id 不再重装。

对 C：`setup(pi)` 已经有了，缺的是**能被列出来、能被开关、能带图标**的元数据。
这直接对应 DSH 分析里的"可治理单元"短板，而且是 C 里少数能"两天做完、立刻能用"的平台功能。

### 4.11 模型 preset + fallback + 每轮覆盖（3–5 天）

A 的三点：

- `ChatModelFactory` 按 `(provider, model_id)` 缓存实例，缓存键带 provider 维度——两个共用同一个 `base_url` 的 provider 不会撞车；
- **17 个可编辑 preset**（Hunyuan / Kimi / GLM / DeepSeek / MiniMax / Moonshot …），
  文件在 `providers/provider_template.json`，加厂商 = 加 JSON，不改代码；
- **每轮模型覆盖**：`middleware/turn_model.py` + `model_router.py`，按请求切换模型/套餐却不重建 agent。

对 C：DSH 分析 §2.10 已经指出 C 的痛点是"24 个模型里 12 个选中即报错"和"加厂商要改代码"。
A 的 preset 文件格式可以**直接借鉴形状**（C 已有 `models.json`，只差"每个 provider 一个模板 + 校验"）。

注意 A 的 `factory.py` 里有一个 C 可能缺的细节：`reasoning_content` 回放
（DeepSeek thinking 模型下一轮会 400）。C 的 DSH 分析说已实现"thinking 回放"，**这条已知覆盖，不用抄**。

### 4.12 上下文用量与媒体卸载（2–3 天）

- `middleware/context_usage.py`：每轮把"上下文窗口用了多少 / 剩多少"算出来，让 UI 和压缩决策有依据；
- `middleware/media_offload.py`：图片/视频超过阈值就卸载到存储，state 里只留定位符
  （和 §4.5 是同一个思路的另一个应用）。

C 有 `compaction.py`（阈值摘要 + keep_recent），但阈值是 `chars/4` 估算 + 最近一次 usage
（DSH 分析 §2.10 已指出"压缩触发偏粗"）。A 的"用量锚点 + 增量复算"更准，
但这是**打磨**，不是**缺口**——排在最后。

### 4.13 IM 通道（按需，5–8 天/平台）

B 的网关把 Web / IM / cron 三种入口归一成同一条管线（`infra/gateway/process/processor.py` 的 `GlobalProcessor`，README 称 `HarnessProcessor`），
通道适配器在独立包 `octop-gateway` 里，`ChannelKind` + `ChannelManager` + `WebSocketHub`，
飞书用官方 `lark-oapi`。C 的 WebSocket hub 已经是同构的东西（`Connection` + 广播），
**缺的只是"把外部消息变成一个 prompt"的适配层**。

但这条**不建议现在做**：C 是内网团队部署，浏览器已经覆盖 95% 场景；
而且 IM 通道会引入新的鉴权面（谁发的消息算谁的 uid），比看起来贵。

---

## 5. 不建议抄

| A / B 的东西 | 为什么不抄 |
|---|---|
| **LangChain / LangGraph / deepagents 作为核心** | 会同时摧毁 C 的两个资产：pi 会话互通、逐字对齐的事件语义。而且 C 的 12 事件流已经在跑 |
| **27 个中间件组成的链** | C 已有 21 个扩展事件；再叠一套 = 两套机制做同一件事。抄"清单"（横切关注点表），不抄"机制" |
| A 的 **8 种 backend 矩阵**（local/bwrap/docker/opensandbox/s3/cos/oss/postgres） | 那是**库**的正确答案（未知宿主）。C 是自托管单机平台，一种 Docker 沙箱做透更值钱 |
| B 的 **`history_v2` 分段归档** | 60+ 行启用/回退文档，本质是在补 checkpoint 模型的坑。C 的 JSONL 没有这个问题，只借"内容哈希共享大字段"一个小点 |
| B 的 **expert teams**（主持人 + 成员 + dispatch + 房间转播） | beta，且深度耦合通道与 `agents.kind=team`。C 需要的是 subagent（子会话 + 工具），不是"群聊里多个专家互相派工" |
| B 的 **远程桌面 / Browser AI+ / Terminal AI+** | 需要 `mss` / `playwright` / x11，运维面巨大，与 coding agent 的定位无关 |
| **MBTI 人格 / 云协作 / 专家市场 / 桌面端打包 / FnOS** | 产品取向完全不同，C 的用户是同事不是家人 |
| A 的 **L0–L3 分层纪律**写进目录结构 | 4.4 万行才需要的仪式；C 2.5 万行按 `pi-ai / agent-core / coding-agent / server` 分已经够 |
| B 的 **i18n（en/zh + domains）** | 团队内网中文单语。等真有海外用户再说，成本按 5 天算 |
| A 的 **`mypy --strict` 一步到位** | 2.5 万行一次性加 strict 注解 = 数周无功能产出。分模块渐进 |
| **Octop 的 60 个路由一一对应** | C 的 21 个端点覆盖了同样的人机路径；路由数量是产品面宽度，不是质量 |

---

## 6. 路线建议

工时按"熟悉本仓库的一名 Python 工程师"估算，含测试。

### P0：让它变成一个"可交付"的工程（约 1.5 周）

| # | 事项 | 工时 | 来自 | 理由 |
|---|---|---|---|---|
| 1 | ruff + `ruff format --check` + pre-commit | 0.5 d | A | 无设计成本 |
| 2 | 一个 CI workflow（uv sync → ruff → pytest） | 0.5 d | A | 让全部 588 个测试开始自动生效 |
| 3 | mypy 渐进接入（先 `pi-ai`，`per-module` 配置） | 1–2 d | A | 从最干净的包开始 |
| 4 | 规则即数据：`policy/rules.py` → YAML + severity/remediation | 1–2 d | A | 同时修误报与粒度 |
| 5 | 粘性放行（`ask`/`allow_all`/`allow_tools`） | 1 d | B | 直接降对话框数量 |
| 6 | PII 脱敏（mask 策略）+ 审计留痕 | 1 d | A | 共用 key 场景必需 |
| 7 | 三个小工具（ask_user / send_file / current_time） | 2–3 d | A | 复用现成 dialog 通道 |
| 8 | `CHANGELOG` / `CONTRIBUTING` / `SECURITY` | 0.5 d | A | 开源门面 |

### P1：补 agent 的能力面（约 4–6 周）

| # | 事项 | 工时 | 来自 | 前置 |
|---|---|---|---|---|
| 9 | 工具结果数据面/控制面分离（定位符 + artifact 存储） | 3–5 d | B | 无（直接改工具管线） |
| 10 | 分层记忆（`USER.md`/`MEMORY.md` + 快照 + `memory_save`） | 3–5 d | A | 无 |
| 11 | MCP client（stdio + HTTP，含 4 个坑） | 5–7 d | A | 需先有工具注册缝（DSH P1 #8） |
| 12 | 插件清单（`plugin.yaml` + 管理端开关） | 2–3 d | B | 无 |
| 13 | 模型 preset 文件 + 修 12 个坏模型链接 | 2–3 d | A | 无 |
| 14 | 知识库（md/txt/pdf + 本地 embedding + `search_knowledge`） | 3–4 d（精简版） | B | 需 §4.5 的 artifact 存储 |
| 15 | 定时任务 DSL | 5–7 d | B | 无 |

### P2：按需

IM 通道（先飞书，5–8 d）· 知识库完整版（OCR / 分享 / 引用 UI，再 +6 d）·
上下文用量可视化（2–3 d）· 媒体卸载（2 d）· ADR 补写（1 d）

### 与 DSH 分析的关系

DSH 分析的 P0（修坏模型链接 / todo / 后台作业 / ask_user / spill / 自动标题 / 附件 / cache_control）
**仍然是 P0**，本文不推翻它。本文新增的是**它没有的**：工程卫生、安全默认形态的**具体形状**、
PII/SSRF、数据面分离的**现成设计**、分层记忆、知识库、cron DSL、插件清单。

两边合并后的真实优先级：

```
第 1 周   CI + ruff + 安全（规则即数据 + 粘性放行 + PII）   ← 本文 P0，成本最低
第 2–3 周 DSH P0（模型链接 / ask_user / todo / 作业控制 / spill）  ← 用户感知最强
第 4 周   分层记忆 + 插件清单                              ← 平台感
第 5–8 周 MCP + 知识库 + cron                              ← 生态与场景
```

---

## 7. 怎么验证借鉴有效

1. **工程卫生**：把 CI 跑在**故意引入**的一个类型错误上，确认它挡住 PR——而不是"配置好了"。
2. **安全**：复用 C 自己的 `deploy/smoke.py` 思路，加三条断言——`python3 -c "import os;print(os.listdir('..'))"`
   必须被问；`allow_tools` 放行 `grep` 后仍要问 `bash`；粘贴一个 `sk-` 开头的串进会话，
   落盘的 JSONL 里必须是 `sk-4****5f2`。
3. **数据面分离**：让 bash `seq 1 200000`，确认 ① 模型侧看到的是摘要 + 定位符；
   ② Web 侧能展开全文；③ 会话 JSONL 不因这一条膨胀超过 1 MB。
4. **分层记忆**：同一用户开两个会话，第二个会话的 system prompt 里必须出现 `USER.md` 的内容，
   且**第一次会话中修改的 `USER.md` 不生效**（快照语义）。
5. **知识库**：问一个只有文档里才有答案的问题，确认 ① 展示层看不到 `<!--octop-kb-citations:...-->` 原文；
   ② 用户能看到来源文件名；③ 文件名**真实存在**（防幻觉引用）。
6. **MCP**：拿一个会返回非法工具名（含 `.` 或空格）的 server 测一遍，确认不因名字被模型 API 拒绝。

---

## 附录：本文引用到的关键文件

| 主题 | A：octop-harness | B：Octop |
|---|---|---|
| 工程卫生 | `Makefile`、`.github/workflows/ci.yml`、`pyproject.toml`（ruff + mypy） | `Makefile`、`.github/`、`docs/adr/` |
| 安全策略 | `security/models.py`、`security/tool_guard/rules/dangerous_shell_commands.yaml` | `infra/agents/security/hitl_session.py`、`tool_guard_rules.py`、`policy_store.py` |
| PII / SSRF | `middleware/pii.py`、`security/ssrf.py` | — |
| 小工具 | `builtin/tools/ask_user.py`、`send_file.py`、`current_time.py` | — |
| 数据面分离 | `middleware/media_offload.py`（媒体换定位符） | `docs/octop-ui-payload-offload.md` + `backends/workspace.py` 式的定位符回读 |
| 记忆 | `middleware/memory.py`、`memory_recall.py`、`backends/workspace.py`（`MEMORY.md`/`USER.md`/`SOUL.md`） | `infra/agents/memory/`、`docs/memory-slim.md` |
| MCP | `mcp.py`、`mcp_client.py`、`middleware/mcp_tool_error.py` | `infra/agent_mcp/`、`api/routers/agent_mcp.py` |
| 知识库 | — | `infra/knowledge/`（`index.py` / `retrieve.py` / `tools.py` / `citations.py` / `parse.py`） |
| 定时任务 | — | `infra/cron/`（`trigger.py` / `delivery.py` / `task_type.py` / `tools.py`） |
| 插件 | `middleware/`（另一种形态） | `plugins/README.md`、`plugins/demo-ui-card/plugin.yaml` |
| 模型路由 | `llm/factory.py`、`providers/provider_template.json`、`middleware/turn_model.py` | `infra/agents/providers/` |
| 会话历史 | LangGraph checkpoint | `docs/versioned-history.md` |
