# 团队自托管版设计（路线甲 · 一期）

> 目标：把 python-pi 从「单人单机助手」改造为公司内部团队自托管的多租户服务。
> 本文是改造的锚点：数据模型、鉴权、会话注册表、治理层、页面清单、部署拓扑与分期里程碑。

## 0. 现状与问题

`packages/pi-server/src/pi_server/app.py` 的模块注释把三条假设写得很直白，它们是本次改造要拆的东西：

| 假设 | 位置 | 后果 |
|---|---|---|
| 一个进程只有一个共享会话 | `app.py:11-14` | 多人无法各自对话 |
| 事件广播给所有客户端，对话框发给「最近活跃的那个」 | `app.py:87-107` | 用户之间互相看见、互相抢弹窗 |
| REST 端点报告唯一状态 | `app.py:264-267` | 无法按用户取状态 |

另外四个事实决定了改造成本：

| 事实 | 位置 | 影响 |
|---|---|---|
| 无任何认证、无用户概念 | 全仓 grep 无 auth/login/user | 要新增身份面 |
| `bash`/`write`/`edit` 以服务进程完整权限运行，无沙箱 | `extensions/types.py:15` | 团队共享机器时是 RCE 面 |
| `bash` 子进程环境 = `dict(os.environ)` | `bash.py:112` | 共用 key 会被任何用户用 `env` 读走 |
| 用户扩展以进程完整权限加载 | `extensions/loader.py` | per-uid `agent_dir` 会天然给每人一个 RCE 目录 |

同时有两个**有利于改造的现成 seam**，一期尽量复用它们而不是重写工具：

| Seam | 位置 | 用途 |
|---|---|---|
| `tool_call` / `tool_result` 事件 + `ToolCallEventResult.block` | `extensions/types.py:280`、`agent_session.py:796-830` | 工具白名单、危险命令闸门、审计，**无需改 `tools/*.py`** |
| `RpcMode` 把 `host.has_ui` 置为 `True` | `rpc_mode.py:244` | 审批弹窗可复用现有 `Dialog` UI |
| `SessionManager.list(cwd, agent_dir=...)` 已支持按 agent_dir 取列表 | `session_manager.py:1271` | 按 uid 分会话列表几乎白送，`app.py:248` 补传参数即可 |

## 1. 决策记录

| # | 决策 | 依据 |
|---|---|---|
| D1 | 形态是**公司内部自托管**，不对外收费 | 免掉支付层与公网安全等级要求 |
| D2 | 工作区按 **uid** 隔离（否决最初的「一份固定目录」） | MedBuddy 的 `_shared_workspace_host_path(uid)` 证明「共享」的边界应画在租户上；从「一份目录」到「per-uid」只差 `BootstrapOptions.cwd` 一个变量的位置 |
| D3 | **一把共用 key + 按人记账 + 配额封顶** | 需要用量记账与拦截，正是配额页的数据源 |
| D4 | 控制面走**单进程多租户**（对齐 MedBuddy），执行面一期只做**应用层收窄**，容器化留二期 | 目录按人分开后 80% 的踩踏风险已消失；docker.sock + 镜像是运维最重的一块，不该压在一期 |

### 参考实现：MedBuddy 的分层（调研结论归档）

MedBuddy 不是「每用户一个实例」，而是**单进程多租户 + 按会话临时沙盒**：

| 层 | 做法 | 证据 |
|---|---|---|
| 控制面 | 单 api 容器、单 uvicorn 进程（无 `--workers`），租户边界落在 `uid`，靠 PG 行所有权 | `docker-compose.yml:45-88`、`docker/nginx/default.conf:14` |
| 执行面 | 一个 **thread** 一个临时容器，key = `uid::file_thread::skills_thread`，空闲 120s 回收、exec 180s 超时，每沙盒独立 network | `sandbox/provider.py:24-35`、compose `SANDBOX_IDLE_TIMEOUT_SECONDS` |
| 沙盒挂载 | per-uid workspace(rw) + per-thread uploads/outputs(rw) + skills(ro)，`/home/gem` 是 tmpfs | `docker/sandbox_provisioner/app.py:540-552` |
| Agent 侧 | `ProvisionerSandboxBackend` 实现文件/执行，带可读/可写根白名单 + `..` 拒绝 | `backends/sandbox/backend.py` |
| 数据面 | PG 是 Run/Message/权限终态的 owner，Redis/ARQ 投递，MinIO 文件 | `backend/AGENTS.md` |
| 身份面 | JWT（`/api/auth/token`）+ `yxkey_` API key，角色 `superadmin/admin/user` | `server/utils/auth_middleware.py` |

**一期只抄它的控制面与身份面**，执行面用 `tool_call` 闸门代替容器；二期再补 `ProvisionerSandboxBackend` 的对应物。

## 2. 目标架构分层

| 层 | 一期 | 归属 |
|---|---|---|
| 身份面 | Cookie 会话 + 角色，服务端可撤销 | 新增 `pi_server/auth.py`、`db.py` |
| 控制面 | 多租户 `SessionRegistry`，key = `(uid, session_id)` | 改 `app.py` |
| 执行面 | 内置 policy 扩展：白名单 + 审批 + 审计；systemd 硬化 | 新增 `pi_coding_agent/policy/` |
| 数据面 | SQLite(WAL) + JSONL 会话文件 + per-uid 目录 | 新增 `pi_server/db.py` |
| 展现面 | React SPA 增页（登录/文件/用量/管理） | 改 `web/src` |

数据面刻意降级到 SQLite：内部 5–30 人、单机部署，引入 PG+Redis 是净负担。
**换 PG 的触发条件写死为：api 进程数 > 1，或用户数 > 50。**

## 3. 数据模型（SQLite）

```sql
-- 用户与额度
CREATE TABLE users (
  uid            TEXT PRIMARY KEY,          -- uuid4，租户边界
  username       TEXT NOT NULL UNIQUE,
  display_name   TEXT NOT NULL DEFAULT '',
  password_hash  TEXT NOT NULL,             -- scrypt$n$r$p$salt$hash
  role           TEXT NOT NULL DEFAULT 'user',   -- superadmin|admin|user
  status         TEXT NOT NULL DEFAULT 'active', -- active|disabled
  must_change_password INTEGER NOT NULL DEFAULT 0,
  monthly_token_quota  INTEGER,             -- NULL = 不限
  monthly_cost_quota   REAL,                -- 计价单位：模型成本
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  last_login_at  TEXT
);

-- 服务端会话（可撤销；不用 JWT，零依赖且能强制下线）
CREATE TABLE auth_sessions (
  token_hash   TEXT PRIMARY KEY,            -- sha256(token)，不存明文
  uid          TEXT NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  revoked_at   TEXT,
  user_agent   TEXT NOT NULL DEFAULT ''
);

-- 用量记账（配额的数据源）
CREATE TABLE usage_records (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL,
  uid          TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  provider     TEXT NOT NULL DEFAULT '',
  model_id     TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  cost         REAL NOT NULL DEFAULT 0,
  request_kind TEXT NOT NULL DEFAULT 'turn'   -- turn|compaction
);

-- 审计：每一次工具调用一行，放行/拦截/审批都记
CREATE TABLE audit_records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  uid           TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  tool_name     TEXT NOT NULL,
  tool_call_id  TEXT NOT NULL DEFAULT '',
  decision      TEXT NOT NULL,              -- allow|auto_allow|approved|denied|blocked
  reason        TEXT NOT NULL DEFAULT '',
  input_preview TEXT NOT NULL DEFAULT '',   -- 截断，避免把密钥写进审计
  is_error      INTEGER NOT NULL DEFAULT 0,
  duration_ms   INTEGER,
  exit_code     INTEGER
);

CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE INDEX idx_usage_uid_ts ON usage_records(uid, ts);
CREATE INDEX idx_audit_uid_ts ON audit_records(uid, ts);
```

**会话内容不进库**：`<agent_dir>/sessions/**/*.jsonl` 保持 pi 的 v3 格式，这是与上游互通的资产，不动。

## 4. 目录布局

```
$PI_DATA_ROOT/                        # 默认 /var/lib/pi（systemd 里是 ReadWritePaths）
  pi.db                               # SQLite，WAL
  workspaces/<uid>/                   # agent 的 cwd —— per-uid 工作区（D2）
  agents/<uid>/                       # 原 agent_dir
    sessions/--<encoded-cwd>--/*.jsonl
    settings.json  models.json        # 可写
    skills/                           # 只读挂载或首次复制
    extensions/                       # 存在但不加载（见 §6）
  secrets/api_key                     # 0600，systemd LoadCredential，不进 argv/env
```

`cwd` 与 `agent_dir` 都从 `BootstrapOptions` 注入（`bootstrap.py:243-254` 已经在用这两个值），
所以「per-uid」在这层只是把常量换成 `f"{root}/workspaces/{uid}"`。

## 5. 身份与鉴权

- **密码哈希**：`hashlib.scrypt` + `hmac.compare_digest`，标准库，不引 passlib/bcrypt。
- **会话令牌**：`secrets.token_urlsafe(32)`，只在 Set-Cookie 里出现一次，库里存 `sha256`。
  Cookie 属性：`HttpOnly; SameSite=Lax; Path=/`，生产加 `Secure`。
- **为什么不用 JWT**：零依赖、能立即撤销（禁用用户/强制下线）、不需要刷新令牌逻辑。
- **WebSocket 鉴权**：浏览器握手是同源请求，Cookie 会自动带上，无需 query token
  （query token 会进 access log，是常见泄漏点）。服务端在 `websocket_endpoint` 里
  用同一个依赖解析用户，失败则 `close(code=4401)`。
- **角色**：`superadmin`（可改角色/配额）、`admin`（可建号/禁用/看审计）、`user`。
- **管理员初始化**：首次启动若无任何用户，生成随机初始密码并打到 stdout 一次（或读
  `PI_ADMIN_PASSWORD`），同时置 `must_change_password=1`。绝不硬编码默认密码。
- **API key**（可选，二期）：给 RPC/脚本客户端用，前缀 `pip_`，与 MedBuddy 的 `yxkey_` 同构。

## 6. 治理层（一期隔离）

### 6.1 强制加载的内置 policy 扩展

新增 `pi_coding_agent/policy/`，作为**内置扩展由服务端强制加载**，用户不可关闭。
同时**团队模式下拒绝加载 `agents/<uid>/extensions/` 与项目 `.pi/extensions/`**：
per-uid 的扩展目录等于给每个同事一个以服务进程权限运行的 RCE 入口（`extensions/types.py:15`），
必须在一期就封死。`--no-extensions` 这个现有开关不够用，它会把 policy 扩展一起关掉，
所以要在 `BootstrapOptions` 上区分「内置扩展」与「用户扩展」两类来源。

### 6.2 工具策略矩阵

| 工具 | 默认 | 条件 |
|---|---|---|
| `read` `ls` `grep` `find` | 放行 | 解析后的真实路径必须落在工作区内，越界拒绝（复用 `path_utils.resolve_path`，加 realpath 校验防符号链接逃逸） |
| `write` `edit` | 放行 | 同上 |
| `bash` | 分级 | 见下 |
| 未列出 / 扩展注册的工具 | 拒绝 | 管理员白名单显式放行 |

`bash` 三档：

1. **自动放行**：只读且无副作用的命令（`ls`、`cat`、`git status|diff|log`、`rg`、`pytest` 等）。
2. **需审批**：可能修改工作区或联网（`git commit|push`、`mv`、`cp`、`rm`（工作区内）、`pip install`）。
   走 `ctx.confirm`，弹窗进用户自己的标签页（依赖 §7 的按用户路由）。
3. **硬拒绝**：`sudo`、`su`、`docker`/`podman`、`systemctl`、`mount`、`crontab`、
   `curl|wget … | sh`、写 `/etc`、`/root`、`$PI_DATA_ROOT` 之外的系统路径、
   读 `~/.pi`、`env`/`printenv` 类环境转储、`chmod 777 /`、fork bomb。

### 6.3 子进程环境净化（必修）

`build_shell_env`（`bash.py:106-120`）现在把整个 `os.environ` 交给子进程。改为**白名单构造**：
只保留 `PATH`/`HOME`/`LANG`/`TZ`/`TERM` + `PI_*` 工作区变量，并显式剔除
`*_KEY`/`*_TOKEN`/`*_SECRET`/`*_PASSWORD` 与 `AWS_*`。同时共用 key 不再走 `--api-key`
（`ps aux` 可见，`cli.py:28`），改由 systemd credential 注入并只保存在进程内存里。

### 6.4 审计

用 `tool_result` 事件（`agent_session.py:800`）落 `audit_records`，与 `tool_call` 的决策配对。
`input_preview` 截断到 2KB 且过一遍密钥模式遮蔽。

### 6.5 明确写出：这不是沙盒

白名单+审批防的是**误操作**，防不住**恶意**。真实的边界是二期的容器。
这句话要同时进文档、代码注释和 UI 提示，不给同事虚假安全感。

## 7. 多租户会话注册表

`AgentServer` 拆成 `SessionRegistry`：

```
SessionKey = (uid, session_id)
SessionWorker = { options(per-uid cwd/agent_dir), AgentSession, RpcMode, Hub(users of this session) }
```

| 关注点 | 设计 |
|---|---|
| 启动 | 懒启动：第一次 `prompt`/`open` 才 `bootstrap(per-uid options)`，服务绑定瞬间完成 |
| 回收 | 空闲回收（默认 30min 无活动）释放 `AgentSession`；会话文件路径先写进 `session_pointers`，切回时按指针 `resume`，因此回收是无损的 |
| 并发 | **每用户一个活跃 worker**（同一用户的多个标签页共享同一会话，与改造前行为一致）；全进程上限默认 20；超限返回明确错误而不是排队静默。多 slot（同一用户并发多个会话）延后到确有需要时再加，因为它需要按 session 寻址与路径校验 |
| 对话框路由 | 从「全局最近活跃」改为「**该会话的**最近活跃标签页」；每个 worker 持有自己的 Hub，这是路由安全的实现方式 |
| 事件广播 | 只广播给订阅了该 session 的连接 |
| REST 作用域 | `/api/state`、`/api/sessions`、`/api/models` 全部按当前用户取；`/api/sessions` 传该用户的 `agent_dir` |
| 凭据 | 共用 key 由 `ServerConfig.api_key` 注入每个用户的 bootstrap；per-user `auth.json` 不存在也无法覆盖 |
| 目录模板 | 新用户的 agent 目录从 `<data_root>/template` 播种：复制 `models.json`/`settings.json`/`AGENTS.md`，`skills/` 用符号链接共享。**不复制 `auth.json`** |
| 故障隔离 | 一个 worker 的启动失败（`bootstrap` 用 `SystemExit` 报错，不是 `Exception`）被转换成 `BootstrapError`，否则会击穿请求处理；HTTP 返回 503，WS 发一帧错误再关闭 |

**单 worker 是硬约束**：会话状态在进程内，`uvicorn --workers 1`。要横向扩展就得把
registry 换成 Redis 广播 + 无状态网关（届时一并换 PG）。

## 8. 用量与配额

- **记账时机**：`message_end`/`turn_end` 上取累计 `usage` 与 `pi_ai` 的成本计算，逐轮写
  `usage_records`；压缩（compaction）单独记为 `request_kind='compaction'`。
- **配额校验点**：`prompt` 命令的 preflight（`RpcMode.handle_line` 已在此阶段回响应），
  超限直接返回失败事件并给出人话原因，而不是让用户等模型跑完才发现。
- **月度口径**：自然月，按 `ts` 聚合 `SUM(cost)`/`SUM(input+output)`；`NULL` 配额表示不限。
- **软告警**：用到 80% 时在 UI 顶部给提示，不阻断。

## 9. 页面清单

现状是**单页、零路由**（`web/src/main.tsx` 直接 render `<App/>`，依赖只有
react/react-dom/marked/dompurify）。一期加一个约 30 行的 hash 路由，不引 react-router，
符合本仓「依赖极简」的调性。

**新增**

| 页面 | 内容 | 优先级 |
|---|---|---|
| `/login` | 账号密码、首次强制改密；未认证时唯一可达 | P0 |
| `/files` | per-uid 工作区：树、预览、上传/下载、新建目录、删除 | P0 |
| `/usage` | 本月 token/成本、剩余额度、按会话与按天明细 | P1 |
| `/admin/members` | 建号/禁用/重置密码/角色/配额 | P1 |
| `/admin/usage` | 全局用量排行 + 调额 | P1 |
| `/admin/audit` | 工具调用、命令原文、决策（放行/拦截/审批人）、时间，可导出 | P1 |

**改造**

| 组件 | 改什么 |
|---|---|
| `lib/useAgent.ts`（1115 行） | WS 生命周期从「全局单会话」改为「绑定当前会话」；`ready`/`get_state` 按会话；未认证 401 时跳登录 |
| `components/Sidebar.tsx` | 会话增删改查+搜索；用户菜单与登出；模型选择受管理员白名单约束 |
| `components/Dialog.tsx` | 危险命令审批：展示命令原文、批准/拒绝/本会话记住 |
| `App.tsx` | 路由出口、用户菜单、额度角标 |
| `styles.css` | 登录/管理页需要一套中性表单样式（现只有深色工作台） |

## 10. 部署拓扑

```
Caddy :443 (自动证书)
   └── reverse_proxy 127.0.0.1:8787
         └── uvicorn --workers 1  pi_server.cli     [systemd: User=pi]
                └── $PI_DATA_ROOT (SQLite WAL + workspaces/<uid> + agents/<uid>)
```

systemd 硬化（一期就上，成本极低、收益真实）：

```
User=pi  Group=pi
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/pi
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LoadCredential=api_key:/etc/pi/api_key
MemoryMax=4G  CPUQuota=200%
```

- **备份**：`sqlite3 .backup`（不要直接拷 WAL 文件）+ 工作区/会话目录的 rsync 快照。
  建议工作区本身是 git 仓库，每轮 prompt 前后自动快照 → 免费获得「回滚到上一轮」的能力，
  这也是对「无法真正隔离」的补偿手段。
- **不挂 docker.sock**（那只属于二期 provisioner）。
- **日志**：jsonl 到 stderr 交给 journald；访问日志记 uid/session/命令摘要，不记密钥。

## 11. 里程碑

| # | 交付 | 验收 |
|---|---|---|
| M0 | SQLite + 迁移框架 + 用户/会话/用量/审计仓储 + scrypt 密码 + Cookie 鉴权依赖 + 管理员初始化 | 单测：建号/登录/越权/登出/禁用即失效 |
| M1 | `SessionRegistry`：per-uid cwd/agent_dir、懒启动、空闲回收、并发上限、按会话路由 | 两个用户同时跑，互不可见；A 的弹窗不进 B 的页面 |
| M2 | 工作区文件 API（`workspace.py` 的路径规则 + `/api/workspace/*`） | 跨 uid 路径访问被拒；`..`、绝对路径、符号链接逃逸全部 400 |
| M3 | policy 扩展：白名单+审批+审计；禁用用户扩展；子进程环境净化；systemd 硬化 | 越界路径被拒；`sudo` 被拒；环境里搜不到 key；审计有对应行 |
| M4 | 用量记账 + 配额拦截 + `/usage` + `/admin/*` | 配额耗尽时 `prompt` 立即被拒；账目与模型返回的 usage 一致 |
| M5 | 前端路由 + 登录页 + `/files` 工作区页 + 用户菜单 + 管理页 | 未登录全站重定向；刷新保持登录；管理员能建号 |
| M6 | 部署脚本 + 备份/迁移 + 冒烟测试 | 新机器一条命令起服务；回滚演练通过 |

顺序理由：M0 先于一切（没有身份就没有多租户）；M1 是核心改造；M2 是可用性前提
（用户得能把文件放进去）；M3/M4 是治理与钱；M5 是门面；M6 是交付。

**一处与初版的偏离**：`/files` 页面从 M2 挪到 M5。M2 只交付 API——在没有登录页之前，
一个需要登录才能打开的文件页谁也到不了，先做它只会得到一段无法验证的 UI。

## 12. 二期口子：执行面 Backend protocol

一期**定义协议、不迁移实现**，避免 `tools/*.py` 继续各自直连本地 FS：

```
SandboxBackend（Protocol）
  exec(command, cwd, timeout, env) -> ExecResult
  read / write / edit / ls / grep / find / glob
```

- 一期：只落地 `LocalBackend`（把现有行为包一层），并在新工具上强制走协议。
- 二期：新增 `ProvisionerBackend`（HTTP 调 provisioner），复用 MedBuddy 的
  `sandbox_id_for_thread(uid, thread)` 键法、空闲回收（120s）、exec 超时（180s）、
  挂载布局（per-uid workspace rw / per-thread uploads+outputs rw / skills ro）。
- 迁移完成前，`tools/*.py` 与 `backend` 双轨，但**新代码不得再直连 FS**。

## 13. 非目标（一期明确不做）

支付与计费结算、自助注册、组织/部门层级、对象存储、知识库与 RAG、微服务拆分、K8s、
跨语言（TS）扩展兼容、把会话文件从 JSONL 迁进数据库。

## 14. 风险与已知限制

| 风险 | 现状 | 缓解 |
|---|---|---|
| 一期**没有真正的沙盒**，恶意用户可越权 | 白名单是 deterrence 不是 containment | 明说；systemd 硬化收窄爆炸半径；二期容器 |
| 单进程故障域，一个失控会话可能拖垮全体 | `app.py` 单 registry | 并发上限、会话级 try/except、MemoryMax、熔断 |
| SQLite 单写者 | 本设计选型 | WAL + busy_timeout；进程数 >1 或人数 >50 换 PG |
| 共用 key 的滥用面 | D3 选型 | 配额封顶 + 审计；环境净化防止直接读取 |
| pi 会话文件与团队存储双写不一致 | JSONL 是权威 | 不做双写：会话只在磁盘，库里只存元数据与用量 |
