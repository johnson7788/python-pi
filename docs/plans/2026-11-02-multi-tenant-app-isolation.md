# 工作台多租户能力隔离：可行性设计与改造清单

> 输入：`WorkBuddy_Development_Guide.docx`（Buddy 应用 / 工作模式 / 场景胶囊 / Skill / Expert / Connector）
> 目标：单系统（单容器）多租户下，切换应用、技能、插件时互不影响。
> 结论：**逻辑隔离可以做到；代码级安全隔离取决于“插件是否允许跑第三方代码”。**

---

## 1. WorkBuddy 的应用模型

官方把 Buddy 应用定义为“行业 Harness / 工作台外壳”，它本身是**配置**，不是代码：

| 维度 | 内容 | 运行期作用 |
|---|---|---|
| 应用身份与授权 | app id、头像、名称、OAuth 回调、可信 Origin | 入口与租户授权 |
| 工作模式 | System Prompt + 预绑定 Skill（建议 2–4 个） | 模式级 Harness，持续生效 |
| 场景胶囊 | 输入框上方的快捷入口，可绑定 Expert / Skill / 案例 | 场景级 Harness，叠加在模式之上 |
| 模型配置 | 模型池、排序、默认模型 | 限制与默认选择 |
| 行业市场 | 展示哪些 Expert / Skill / Connector | 可见性与可安装范围 |
| 内嵌功能 | MCP Apps / iframe | 复用已有系统 |

四类资产的边界也很清楚：

- **Skill**：`SKILL.md` + references/scripts/templates，是方法、规则、输出格式（数据，不是服务）。
- **Expert**：`.codebuddy-plugin/plugin.json` + `agents/*.md` + 头像 + 可选 skills / MCP 依赖，本质是**角色提示词包**，工具由平台分配。
- **Connector**：MCP（HTTP/stdio）或 CLI，是真正执行外部动作的进程。
- **Buddy 应用**：引用上述资产，组合成工作台。

这个模型对多租户是**天然友好**的：应用、模式、胶囊、技能、专家都是声明式配置；
只有 Connector 和代码插件涉及执行，也正是隔离要重点处理的地方。

---

## 2. “互不影响”分五层，不是一件事

| 层级 | 含义 | 单容器多租户能否做到 | 关键手段 |
|---|---|---|---|
| L1 展示/上下文 | A 应用的模式、技能、提示词不会出现在 B 的会话里 | ✅ 可以 | 每会话解析并冻结 Composition |
| L2 数据 | 工作空间、会话、产物、定时任务按租户/应用隔离 | ✅ 可以 | 现有 per-uid 目录 + 新表按 tenant/app 建键 |
| L3 凭据 | Connector Token、OAuth 不共享 | ✅ 可以 | 租户级凭据表 + 引用解析，禁止写进包 |
| L4 工具执行 | A 的脚本/CLI 不能读 B 的文件、不能越权 | 🟡 部分 | 现有 Docker sandbox（仅 bash）+ MCP 独立进程 |
| L5 插件代码 | A 的插件崩溃/死循环/读文件不影响 B | ❌ 进程内做不到 | 需要 per-tenant 或 per-plugin 进程/容器/沙箱 |

**真正决定“能不能”的是 L5。**
如果插件是平台自己维护、随容器一起发布的（租户只能启用/停用），
那么 L1–L4 + 同一版本 + 进程内过滤就足够，单容器完全可行。
如果租户能上传任意 Python 代码并要“互不影响”，那就不是配置问题，而是安全边界问题：
单进程内无法隔离，需要子进程/容器，甚至 gVisor/Firecracker 级别的沙箱。

---

## 3. 建议的目标模型：Composition（会话级能力快照）

不要把“当前应用/当前模式”做成进程级全局状态；把它做成**创建会话时解析出来的一份不可变 Composition**：

```
租户 (uid)
  └── Buddy 应用 (app_id, version)
        └── 工作模式 (mode_id) + 场景胶囊 (capsule_id)
              └── Composition (resolved, immutable)
                    ├── prompt sections（模式 + 胶囊叠加）
                    ├── skills[]（只含本应用允许的）
                    ├── experts[]（角色提示词）
                    ├── connectors[]（MCP/CLI + 凭据引用）
                    ├── tools allowlist（基础工具 + connector 工具 + 插件工具）
                    ├── model policy
                    └── permissions / quotas
```

要点：

1. **会话创建时解析一次**，把 `app_id / mode_id / capsule_id / composition_hash` 记到会话上；
   应用配置更新**不影响已运行的会话**，下一个会话才用新版本——这就是“切换应用不互相影响”的核心。
2. **切换应用 = 新建会话**，不是改一个全局变量。旧会话保留原 Composition，可回看。
3. Composition 里的技能、插件、工具都是**引用**，不是拷贝代码；运行时只加载被引用的部分。
4. 任何“全局注册表”都要变成“按 Composition 解析的注册表”。

---

## 4. 映射到 python-pi：现在有什么、缺什么

| WorkBuddy 资产 | python-pi 现状 | 差距 |
|---|---|---|
| Buddy 应用 | 无 app 表、无 app 路由、无会话 app 归属 | ❌ 全缺 |
| 工作模式 | 每个 session 一个 system prompt，构造时生成 | 🟡 有 prompt 机制，无“模式”概念与按模式绑定技能 |
| 场景胶囊 | 无 | ❌ 全缺 |
| Skill | `SKILL.md` 发现、frontmatter 校验、提示词注入、`/skill` 展开、`get_commands` | 🟡 缺 display/version/author/allowed-tools/examples；缺按应用/租户过滤与启用 |
| Expert | 无 `plugin.json` / `agents/*.md`；但有 subagent（spawn/fork） | 🟡 可用 prompt + 工具白名单 + subagent 模拟，缺包格式与市场信息 |
| Connector | 无 MCP client；有 bash/CLI + Docker sandbox | 🟡 CLI 可先用 Skill 说明 + bash；MCP 需新建；凭据注入没有 |
| 插件（DSH/Codex 式代码扩展） | Python 扩展：21 事件、工具/命令/flag/provider、entry point；启动时加载 | 🟡 运行时在，治理/隔离/生命周期全缺 |

### 当前代码里会直接挡住隔离的地方

1. **扩展启动时一次性加载，模块进 `sys.modules`**
   `bootstrap.py:311` → `load_extension_runner()` → `load_extensions()`；
   `ExtensionRunner` 一个 bootstrap 共享给子 agent。
   → 同一进程里不能为两个租户加载同一插件的不同版本，也没有“仅对本会话启用”的机制。
2. **Skill 是每个 worker 的 ResourceLoader 固定快照**
   `ResourceLoader(cwd, agent_dir, skills_enabled=...)`；
   只有全局 `no_skills` 开关，没有按 app/租户选择技能。
3. **System prompt 在 AgentSession 构造时组装**
   技能块、工具说明、扩展段落都在其中；没有“应用/模式”输入。
4. **一个用户一个 SessionWorker，cwd 固定**
   `config.py:158` 的 `workspace_dir(uid)`；
   会话按 cwd 分目录（`session_manager.py:1271`），这反而是多工作空间的现成基础。
5. **团队模式直接禁用用户扩展**
   `app.py:545` `no_extensions=not allow_user_extensions`；
   平台预装插件是唯一现实路径。
6. **没有 app/mode/capsule/plugin 表与 API**
   服务端只有会话、文件、用量、管理四组路由。

---

## 5. 单容器多租户的推荐架构

### 5.1 三类资产用三种隔离策略

| 资产 | 形态 | 推荐隔离 |
|---|---|---|
| 应用 / 模式 / 胶囊 / Skill / Expert | 声明式 JSON/Markdown | 进程内按 Composition 解析；数据/提示词天然隔离 |
| Connector | MCP Server / CLI 进程 | 每租户独立连接/进程 + 租户凭据；MCP 本身在进程外，最适合做隔离边界 |
| 代码插件 | 可执行 Python/JS | 平台预装 + 租户级启用（v1）；需要强隔离时 per-tenant 子进程/容器（v2） |

### 5.2 运行时组装

```
HTTP/WS 请求
  → 解析 tenant / app / mode / capsule
  → CompositionResolver（校验归属、合并模式+胶囊、过滤可见资产）
  → 新建 AgentSession（冻结 Composition）
      ├── prompt: 基础 + 模式 + 胶囊 + 技能目录
      ├── tools: 基础工具 ∩ allowlist + connector tools + plugin tools(enabled)
      ├── skills: 仅引用列表（ResourceLoader 加 selector）
      └── credentials: 仅本租户引用
```

### 5.3 插件启用过滤，而不是热插拔

v1 不做“运行时加载/卸载任意插件”，只做：

- 进程启动时加载**平台允许的插件集合**（同一版本）；
- 每个插件有 manifest（id/version/permissions/entry）；
- 每个租户/应用有一张 `plugin_enable` 表；
- 构造会话时，只把 enabled 的插件工具/命令/钩子挂到该会话；
- 插件内部的全局副作用（如全局变量、后台线程）必须在 manifest 里标为 `singleton`，
  不允许按租户差异使用；否则就升级为独立进程。

这样能保证“行为不互相影响”，但**不能保证崩溃隔离**。v1 的承诺要写清楚。

---

## 6. 数据模型（建议）

```sql
-- 应用与配置
apps(id, tenant_id, name, icon, status, version, config_json, created_at, updated_at)
app_modes(id, app_id, name, prompt, skill_ids_json, model_policy_json, sort)
app_capsules(id, app_id, name, prompt, skill_ids_json, expert_ids_json, sort)

-- 资产
skills(id, tenant_id, app_id NULL, name, display_name, version, path, meta_json, enabled)
experts(id, tenant_id, app_id NULL, name, version, plugin_json, enabled)
connectors(id, tenant_id, type, config_json, secret_ref, enabled)
plugins(id, name, version, manifest_json, permissions_json, enabled)

-- 关系与状态
app_assets(app_id, asset_type, asset_id, version)
plugin_enable(tenant_id, plugin_id, enabled)
credentials(tenant_id, kind, name, ciphertext, created_at)

-- 会话归属（也可写入 JSONL custom entry，DB 更好查）
session_app(session_id, tenant_id, app_id, mode_id, capsule_id, composition_hash)
```

原则：

- 所有业务表第一列 `tenant_id`，查询必须带租户条件；
- 资产可以被多个应用引用，但**版本要锁定**（`app_assets.version`）；
- 会话记录 `composition_hash`，便于审计“这轮到底用了哪套配置”。

---

## 7. API 草图

```
# 应用
GET    /api/apps                      当前租户可见应用
POST   /api/apps                      创建（管理员/开发者）
GET    /api/apps/{id}                 详情（modes/capsules/assets）
POST   /api/apps/{id}/sessions        以 app+mode+capsule 新建会话（核心）
PATCH  /api/apps/{id}                 更新（新版本，不影响已运行会话）

# 技能
GET    /api/skills?app_id=            可见技能
PATCH  /api/skills/{id}               enabled 切换（租户/应用范围）
GET    /api/skills/{id}/content       详情/预览

# 专家
GET    /api/experts
POST   /api/experts/{id}/sessions     召唤专家（等价于新建带 Expert 的会话）

# 连接器
GET    /api/connectors
POST   /api/connectors/{id}/bind      租户凭据绑定（OAuth/Token）
POST   /api/connectors/{id}/test

# 插件
GET    /api/plugins                   已安装（平台预装）
PATCH  /api/plugins/{id}/enable       启用/停用（租户级）
```

关键设计：**`POST /api/apps/{id}/sessions` 是唯一入口**，前端切应用、切模式、点胶囊都走它；
不要提供“全局切换当前应用”的接口。

---

## 8. 分期与工时

| 阶段 | 交付 | 依赖 | 估算 |
|---|---|---|---|
| P0 | Composition 模型 + `apps/modes/capsules` 表 + 新建会话 API（先 1 个内置应用） | 无 | 6–9 人日 |
| P0 | 会话级技能选择（ResourceLoader selector + prompt 重组）+ 工具 allowlist | P0 | 4–6 人日 |
| P0 | 应用切换前端（模式/胶囊/会话归属） | P0 | 5–7 人日 |
| P1 | 技能管理 API + 技能页（列表/详情/启停） | P0 | 4–6 人日 |
| P1 | Expert 包（plugin.json + agent md）+ 召唤入口 | P0 | 5–7 人日 |
| P2 | Connector：MCP client（stdio/HTTP）+ 租户凭据 + 工具注入 | P0 | 8–12 人日 |
| P2 | 代码插件：manifest + 启用过滤 + 治理页 | P0 | 8–12 人日 |
| P3 | 强隔离：per-tenant / per-plugin 子进程或容器 + 资源限额 | P2 | 10–20 人日 |
| P4 | 定时任务/产物等（见后端梳理文档） | P0/P1 | 另计 |

测试重点（必须从 P0 就有）：

- 两个租户、两个应用，同一时间运行：A 的提示词/技能/工具不出现在 B；
- 同一租户两个应用切换：旧会话保持旧 Composition，新会话用新配置；
- 应用更新版本：已运行会话不受影响；
- 禁用插件后：新会话不再出现其工具，旧会话行为可复现；
- 越权引用：A 引用 B 的私有技能/连接器必须 404，而不是静默加载；
- 资源耗尽：一个租户的 subagent/连接器不拖垮整个进程（并发/超时/内存）。

---

## 9. 已确认前提与结论（2026-11-03）

- 插件 = 可执行代码插件，**由使用者用 python-pi 自己的 Python SDK 开发**；不做 DSH/Codex 兼容。
- 租户 = **内部同事**；插件由同事开发、管理员安装（不引入 DSH 插件）。
- **不需要**同一租户/不同应用使用不同插件版本。

这三点确认后：

- 单进程不必解决“同名插件多版本”，`sys.modules` 冲突的问题消失；
- 但仍需要 **插件包 manifest、上传审核、每会话启用、独立 host 进程**；
- “插件绝对读不到其他租户数据”在单进程/同 UID 下做不到，需要每租户 host + 独立 uid 或容器。

插件运行的详细设计见 [`2026-11-03-python-plugin-runtime.md`](2026-11-03-python-plugin-runtime.md)
（2026-11-03 进一步确定：不做 DSH 兼容，插件改用 python-pi 原生 Python SDK）。

---

## 10. 结论与需要你确认的问题

**结论：可以，但要先定隔离承诺。**

- 如果“互不影响”指**提示词/技能/应用配置/数据**：单容器完全可行，按 Composition 解析即可。
- 如果指**插件崩溃/越权不影响别人**：单进程不行，必须把代码插件放到独立进程/容器；
  单容器内可以用子进程做到“崩溃隔离”，但“安全隔离”仍依赖 OS 沙箱。

需要确认：

1. 租户是内部同事，还是外部客户？（决定要不要 L5）
2. 应用/技能/插件由平台维护，还是租户可自行上传？（决定代码执行边界）
3. 你说的“插件”是 WorkBuddy 的 **Expert（提示词包）**，还是 DSH/Codex 式的**可执行代码插件**？
   两者隔离成本差一个数量级。
4. 是否需要“同一租户不同应用使用不同插件版本”？（决定能不能单进程）
5. 切换应用是“新建会话”还是“同一会话内切换”？（推荐前者）
6. Connector 只做 MCP，还是 CLI 也要支持？

我的建议：**v1 把应用/技能/专家做成声明式 Composition，插件只做平台预装 + 租户启用，
Connector 走 MCP 独立进程**。这样单容器多租户可以安全上线；等确有第三方代码插件需求，
再把插件宿主拆成独立进程/容器，而不是现在就为它把整个运行时复杂化。
