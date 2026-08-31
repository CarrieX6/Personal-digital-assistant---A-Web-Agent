from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from dotenv import load_dotenv


PUBLIC_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
TRUE_VALUES = {"1", "true", "yes", "on"}


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _clear_state(path: Path, public_url: str | None = None) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if public_url is not None and payload.get("url") != public_url:
            return
        path.unlink()
    except (OSError, ValueError, json.JSONDecodeError):
        try:
            path.unlink()
        except OSError:
            pass


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    parser = argparse.ArgumentParser(
        description="Start an opt-in HTTPS tunnel for signed spatial Viewer links."
    )
    parser.add_argument(
        "--acknowledge-public-media",
        action="store_true",
        help="Confirm that signed private media will transit Cloudflare's public network.",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="Only start when PUBLIC_VIEWER_AUTO_START is enabled in .env.",
    )
    parser.add_argument(
        "--reconnect",
        action="store_true",
        help="Reconnect with bounded exponential backoff if the quick tunnel exits.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("LAN_VIEWER_PORT", "8766")),
    )
    args = parser.parse_args()
    if args.from_env and not _enabled(os.getenv("PUBLIC_VIEWER_AUTO_START")):
        return 0
    acknowledged = args.acknowledge_public_media or _enabled(
        os.getenv("PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA")
    )
    if not acknowledged:
        parser.error(
            "必须显式添加 --acknowledge-public-media，或在 .env 中设置 "
            "PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA=true；公网链接持有者可在有效期内"
            "查看对应私人图片。"
        )
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        parser.error("未找到 cloudflared，请先按 docs/guides/deployment.md 安装。")
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024 到 65535 之间。")

    state_path = root / "backend" / "data" / "viewer-public-url.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stopping = False
    process: subprocess.Popen[str] | None = None

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    retry_delay = 1.0
    while not stopping:
        _clear_state(state_path)
        process = subprocess.Popen(
            [
                cloudflared,
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{args.port}",
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        public_url: str | None = None
        try:
            assert process.stdout is not None
            for line in process.stdout:
                match = PUBLIC_URL_PATTERN.search(line)
                if match and public_url is None:
                    public_url = match.group(0)
                    _write_state(
                        state_path,
                        {
                            "url": public_url,
                            "created_at": time.time(),
                            "status": "connected",
                            "pid": process.pid,
                        },
                    )
                    retry_delay = 1.0
                    print(f"公网 Viewer 已就绪：{public_url}", flush=True)
                    print(
                        "临时域名失效时会自动重连；旧域名仍需从飞书卡片刷新。",
                        flush=True,
                    )
                elif "ERR" in line or "error" in line.lower():
                    print(line.rstrip(), file=sys.stderr, flush=True)
            return_code = process.wait()
        finally:
            _clear_state(state_path, public_url)
        if stopping or not args.reconnect:
            return 0 if stopping else return_code
        print(
            f"公网 Viewer 隧道已断开，{retry_delay:g} 秒后重新连接。",
            file=sys.stderr,
            flush=True,
        )
        deadline = time.monotonic() + retry_delay
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))
        retry_delay = min(30.0, retry_delay * 2)
    _clear_state(state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
