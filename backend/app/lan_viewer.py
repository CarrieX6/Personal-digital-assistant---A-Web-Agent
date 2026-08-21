from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .assets import AssetError, SpatialSceneService


class ViewerLinkError(ValueError):
    pass


class LanViewerService:
    """Lazy, read-only LAN server for a single signed spatial asset URL."""

    def __init__(
        self,
        assets: SpatialSceneService,
        *,
        secret_path: Path,
        bind_host: str = "0.0.0.0",
        port: int = 8766,
        ttl_seconds: int = 12 * 60 * 60,
        public_base_url: str | None = None,
        runtime_public_base_path: Path | None = None,
    ) -> None:
        self.assets = assets
        self.secret_path = secret_path
        self.bind_host = bind_host
        self.port = port
        self.ttl_seconds = max(300, min(ttl_seconds, 7 * 24 * 60 * 60))
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.runtime_public_base_path = runtime_public_base_path
        self._secret = self._load_or_create_secret()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        assets: SpatialSceneService,
        *,
        data_path: Path,
    ) -> "LanViewerService":
        return cls(
            assets,
            secret_path=data_path / "viewer-secret.key",
            bind_host=os.getenv("LAN_VIEWER_BIND", "0.0.0.0"),
            port=_safe_int(os.getenv("LAN_VIEWER_PORT"), 8766, 1024, 65535),
            ttl_seconds=_safe_int(
                os.getenv("LAN_VIEWER_TTL_SECONDS"),
                12 * 60 * 60,
                300,
                7 * 24 * 60 * 60,
            ),
            public_base_url=os.getenv("LAN_VIEWER_PUBLIC_BASE_URL"),
            runtime_public_base_path=data_path / "viewer-public-url.json",
        )

    def create_link(self, asset_id: str) -> str:
        asset = self.assets.get_asset(asset_id)
        if asset.kind != "spatial_scene" or asset.status != "ready":
            raise ViewerLinkError("只有已完成的空间照片可以生成预览链接。")
        if not asset.background_url or not asset.foreground_url:
            raise ViewerLinkError("空间照片缺少可动视角分层文件。")
        self.start()
        token = self._issue_token(asset_id)
        base = (
            self.public_base_url
            or self._runtime_public_base_url()
            or f"http://{self.lan_ip()}:{self.port}"
        )
        return f"{base}/v/{quote(token, safe='')}"

    def _runtime_public_base_url(self) -> str | None:
        path = self.runtime_public_base_path
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            url = str(payload["url"]).rstrip("/")
            created_at = float(payload["created_at"])
            parsed = urlsplit(url)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if time.time() - created_at > 24 * 60 * 60:
            return None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        return url

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            handler = self._handler_type()
            try:
                server = ThreadingHTTPServer((self.bind_host, self.port), handler)
            except OSError as exc:
                raise ViewerLinkError(
                    f"无法启动局域网 Viewer（端口 {self.port}）：{exc}"
                ) from exc
            server.daemon_threads = True
            self.port = int(server.server_address[1])
            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="lan-spatial-viewer",
                daemon=True,
            )
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)

    def verify_token(self, token: str) -> str:
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
            payload = _b64decode(encoded_payload)
            signature = _b64decode(encoded_signature)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ViewerLinkError("预览链接签名无效。")
            claims = json.loads(payload.decode("utf-8"))
            asset_id = claims["asset_id"]
            expires_at = int(claims["exp"])
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ViewerLinkError("预览链接格式无效。") from exc
        if not isinstance(asset_id, str) or not asset_id:
            raise ViewerLinkError("预览链接缺少资产 ID。")
        if expires_at < int(time.time()):
            raise ViewerLinkError("预览链接已过期，请在电脑端重新生成。")
        return asset_id

    @staticmethod
    def lan_ip() -> str:
        candidates = _interface_ipv4_candidates()
        if candidates:
            return min(candidates, key=_lan_candidate_rank)[1]

        connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            connection.connect(("1.1.1.1", 80))
            address = str(connection.getsockname()[0])
            return address if address and not address.startswith("127.") else "127.0.0.1"
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return "127.0.0.1"
        finally:
            connection.close()

    def _issue_token(self, asset_id: str) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "asset_id": asset_id,
                "exp": int(time.time()) + self.ttl_seconds,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def _load_or_create_secret(self) -> bytes:
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        if self.secret_path.exists():
            secret = self.secret_path.read_bytes()
            if len(secret) >= 32:
                return secret
        secret = os.urandom(32)
        self.secret_path.write_bytes(secret)
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return secret

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        viewer = self

        class ViewerHandler(BaseHTTPRequestHandler):
            server_version = "AgentLabViewer/1.0"

            def do_GET(self) -> None:  # noqa: N802
                viewer._handle_get(self)

            def log_message(self, _: str, *args: Any) -> None:
                return

        return ViewerHandler

    def _handle_get(self, request: BaseHTTPRequestHandler) -> None:
        parts = [unquote(part) for part in urlsplit(request.path).path.split("/") if part]
        if len(parts) < 2 or parts[0] != "v":
            self._send_text(request, 404, "找不到预览页面。")
            return
        token = parts[1]
        try:
            asset_id = self.verify_token(token)
            asset = self.assets.get_asset(asset_id)
            if asset.kind != "spatial_scene" or asset.status != "ready":
                raise ViewerLinkError("空间照片当前不可预览。")
            if len(parts) == 2:
                self._send_html(request, self._viewer_html(token, asset))
                return
            if len(parts) == 4 and parts[2] == "files":
                self._send_asset_file(request, asset, parts[3])
                return
            self._send_text(request, 404, "找不到预览资源。")
        except (AssetError, ViewerLinkError) as exc:
            self._send_text(request, 403, str(exc))

    def _send_asset_file(
        self,
        request: BaseHTTPRequestHandler,
        asset: Any,
        filename: str,
    ) -> None:
        allowed = {
            _url_filename(value)
            for value in (
                asset.source_url,
                asset.preview_url,
                asset.depth_url,
                asset.background_url,
                asset.foreground_url,
            )
            if value
        }
        if filename not in allowed:
            raise ViewerLinkError("该资源不属于当前空间照片。")
        path = self.assets.resolve_asset_file(asset.id, filename)
        payload = path.read_bytes()
        request.send_response(200)
        request.send_header(
            "Content-Type",
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "private, max-age=3600")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        request.wfile.write(payload)

    def _viewer_html(self, token: str, asset: Any) -> str:
        background = quote(_url_filename(asset.background_url), safe="")
        foreground = quote(_url_filename(asset.foreground_url), safe="")
        title = html.escape(asset.name)
        file_base = f"/v/{quote(token, safe='')}/files"
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
<meta name="theme-color" content="#05070b" /><title>{title} · 空间照片</title>
<style>
*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#05070b;color:#fff;font-family:system-ui,-apple-system,sans-serif}}
#scene{{position:fixed;inset:0;overflow:hidden;touch-action:none;perspective:1000px}}
.layer{{position:absolute;inset:-5%;width:110%;height:110%;object-fit:cover;will-change:transform;transition:transform .12s ease-out}}
#bg{{transform:translate3d(var(--bx,0),var(--by,0),-20px) scale(1.08)}}#fg{{transform:translate3d(var(--fx,0),var(--fy,0),35px) scale(1.06)}}
.shade{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.18),transparent 28%,rgba(0,0,0,.35));pointer-events:none}}
.hint{{position:absolute;left:50%;bottom:max(22px,env(safe-area-inset-bottom));transform:translateX(-50%);padding:9px 14px;border:1px solid rgba(255,255,255,.25);border-radius:999px;background:rgba(10,12,18,.48);backdrop-filter:blur(14px);font-size:13px;white-space:nowrap}}
</style></head><body><main id="scene" aria-label="{title} 可动视角预览">
<img id="bg" class="layer" src="{file_base}/{background}" alt="" />
<img id="fg" class="layer" src="{file_base}/{foreground}" alt="{title}" />
<div class="shade"></div><div class="hint">拖动或轻微转动手机查看空间视差</div></main>
<script>
const root=document.documentElement,scene=document.getElementById('scene');let tx=0,ty=0;
function move(x,y){{tx=Math.max(-1,Math.min(1,x));ty=Math.max(-1,Math.min(1,y));root.style.setProperty('--bx',`${{tx*-10}}px`);root.style.setProperty('--by',`${{ty*-7}}px`);root.style.setProperty('--fx',`${{tx*22}}px`);root.style.setProperty('--fy',`${{ty*15}}px`)}}
scene.addEventListener('pointermove',e=>{{const r=scene.getBoundingClientRect();move((e.clientX/r.width-.5)*2,(e.clientY/r.height-.5)*2)}});
scene.addEventListener('pointerleave',()=>move(0,0));window.addEventListener('deviceorientation',e=>{{if(e.gamma==null||e.beta==null)return;move(e.gamma/28,(e.beta-45)/35)}},true);
</script></body></html>"""

    @staticmethod
    def _send_html(request: BaseHTTPRequestHandler, body: str) -> None:
        payload = body.encode("utf-8")
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "private, no-store")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("X-Frame-Options", "DENY")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        request.end_headers()
        request.wfile.write(payload)

    @staticmethod
    def _send_text(request: BaseHTTPRequestHandler, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        request.send_response(status)
        request.send_header("Content-Type", "text/plain; charset=utf-8")
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        request.wfile.write(payload)


def _safe_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def _interface_ipv4_candidates() -> list[tuple[str, str]]:
    """Return private IPv4 candidates while excluding common tunnel interfaces."""
    command = ["ipconfig"] if os.name == "nt" else ["ifconfig"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    interface = "unknown"
    candidates: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if os.name == "nt":
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                interface = line.rstrip(": ").strip()
        else:
            match = re.match(r"^([A-Za-z0-9_.:-]+):", line)
            if match:
                interface = match.group(1)
        for address in re.findall(r"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])", line):
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if (
                isinstance(ip, ipaddress.IPv4Address)
                and ip.is_private
                and not ip.is_loopback
                and not ip.is_link_local
            ):
                candidates.append((interface, address))
    return list(dict.fromkeys(candidates))


def _lan_candidate_rank(candidate: tuple[str, str]) -> tuple[int, int, str]:
    interface, address = candidate
    lowered = interface.lower()
    tunnel_prefixes = (
        "utun",
        "tun",
        "tap",
        "ppp",
        "ipsec",
        "wg",
        "docker",
        "veth",
        "vmnet",
        "bridge",
        "awdl",
        "llw",
    )
    tunnel_penalty = 1 if any(part in lowered for part in tunnel_prefixes) else 0
    ip = ipaddress.ip_address(address)
    if ip in ipaddress.ip_network("192.168.0.0/16"):
        subnet_rank = 0
    elif ip in ipaddress.ip_network("172.16.0.0/12"):
        subnet_rank = 1
    else:
        subnet_rank = 2
    return tunnel_penalty, subnet_rank, address


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _url_filename(value: str) -> str:
    return unquote(urlsplit(value).path.rsplit("/", maxsplit=1)[-1])
