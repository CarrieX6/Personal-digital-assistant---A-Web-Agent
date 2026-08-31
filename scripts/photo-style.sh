#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python_bin=".venv/bin/python"
if [ ! -x "$python_bin" ]; then
  echo "未找到主项目 .venv，请先运行 ./scripts/setup.sh。" >&2
  exit 2
fi

exec "$python_bin" scripts/manage_photo_style.py "$@"
