#!/usr/bin/env bash
# Pack every starter plugin into dist/<id>.zip, refusing to ship one that fails check.
set -euo pipefail

cd "$(dirname "$0")/.."
python="${PYTHON:-.venv/bin/python}"
[[ -x "$python" ]] || python="python3"

mkdir -p dist
status=0
for dir in plugins/*/; do
  name="$(basename "$dir")"
  [[ -f "$dir/plugin.json" ]] || continue
  echo "== $name"
  if ! "$python" plugin-kit/pikit.py pack "$dir" --out "dist/$name.zip" --force; then
    status=1
  fi
done

if [[ $status -ne 0 ]]; then
  echo "有插件未通过校验，见上面的输出。" >&2
  exit 1
fi
echo "完成：dist/*.zip"
