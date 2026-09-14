#!/usr/bin/env bash
#
# 一键启动 python-pi 的浏览器 UI。
#
#   ./start.sh                     # agent 的工作目录 = 当前目录
#   ./start.sh ~/git/my-project    # 指定工作目录
#   PORT=9000 ./start.sh           # 换端口
#
# 配置读取顺序（后者覆盖前者）：
#   1. 本文件里的默认值
#   2. 仓库根目录的 .env（已在 .gitignore 中，不会被提交）
#   3. 命令行环境变量，例如 PI_API_KEY=sk-xxx ./start.sh
#
# .env 里至少要有 PI_API_KEY。模型地址的覆盖（阿里云百炼的 baseUrl）
# 放在 ~/.pi/agent/models.json 里，见 README「模型配置」。
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 先解析成绝对路径，顺带校验目录存在（后面不再 cd，避免相对路径失效）
PROJECT_DIR="$(cd "${1:-$PWD}" && pwd)"

# --- 默认值 --------------------------------------------------------------------
DEFAULT_MODEL="deepseek/deepseek-v4-flash"
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT="8787"
DEFAULT_AGENT_DIR="$HOME/.pi/agent"

# --- 先记住命令行上显式传入的值（它们优先级最高）---------------------------------
# `set -u` 下用 :- 取默认，避免未定义变量报错。
_explicit_key="${PI_API_KEY:-}"
_explicit_model="${MODEL:-}"
_explicit_host="${HOST:-}"
_explicit_port="${PORT:-}"
_explicit_agent_dir="${PI_AGENT_DIR:-}"
_explicit_no_skills="${PI_NO_SKILLS:-}"

# --- 读取 .env ------------------------------------------------------------------
ENV_FILE="$ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a          # 让 .env 里的变量自动导出给子进程
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# --- 合并：命令行 > .env > 默认值 -----------------------------------------------
[[ -n "$_explicit_key" ]]       && PI_API_KEY="$_explicit_key"
[[ -n "$_explicit_model" ]]     && MODEL="$_explicit_model"
[[ -n "$_explicit_host" ]]      && HOST="$_explicit_host"
[[ -n "$_explicit_port" ]]      && PORT="$_explicit_port"
[[ -n "$_explicit_agent_dir" ]] && PI_AGENT_DIR="$_explicit_agent_dir"
[[ -n "$_explicit_no_skills" ]] && PI_NO_SKILLS="$_explicit_no_skills"

MODEL="${MODEL:-$DEFAULT_MODEL}"
HOST="${HOST:-$DEFAULT_HOST}"
PORT="${PORT:-$DEFAULT_PORT}"
AGENT_DIR="${PI_AGENT_DIR:-$DEFAULT_AGENT_DIR}"
API_KEY="${PI_API_KEY:-}"

# --- 启动前检查 -----------------------------------------------------------------
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
  echo "[start.sh] 未找到 $ROOT/.venv，改用 $PY（依赖可能不全）" >&2
fi

if [[ -z "$API_KEY" ]]; then
  echo "[start.sh] 没有拿到 API key。" >&2
  echo "           复制模板并填入：cp .env.example .env" >&2
  echo "           或临时传入：    PI_API_KEY=sk-xxx $0" >&2
  exit 1
fi

# 前端没构建过就先构建：服务器只有在 web/dist/index.html 存在时才托管 SPA
if [[ ! -f "$ROOT/web/dist/index.html" ]]; then
  echo "[start.sh] 前端尚未构建，正在构建（首次会比较慢）…"
  ( cd "$ROOT/web" && npm install --no-audit --no-fund && npm run build )
fi

# 端口被占用时给一句人话，而不是让 uvicorn 抛一堆栈
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[start.sh] 端口 $PORT 已被占用，换一个：PORT=9000 $0" >&2
  exit 1
fi

# --- 启动 ------------------------------------------------------------------------
# 注意：变量后面紧跟中文标点时必须用 ${VAR} 界定，否则 bash 会把多字节字符
# 当作变量名的一部分（$ENV_FILE（ 会去找一个叫 "ENV_FILE（" 的变量）。
if [[ -n "$_explicit_key" ]]; then
  KEY_SOURCE="命令行 PI_API_KEY"
else
  KEY_SOURCE="${ENV_FILE}（${#API_KEY} 字符，尾号 ${API_KEY: -4}）"
fi

ARGS=(--cwd "$PROJECT_DIR" --model "$MODEL" --agent-dir "$AGENT_DIR"
      --host "$HOST" --port "$PORT")

# 凭据走环境变量，不走命令行参数。
#
# 命令行是公开的：同一台机器上任何账号 `ps aux` 都能看到全量 argv，key 会被完整打印出来
# 并可能落进 shell history / 审计日志。环境变量只有进程属主和 root 能读（/proc/<pid>/environ）。
# 团队部署更进一步，用 systemd 的 LoadCredential + PI_API_KEY_FILE，见 deploy/。
export PI_API_KEY="$API_KEY"

# skills 会整份列进每次请求的 system prompt，本机 ~/.agents/skills 下有 100 个
# （来自其它 agent 工具），实测多出约 10k tokens，所以给一个开关。
#
# 注意：这里用「先建非空数组、再按需追加」的写法。不要写 "${EMPTY[@]}" ——
# macOS 自带 bash 3.2，在 set -u 下展开空数组会直接终止脚本。
NO_SKILLS_NOTE=""
if [[ -n "${PI_NO_SKILLS:-}" && "${PI_NO_SKILLS}" != "0" ]]; then
  ARGS+=(--no-skills)
  NO_SKILLS_NOTE="不注入 skills（省 token）"
fi

echo "[start.sh] 工作目录 : $PROJECT_DIR"
echo "[start.sh] 模型     : $MODEL"
echo "[start.sh] 凭据     : $KEY_SOURCE"
echo "[start.sh] 配置目录 : $AGENT_DIR"
if [[ -n "$NO_SKILLS_NOTE" ]]; then
  echo "[start.sh] 附加行为 : $NO_SKILLS_NOTE"
fi
echo "[start.sh] 打开地址 : http://$HOST:$PORT"
echo

exec "$PY" -m pi_server.cli "${ARGS[@]}"
