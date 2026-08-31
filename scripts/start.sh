#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

public_viewer_pid=""
if .venv/bin/python -c 'import os; from dotenv import load_dotenv; load_dotenv(); raise SystemExit(0 if os.getenv("PHOTO_STYLE_AUTO_START", "").strip().lower() in {"1", "true", "yes", "on"} else 1)'; then
  if ! .venv/bin/python scripts/manage_photo_style.py start; then
    echo "警告：图片风格化独立服务未能自动启动；控制台会继续启动并显示真实不可用状态。" >&2
  fi
fi
if .venv/bin/python -c 'import os; from dotenv import load_dotenv; load_dotenv(); raise SystemExit(0 if os.getenv("PUBLIC_VIEWER_AUTO_START", "").strip().lower() in {"1", "true", "yes", "on"} else 1)'; then
  .venv/bin/python scripts/start_public_viewer.py --from-env --reconnect &
  public_viewer_pid=$!
fi
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 &
backend_pid=$!
cleanup() {
  kill "$backend_pid" 2>/dev/null || true
  if [ -n "$public_viewer_pid" ]; then
    kill "$public_viewer_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM
echo "控制台: http://localhost:3000"
echo "空间照片局域网 Viewer 会按需监听 8766 端口。"
pnpm run dev
