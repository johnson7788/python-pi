#!/usr/bin/env bash
# 安装「数据绘图」的运行期依赖：技能 + 插件。
#
#   ./apps/chart/install.sh                # 用户级 ~/.pi/agent/skills，不碰部署
#   ./apps/chart/install.sh --project      # <repo>/.pi/skills（单机 / 仓库当工作区）
#   ./apps/chart/install.sh --template     # <repo>/template/skills（随仓库分发）
#   ./apps/chart/install.sh --team         # 部署：技能进模板 + 插件进 <data-root>/plugins
#
# --team 的 data root 默认 ~/.pi/team，可用 PI_DATA_ROOT 覆盖。
#
# 应用本身是内置应用（apps.py 里的 CHART），重启服务即可出现，不需要导入 JSON；
# 它声明了依赖 chart-tools 插件，所以也不需要再去插件页点「启用」。
#
# 技能装错地方不会报错，只会静默退化成"没有技能"：team 模式下 worker 的 cwd 是
# <data-root>/workspaces/<user>，技能目录是 <data-root>/agents/<uid>/skills
# （软链到 <data-root>/template/skills），且 include_home_skills=False。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PLUGIN="$REPO/plugins/chart-tools"
SKILLS_SRC="$REPO/template/skills"
DATA_ROOT="${PI_DATA_ROOT:-$HOME/.pi/team}"
TARGET_KIND="user"
SKILL_NAMES=(chart-spec chart-gallery)

for arg in "$@"; do
  case "$arg" in
    --project) TARGET_KIND="project" ;;
    --template) TARGET_KIND="template" ;;
    --team) TARGET_KIND="team" ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 1 ;;
  esac
done

case "$TARGET_KIND" in
  user) SKILLS="$HOME/.pi/agent/skills" ;;
  project) SKILLS="$REPO/.pi/skills" ;;
  template) SKILLS="$SKILLS_SRC" ;;
  team) SKILLS="$DATA_ROOT/template/skills" ;;
esac

mkdir -p "$SKILLS"
for skill in "${SKILL_NAMES[@]}"; do
  if [ "$SKILLS" = "$SKILLS_SRC" ]; then
    echo "技能已在仓库模板里：$SKILLS_SRC/$skill"
    continue
  fi
  rm -rf "$SKILLS/$skill"
  cp -R "$SKILLS_SRC/$skill" "$SKILLS/$skill"
  echo "技能 → $SKILLS/$skill"
done

# team 模式下已存在的用户是软链到模板的；软链指向别处时这个用户读不到新技能。
if [ "$TARGET_KIND" = "team" ]; then
  for agent_skills in "$DATA_ROOT"/agents/*/skills; do
    [ -e "$agent_skills" ] || continue
    if [ "$(readlink "$agent_skills" 2>/dev/null || true)" != "$SKILLS" ]; then
      target="$(readlink "$agent_skills" 2>/dev/null || echo '(真实目录，未软链)')"
      echo "注意：${agent_skills} 没指向 ${SKILLS}，这个用户读的是 ${target}"
      for skill in "${SKILL_NAMES[@]}"; do
        cp -R "$SKILLS_SRC/$skill" "$agent_skills/$skill"
      done
      echo "      已把两个技能复制进去"
    fi
  done

  mkdir -p "$DATA_ROOT/plugins"
  rm -rf "${DATA_ROOT}/plugins/chart-tools"
  cp -R "$PLUGIN" "$DATA_ROOT/plugins/chart-tools"
  echo "插件 → $DATA_ROOT/plugins/chart-tools"
fi

echo
echo "校验技能（用加载器本身校验）："
cd "$REPO"
.venv/bin/python - "$SKILLS" <<'PY'
import sys
from pi_coding_agent.skills import discover_skills_with_diagnostics

result = discover_skills_with_diagnostics([sys.argv[1]])
for skill in result.skills:
    print(f"  ok {skill.name}")
for diag in result.diagnostics:
    print(f"  FAIL {diag}")
PY

echo
echo "校验插件："
.venv/bin/python plugin-kit/pikit.py check "$PLUGIN" 2>&1 | tail -4

echo
echo "打包一份可上传的 zip（别的部署 / 非 team 模式用）："
OUT="$REPO/apps/chart/dist/chart-tools.zip"
mkdir -p "$(dirname "$OUT")"
.venv/bin/python plugin-kit/pikit.py pack "$PLUGIN" --out "$OUT" --force | tail -3

echo
echo "下一步："
if [ "$TARGET_KIND" = "team" ]; then
  echo "  重启服务即可：「数据绘图」是内置应用，插件由应用声明、自动加载。"
  echo "  前端需要 web/public/vendor/echarts.min.js（仓库已带），改过 web 后要 npm run build。"
else
  echo "  team 部署用 ./apps/chart/install.sh --team 一步装齐；"
  echo "  其他环境：插件页上传 ${OUT}，然后重启服务（应用是内置的，不需要导入 JSON）。"
  echo "  单机模式还要确保 web/public/vendor/echarts.min.js 已随前端构建。"
fi
