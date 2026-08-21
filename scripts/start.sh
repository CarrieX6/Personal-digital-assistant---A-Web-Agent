#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 &
backend_pid=$!
trap 'kill "$backend_pid" 2>/dev/null || true' EXIT INT TERM
echo "控制台: http://localhost:3000"
echo "空间照片局域网 Viewer 会按需监听 8765 端口。"
pnpm run dev
