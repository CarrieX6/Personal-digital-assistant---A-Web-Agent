#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  echo "未找到 .venv，请先运行 ./scripts/setup.sh。" >&2
  exit 2
fi

exec .venv/bin/python scripts/deploy.py start "$@"
