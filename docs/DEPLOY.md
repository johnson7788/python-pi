# 团队自托管部署

面向运维：把 python-pi 部署成内部团队共用的服务。
设计依据与取舍见 [`plans/2026-09-14-team-saas-design.md`](plans/2026-09-14-team-saas-design.md)。

## 形态

```
浏览器 ──HTTPS──▶ Caddy ──127.0.0.1:8787──▶ uvicorn（单进程，--workers 1）
                                              └── /var/lib/pi
                                                    pi.db              账号/用量/审计
                                                    workspaces/<uid>/  用户的工作区（agent 的 cwd）
                                                    agents/<uid>/      会话 JSONL、settings、models
```

**单进程是硬约束**：会话状态在进程内存里，跑多 worker 会出现「同一用户的两次请求落到不同进程、看到不同会话」。要横向扩展就得先把注册表换成外部状态（届时一并换 PostgreSQL），见设计文档 §7。

## 前置条件

| 需求 | 说明 |
|---|---|
| Python ≥ 3.11 + [uv](https://docs.astral.sh/uv/) | 运行时依赖只有 httpx 与 pyyaml |
| Node.js（仅构建期） | 构建前端 SPA |
| Caddy 2 | TLS 终止与反向代理 |
| 一个 OpenAI 兼容的模型端点与 key | 默认走阿里云百炼的 `deepseek/deepseek-v4-flash` |

## 安装

```bash
# 1. 代码与服务账号
sudo useradd --system --home /var/lib/pi --shell /usr/sbin/nologin pi
sudo git clone <repo> /opt/python-pi && cd /opt/python-pi
sudo -u pi uv sync

# 2. 构建前端（服务器只在 web/dist/index.html 存在时托管 SPA）
cd web && npm ci && npm run build && cd ..

# 3. 数据目录
sudo install -d -m 0750 -o pi -g pi /var/lib/pi
sudo install -d -m 0750 -o pi -g pi /var/lib/pi/template

# 4. 模型目录模板 —— 每个新用户的 agent 目录从这里播种
#    如果你的 provider 需要覆盖 baseUrl（例如百炼），这一步是必须的，
#    否则用户侧只会拿到内置目录，模型可能选不出来。
sudo -u pi cp ~/.pi/agent/models.json /var/lib/pi/template/models.json
#    可选：把团队的 skills 放进模板，会以软链方式共享给所有人
sudo -u pi cp -r ~/.agents/skills /var/lib/pi/template/skills

# 5. 模型凭据（root 独占，不进 argv、不进环境文件）
sudo install -d -m 0750 -o root -g pi /etc/pi
sudo install -m 0600 -o root -g root /dev/null /etc/pi/api_key
sudo sh -c 'printf %s "sk-你的key" > /etc/pi/api_key'

# 6. 部署配置与 systemd
sudo install -m 0640 -o root -g pi deploy/team.env.example /etc/pi/pi.env
sudo cp deploy/pi-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pi-server

# 7. TLS 与反代
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # 改成你的域名
sudo systemctl reload caddy
```

## 首次登录

首次启动时若库里没有任何账号，服务端会创建一个 superadmin，并在 stderr（journald）打印**一次性**密码：

```bash
sudo journalctl -u pi-server --since '5 min ago' | grep -A3 superadmin
```

用 `admin` + 该密码登录，系统会强制你先改密码。想自己指定初始密码就设 `PI_ADMIN_USERNAME` 并给进程加 `--admin-password`（注意：命令行是公开的，只适合首次启动的临时值）。

建号与配额都在「管理」页：新建账号 → 填初始密码 → 对方首次登录必须改密。`token 额度`留空表示不限；用完后新的对话会在**发起前**被拒绝，而不是跑完一整轮才告知。

## 部署后自检

```bash
python deploy/smoke.py https://pi.example.com --username admin --password '…'
```

20 项检查，覆盖的是「这个部署是否真的在拦截」：健康检查报告 `authMode: team`、匿名访问所有数据端点必须 401、错误密码被拒、登录后能读自己的状态、登出立即失效。**每次改配置后都跑一遍**——尤其是反向代理与鉴权中间件。

## 这个部署保护了什么，没保护什么

必须说清楚，否则会给人虚假的安全感：

**已经做到的**

- 每个用户独立的 `cwd` 与 agent 目录；工作区文件 API 的路径经过 realpath 校验，`..`、绝对路径、符号链接逃逸一律拒绝。
- agent 的工具调用经过策略扩展：越界路径拒绝；`sudo`/`docker`/`systemctl`/写系统目录/读部署数据根（别人的会话）/`env` 转储/管道执行下载 一律拒绝；装包、`git push`、远程 shell、写出工作区需要用户点确认，无人应答或超时按拒绝处理。
- 用户自带的扩展**不加载**：per-user 的 `extensions/` 目录等于给每个同事一个以服务进程权限运行的入口。
- agent 启动的 shell 拿不到凭据类环境变量（按变量名分段剔除）。
- systemd 硬化：`ProtectSystem=strict` + 只允许写 `/var/lib/pi`，专用低权用户，无 capability。

**没有做到的（一期有意为之）**

- **没有沙盒**。上一条白名单是「劝阻」不是「隔离」：它能防误操作，防不住恶意用户。真正的边界是二期把执行面搬进容器（设计文档 §12）。
- 同机共享：所有用户的工作区在同一台机器、同一个文件系统上，靠路径规则与 UID 分目录隔离，不靠内核。
- 单进程故障域：一个失控会话会消耗共同内存与事件循环。缓解是 `max_active_users`、空闲回收、`MemoryMax`，不是根治。
- 计费是按模型目录里的价格**估算**的，不是账单。

因此：**这套东西适合在公司内网给信任的同事用**。要对外开放，先做二期。

## 备份与恢复

```bash
sudo -u pi deploy/backup.sh /var/backups/pi
```

- `pi.db` 用 `sqlite3 .backup` 导出——数据库跑在 WAL 模式，直接拷文件可能拿到缺事务或撕裂的库。
- `agents/`（会话记录）打包；`workspaces/`（用户实际的工作）用 rsync 镜像，因为它可能很大。
- 保留 `PI_BACKUP_KEEP_DAYS`（默认 14）天。

恢复：停服 → 放回 `pi.db`、解开 `agents.tar.gz`、回填 `workspaces/` → 起服。会话文件与库之间没有一致性约束（库只存元数据、用量与审计），所以可以分别恢复。

建议再加一层：把每个用户的工作区本身初始化成 git 仓库。这样「agent 改坏了」变成一次 `git checkout`，而不是一次备份恢复——这也是在「无法真正隔离」下最有效的补偿手段。

## 升级

```bash
cd /opt/python-pi && sudo git pull
sudo -u pi uv sync
(cd web && npm ci && npm run build)
sudo systemctl restart pi-server
python deploy/smoke.py https://pi.example.com --username admin --password '…'
```

数据库迁移是幂等的、只增不改：新版本启动时自动补齐新表。**不要**手改已经发布过的迁移。

## 排查

| 现象 | 原因与处理 |
|---|---|
| 登录成功但立刻又回到登录页 | `PI_COOKIE_SECURE=1` 但没有 HTTPS。要么上 Caddy，要么在纯 HTTP 试跑时关掉它 |
| `/api/state` 返回 503，`detail` 说 no credentials / 模型选不出来 | 模板目录缺 `models.json`。检查 `/var/lib/pi/template/models.json` 是否存在，以及 `PI_TEMPLATE_AGENT_DIR` 是否指对 |
| `/api/models` 里没有你要的模型 | 同上——每个用户的 agent 目录是从模板播种的，模板里没有的 provider 覆盖不会生效 |
| 提示「服务已满，稍后再试」（WS 4429） | 活跃会话达到 `PI_MAX_ACTIVE_USERS`。调大，或调小 `PI_SESSION_IDLE_MINUTES` 让空闲会话更快回收 |
| 用户说「额度已用完」但管理页显示有余量 | 用量按自然月（UTC）聚合，月初会重置；确认你看的是同一个月份 |
| 页面能开但一直「正在连接服务器…」 | 反向代理没透传 WebSocket 升级。Caddy 的 `reverse_proxy` 默认支持，若前面还有一层 nginx，需要显式 `Upgrade`/`Connection` 头 |
| 改动没生效 | 前端改了要 `npm run build`；`web/dist` 不存在时服务器只提供 `/api/*` |
