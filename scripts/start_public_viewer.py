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


PUBLIC_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start an opt-in HTTPS tunnel for signed spatial Viewer links."
    )
    parser.add_argument(
        "--acknowledge-public-media",
        action="store_true",
        help="Confirm that signed private media will transit Cloudflare's public network.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("LAN_VIEWER_PORT", "8766")),
    )
    args = parser.parse_args()
    if not args.acknowledge_public_media:
        parser.error(
            "必须显式添加 --acknowledge-public-media；公网链接持有者可在有效期内查看对应私人图片。"
        )
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        parser.error("未找到 cloudflared，请先按 docs/guides/deployment.md 安装。")
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024 到 65535 之间。")

    root = Path(__file__).resolve().parents[1]
    state_path = root / "backend" / "data" / "viewer-public-url.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
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

    def stop(_signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            match = PUBLIC_URL_PATTERN.search(line)
            if match and public_url is None:
                public_url = match.group(0)
                temporary = state_path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        {"url": public_url, "created_at": time.time()},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                temporary.replace(state_path)
                print(f"公网 Viewer 已就绪：{public_url}", flush=True)
                print("保持本进程运行；停止后机器人会自动回退到局域网链接。", flush=True)
            elif "ERR" in line or "error" in line.lower():
                print(line.rstrip(), file=sys.stderr, flush=True)
        return process.wait()
    finally:
        if public_url and state_path.is_file():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                if payload.get("url") == public_url:
                    state_path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
