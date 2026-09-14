# 团队自托管部署

面向运维：把 python-pi 部署成内部团队共用的服务。
设计依据与取舍见 [`plans/2026-09-14-team-saas-design.md`](plans/2026-09-14-team-saas-design.md)。

## 形态

```
浏览器 ──HTTPS──▶ Caddy ──127.0.0.1:8787──▶ uvicorn（单进程，--workers 1）
                                              ├── /var/lib/pi
                                              │     pi.db              账号/用量/审计
                                              │     workspaces/<uid>/  用户的工作区（agent 的 cwd）
                                              │     agents/<uid>/      会话 JSONL、settings、models
                                              └── 沙盒（可选）
                                                    provisioner ──docker.sock──▶ 每个会话一个容器
                                                                                 只挂载该用户的工作区
```

**单进程是硬约束**：会话状态在进程内存里，跑多 worker 会出现「同一用户的两次请求落到不同进程、看到不同会话」。要横向扩展就得先把注册表换成外部状态（届时一并换 PostgreSQL），见设计文档 §7。

**两个进程的安全边界差一档**：`server` 跑 agent 的代码，`provisioner` 拿 docker.sock。后者绝不能是前者——一个能启动任意容器的进程本身就是逃逸手段。

## 前置条件

| 需求 | 说明 |
|---|---|
| Docker + Compose v2 | 默认部署方式；沙盒必须（provisioner 要访问 daemon） |
| 一个 OpenAI 兼容的模型端点与 key | 默认走阿里云百炼的 `deepseek/deepseek-v4-flash` |
| Caddy 2（可选） | TLS 终止与反向代理；用别的反代也行，注意 WebSocket 升级与不要缓冲 |

不用 Docker 也可以：Python ≥ 3.11 + [uv](https://docs.astral.sh/uv/) 直接跑，见文末附录。那种部署没有沙盒。

## 安装

```bash
git clone <repo> /opt/python-pi && cd /opt/python-pi

# 一条命令做完：预检 → 生成配置 → 建镜像 → 起服务 → 自检
python3 deploy/deploy_docker.py up --api-key sk-你的key
```

脚本会自己算那些"错了不会报错、只会悄悄不对"的东西：

| 它会处理 | 为什么不能靠人手 |
|---|---|
| `PI_SANDBOX_HOST_DATA_ROOT` | daemon 在**宿主**上解析 bind mount，而 server 容器里那份数据叫 `/data`。对不上时沙盒会带着空工作区启动，命令照样"成功" |
| `uid:gid` | 沙盒写的文件属主必须和 server 一致，否则服务端改不动 agent 自己的产出 |
| 端口占用 | 8787 可能已经被一个 `start.sh` 实例占着；不查就会在 compose 里炸得很晚才看见 |
| provisioner token | 写进 `.env.team`（0600）；重跑不会轮换，轮换会让运行中的服务端连不上 |
| 模型 key | 写进 `secrets/api_key`（0400）并作为 docker secret 挂载——从不进 argv，`ps` 看不到 |

常用命令：

```bash
python3 deploy/deploy_docker.py doctor      # 只看环境与解析出的配置，不动任何东西
python3 deploy/deploy_docker.py up          # 重复执行 = 升级/重建，数据不动
python3 deploy/deploy_docker.py status
python3 deploy/deploy_docker.py logs -f
python3 deploy/deploy_docker.py smoke --username admin --password '…'
python3 deploy/deploy_docker.py down        # 停服务；数据保留
```

`up` 结束时会打印首次启动的管理员密码（只出现这一次），并自动在容器里跑一遍完整的 20 项自检。几个有用的开关：

| 开关 | 用途 |
|---|---|
| `--port 8899` | 换发布端口（默认 8787） |
| `--data-root ~/pi-data` | 换宿主数据目录（默认 `/srv/pi`，不可写时退到 `~/pi-data`） |
| `--no-sandbox` | 命令在 server 进程里跑，不需要 provisioner |
| `--insecure-cookies` | **仅本机 HTTP 试用**：清掉 cookie 的 Secure 位，否则登录会跳回登录页 |
| `--rebuild-sandbox` | 重建沙盒镜像 |

### 手工步骤（不想用脚本时）

```bash
sudo install -d -m 0750 -o "$(id -u)" -g "$(id -g)" /srv/pi /srv/pi/template
cp ~/.pi/agent/models.json /srv/pi/template/models.json   # 见下

install -d -m 0700 secrets
install -m 0400 /dev/null secrets/api_key && printf %s 'sk-你的key' > secrets/api_key

cat > .env.team <<EOF
PI_UID=$(id -u)
PI_GID=$(id -g)
PI_SANDBOX_HOST_DATA_ROOT=/srv/pi
SANDBOX_PROVISIONER_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
SANDBOX_IMAGE=pi-sandbox:latest
EOF

docker build -t pi-sandbox:latest deploy/sandbox_image
docker compose --env-file .env.team up -d --build
docker compose --env-file .env.team logs -f server   # 首次启动会打印一次性管理员密码
```

**模型目录模板**：每个新用户的 agent 目录从 `<data-root>/template` 播种。如果你的 provider 需要覆盖 `baseUrl`（例如百炼），这一步是必须的，否则用户侧只会拿到内置目录，模型可能选不出来。

可选：把团队的 skills 放进模板的 `skills/`，会以软链方式共享给所有人。

### TLS

`server` 只发布在 `127.0.0.1:8787`。用 Caddy 终止 TLS：

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # 改成你的域名
sudo systemctl reload caddy
```

反代必须做到两件事：透传 WebSocket 升级（`/api/ws`），以及**不要缓冲**响应——`deploy/Caddyfile` 里的 `flush_interval -1` 就是这个作用。

## 沙盒（容器化执行）

默认 `PI_SANDBOX_BACKEND=local`：命令就在 server 进程里跑，靠策略扩展拦。改成 `provisioner` 之后，**agent 的每条 bash 命令都在一个容器里执行**，该容器只挂载这个用户的工作区：

```bash
docker compose --env-file .env.team exec server env | grep PI_SANDBOX   # 确认已开启
docker ps --filter label=app=pi-sandbox                                 # 看当前沙盒
```

容器里有什么、没有什么：

| | |
|---|---|
| 挂载 | 只有该会话的工作区（`/workspace`，读写）。数据根、别人的工作区、会话记录、pi.db 都不可见 |
| 权限 | `cap_drop=ALL`、`no-new-privileges`、非 root（用 server 的 uid/gid，这样它写的文件你还能改） |
| 限制 | 内存、CPU、pid 数上限；单条命令有超时（容器内 `timeout`） |
| 网络 | 默认 `bridge`（能 `pip install`、`git clone`）；`SANDBOX_NETWORK=none` 是严格档，代价是装不了包 |
| 没有 | docker.sock（不能在沙盒里再起容器）、宿主文件系统、宿主环境变量 |
| 根文件系统 | **可写但短暂**：装的东西随容器消失，不会污染镜像；只有工作区里的改动留下来 |

先验证它真的生效（这是最值得花五分钟的一步）：

```bash
# 在浏览器里让 agent 跑：hostname; cat 你自己放进去的文件
# 容器名应当是容器 id，而不是宿主机名；文件应该读得到。
```

**沙盒只覆盖命令执行。** `read`/`write`/`edit` 仍由 server 进程直接读写工作区（那条路径本来就只能碰工作区，且策略扩展另行校验）。所以「沙盒」在这里的准确含义是：*agent 的 shell 不再是宿主的 shell*。

## 首次登录

首次启动时若库里没有任何账号，服务端会创建一个 superadmin，并在容器日志里打印**一次性**密码：

```bash
docker compose --env-file .env.team logs server | grep -A3 superadmin
```

用 `admin` + 该密码登录，系统会强制你先改密码。想自己指定初始密码就设 `PI_ADMIN_USERNAME` 并给进程加 `--admin-password`（注意：命令行是公开的，只适合首次启动的临时值）。

建号与配额都在「管理」页：新建账号 → 填初始密码 → 对方首次登录必须改密。`token 额度`留空表示不限；用完后新的对话会在**发起前**被拒绝，而不是跑完一整轮才告知。

## 部署后自检

```bash
python3 deploy/deploy_docker.py smoke --username admin --password '…'
# 或对任意地址（含 HTTPS 反代之后）：
python3 deploy/smoke.py https://pi.example.com --username admin --password-stdin <<< '…'
```

20 项检查，覆盖的是「这个部署是否真的在拦截」：健康检查报告 `authMode: team`、匿名访问所有数据端点必须 401、错误密码被拒、登录后能读自己的状态、登出立即失效。**每次改配置后都跑一遍**——尤其是反向代理与鉴权中间件。

## 这个部署保护了什么，没保护什么

必须说清楚，否则会给人虚假的安全感。下面按「开了沙盒 / 没开沙盒」分开说。

**与沙盒无关，一直都有的**

- 每个用户独立的 `cwd` 与 agent 目录；工作区文件 API 的路径经过 realpath 校验，`..`、绝对路径、符号链接逃逸一律拒绝。
- agent 的工具调用经过策略扩展：越界路径拒绝；`sudo`/`docker`/`systemctl`/写系统目录/读部署数据根（别人的会话）/`env` 转储/管道执行下载 一律拒绝；装包、`git push`、远程 shell、写出工作区需要用户点确认，无人应答或超时按拒绝处理。
- 用户自带的扩展**不加载**：per-user 的 `extensions/` 目录等于给每个同事一个以服务进程权限运行的入口。
- agent 启动的 shell 拿不到凭据类环境变量（按变量名分段剔除）。
- 只有 provisioner 挂 docker.sock；server 进程即使被完全控制也无法创建容器。

**`PI_SANDBOX_BACKEND=provisioner` 时额外有的**

- 命令在一个只挂载该用户工作区的容器里执行：宿主文件系统、别人的工作区、会话记录、pi.db 全都不可见。
- 容器无 capability、非 root、有内存/CPU/pid 上限，单条命令有超时，拿不到 docker.sock。
- 因此 `rm -rf /` 之类只毁掉一个临时容器。

**仍然没有做到的**

- 文件工具（`read`/`write`/`edit`）不在容器里，由 server 进程直接读写工作区。它们被路径规则限制在工作区内，但那是应用层判断，不是内核边界。
- **策略闸门不会因为有了沙盒而放宽**：`sudo` 依然被拒。沙盒降低的是「万一」的后果，不是「该不该跑」的判断——审计日志里仍然只记意图。
- 同机共享工作区目录：靠路径规则与 UID 分目录隔离。容器只挂载自己那一份，但同一台机器上仍有别人的数据。
- 单进程故障域：一个失控会话会消耗共同内存与事件循环。缓解是 `max_active_users`、空闲回收、`MemoryMax`，不是根治。
- 计费是按模型目录里的价格**估算**的，不是账单。

因此：**开了沙盒之后，这套东西可以给不太信任的同事用；不开沙盒时，只适合内网信任的人。** 对外网开放仍然需要更多（入站限流、审计留存、镜像更新流程）。

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
| 沙盒里的命令看不到工作区里的文件 | `PI_SANDBOX_HOST_DATA_ROOT` 与 compose 里挂载的路径不一致。daemon 在宿主解析 bind mount，`/data/...` 只在 server 容器里存在 |
| 服务端起不来，日志说 provisioner needs … | 设了 `PI_SANDBOX_BACKEND=provisioner` 但缺 URL 或 token。这是有意的：宁可不起，也不要悄悄退回进程内执行 |
| 沙盒容器起不来，报 image has no timeout | 镜像里没有 coreutils。用 `deploy/sandbox_image/Dockerfile` 构建，或自己装 `coreutils` |
| 沙盒写出来的文件在宿主上是 root 所有 | `PI_UID`/`PI_GID` 与宿主数据目录的属主不一致。`id -u` / `id -g` 对一下 |
| `docker ps` 里堆着 pi-sandbox-* | provisioner 的空闲回收没生效（它被杀过）。跑 `deploy_docker.py down` 会按 label 精确清理，也可以手动 `docker rm -f $(docker ps -aq --filter label=app=pi-sandbox)` |
| 报「the sandbox is not mounting this workspace」 | 探针发现的：`PI_SANDBOX_HOST_DATA_ROOT` 与 server 实际挂载的卷不是同一个目录。这是探针存在的意义——否则沙盒会带着空工作区静默"成功" |
| `up` 说端口被占 | 8787 上可能有个 `start.sh` 实例在跑。换 `--port`，或先停掉它 |

## 附录：不用 Docker / 不用沙盒的部署

有 Python 环境就能跑，代价是没有容器隔离——命令在 server 进程里执行，只靠策略扩展拦。

```bash
sudo useradd --system --home /var/lib/pi --shell /usr/sbin/nologin pi
sudo -u pi git clone <repo> /opt/python-pi && cd /opt/python-pi
sudo -u pi uv sync
(cd web && npm ci && npm run build)

sudo install -d -m 0750 -o pi -g pi /var/lib/pi /var/lib/pi/template
sudo -u pi cp ~/.pi/agent/models.json /var/lib/pi/template/models.json

# 凭据走 systemd credential，不进 argv 也不进环境文件
sudo install -d -m 0750 -o root -g pi /etc/pi
sudo install -m 0600 -o root -g root /dev/null /etc/pi/api_key
sudo sh -c 'printf %s "sk-你的key" > /etc/pi/api_key'

sudo install -m 0640 -o root -g pi deploy/team.env.example /etc/pi/pi.env
sudo cp deploy/pi-server.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pi-server
```

该单元带 systemd 硬化（`ProtectSystem=strict` + 只允许写 `PI_DATA_ROOT`、专用用户、无 capability、内存/CPU 上限）。它**不**设 `PI_SANDBOX_BACKEND`——设成 `provisioner` 会让服务端在启动时拒绝启动，因为那时候还没有 provisioner 可连。
