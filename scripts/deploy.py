from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import struct
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_ROOT = PROJECT_ROOT / ".venv"
MINIMUM_NODE = (22, 13, 0)
TRUTHY = {"1", "true", "yes", "on"}
BUNDLE_MAGIC = b"PDA-BUNDLE-1\n"


class DeploymentError(RuntimeError):
    pass


def project_python() -> Path:
    if os.name == "nt":
        return VENV_ROOT / "Scripts" / "python.exe"
    return VENV_ROOT / "bin" / "python"


def run(
    command: Sequence[str | Path],
    *,
    cwd: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
) -> None:
    printable = [str(part) for part in command]
    print("+ " + " ".join(printable), flush=True)
    try:
        subprocess.run(printable, cwd=cwd, env=env, check=True)
    except FileNotFoundError as exc:
        raise DeploymentError(f"未找到命令：{printable[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise DeploymentError(
            f"命令执行失败（exit={exc.returncode}）：{' '.join(printable)}"
        ) from exc


def capture(command: Sequence[str | Path]) -> str:
    printable = [str(part) for part in command]
    try:
        completed = subprocess.run(
            printable,
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise DeploymentError(f"命令不可用：{' '.join(printable)}") from exc
    return completed.stdout.strip()


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise DeploymentError(f"无法识别版本号：{value}")
    return tuple(int(item or 0) for item in match.groups())


def ensure_python_version() -> None:
    if sys.version_info < (3, 11):
        raise DeploymentError("需要 Python 3.11 或更高版本。")
    if sys.version_info >= (3, 14):
        print("警告：Python 3.14+ 尚未纳入项目实机验证矩阵。", flush=True)


def resolve_pnpm() -> list[str]:
    if shutil.which("pnpm"):
        return ["pnpm"]
    if shutil.which("corepack"):
        return ["corepack", "pnpm"]
    raise DeploymentError(
        "未找到 pnpm 或 corepack。请安装 Node.js 22.13+（含 Corepack）。"
    )


def ensure_node() -> list[str]:
    if not shutil.which("node"):
        raise DeploymentError("未找到 Node.js，请安装 Node.js 22.13+。")
    version = parse_version(capture(["node", "--version"]))
    if version < MINIMUM_NODE:
        raise DeploymentError(
            f"Node.js 版本过低：{version[0]}.{version[1]}.{version[2]}；"
            "需要 22.13+。"
        )
    pnpm = resolve_pnpm()
    if pnpm[:1] == ["corepack"]:
        run([*pnpm, f"--version"])
    return pnpm


def ensure_env_file() -> None:
    destination = PROJECT_ROOT / ".env"
    source = PROJECT_ROOT / ".env.example"
    if destination.exists():
        print("保留现有 .env，不覆盖本机配置。", flush=True)
        return
    if not source.is_file():
        raise DeploymentError("仓库缺少 .env.example。")
    shutil.copyfile(source, destination)
    print("已由 .env.example 创建 .env。", flush=True)


def read_env(path: Path | None = None) -> dict[str, str]:
    selected = path or PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if not selected.is_file():
        return values
    for raw_line in selected.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def update_env(updates: dict[str, str]) -> None:
    path = PROJECT_ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def ensure_virtualenv() -> Path:
    python = project_python()
    if not python.is_file():
        print(f"创建隔离环境：{VENV_ROOT}", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV_ROOT)
    return python


def install_python_dependencies(python: Path, *, complete: bool) -> None:
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "-r", "backend/requirements.txt"])
    if complete:
        run(
            [
                python,
                "-m",
                "pip",
                "install",
                "-r",
                "backend/requirements-segmentation.txt",
            ]
        )


def install_frontend_dependencies(pnpm: list[str]) -> None:
    run([*pnpm, "install", "--frozen-lockfile"])


def prepare_memory_model(python: Path) -> None:
    run(
        [
            python,
            "backend/scripts/prepare_memory_embedding_model.py",
            "--download",
            "--smoke-test",
        ]
    )
    update_env(
        {
            "AGENT_MEMORY_SEMANTIC_ENABLED": "true",
            "AGENT_MEMORY_EMBEDDING_PROVIDER": "transformer",
            "AGENT_MEMORY_EMBEDDING_MODEL_PATH": (
                "backend/models/embeddings/granite-embedding-97m-multilingual-r2"
            ),
        }
    )


def prepare_spatial_models(python: Path) -> None:
    run(
        [
            python,
            "backend/scripts/prepare_spatial_models.py",
            "--download",
            "--verify",
        ]
    )
    update_env(
        {
            "SPATIAL_DEPTH_MODEL": (
                "backend/models/spatial/depth-anything-v2-small"
            ),
            "SPATIAL_BIREFNET_MODEL": "backend/models/spatial/birefnet",
            "SPATIAL_BIREFNET_LOCAL_FILES_ONLY": "true",
        }
    )


def confirm_model_licenses(accepted: bool) -> bool:
    if accepted:
        return True
    if not sys.stdin.isatty():
        raise DeploymentError(
            "完整图片风格化会下载约 9.84 GiB 固定模型。非交互安装必须追加 "
            "--accept-model-licenses。"
        )
    answer = input(
        "已阅读安装器上方列出的模型许可证，并同意下载固定权重？"
        "请输入 ACCEPT 继续："
    ).strip()
    if answer != "ACCEPT":
        raise DeploymentError("未接受模型许可证，已停止真实模型部署。")
    return True


def photo_style_doctor(python: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [python, "scripts/manage_photo_style.py", "status", "--json"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    return payload if isinstance(payload, dict) else {}


def prepare_real_photo_style(
    python: Path,
    *,
    accepted: bool,
    remote_url: str | None,
) -> None:
    if remote_url:
        run(
            [
                python,
                "scripts/manage_photo_style.py",
                "configure-remote",
                "--url",
                remote_url,
            ]
        )
        return

    run([python, "scripts/manage_photo_style.py", "plan-real"])
    confirm_model_licenses(accepted)
    status = photo_style_doctor(python)
    profiles = set(status.get("supported_real_profiles") or [])
    if "macos-mps-native" in profiles:
        run(
            [
                python,
                "scripts/manage_photo_style.py",
                "prepare-macos-mps",
                "--accept-model-licenses",
            ]
        )
        return
    if "windows-nvidia-service" in profiles:
        if not status.get("docker_available"):
            raise DeploymentError(
                "Windows NVIDIA 独立图片服务需要 Docker Desktop（WSL2 后端）。"
            )
        run([python, "scripts/manage_photo_style.py", "install-service"])
        run(
            [
                python,
                "scripts/manage_photo_style.py",
                "prepare-windows-gpu",
                "--accept-model-licenses",
            ]
        )
        run(
            [
                python,
                "scripts/manage_photo_style.py",
                "configure-real-windows",
            ]
        )
        return
    raise DeploymentError(
        "本机没有受支持的真实图片风格化加速器。请在 NVIDIA GPU 电脑部署，"
        "或用 --photo-style-url https://... 配置远程 GPU 服务。"
    )


def prepare_test_photo_style(python: Path) -> None:
    run([python, "scripts/manage_photo_style.py", "deploy-test"])


def install(args: argparse.Namespace) -> None:
    ensure_python_version()
    ensure_env_file()
    pnpm = ensure_node()
    python = ensure_virtualenv()
    complete = args.profile == "complete"
    install_python_dependencies(python, complete=complete)
    install_frontend_dependencies(pnpm)
    if complete and not args.skip_spatial_models:
        prepare_spatial_models(python)
    if complete and not args.skip_memory_model:
        prepare_memory_model(python)
    if args.photo_style == "real":
        prepare_real_photo_style(
            python,
            accepted=args.accept_model_licenses,
            remote_url=args.photo_style_url,
        )
    elif args.photo_style == "test":
        prepare_test_photo_style(python)
    print("\n部署安装完成。下一步运行：python scripts/deploy.py start", flush=True)
    print("启动后运行：python scripts/deploy.py check", flush=True)


def request_json(url: str, *, timeout: float = 3) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def request_status(url: str, *, timeout: float = 3) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def truthy(values: dict[str, str], key: str) -> bool:
    return values.get(key, "").strip().lower() in TRUTHY


def start(args: argparse.Namespace) -> None:
    python = project_python()
    if not python.is_file():
        raise DeploymentError("未找到 .venv，请先运行 deploy install。")
    pnpm = ensure_node()
    values = read_env()
    style_provider = values.get("PHOTO_STYLE_PROVIDER", "pic-style-http").lower()
    if style_provider in {"pic-style-http", "http"} and truthy(
        values, "PHOTO_STYLE_AUTO_START"
    ):
        run([python, "scripts/manage_photo_style.py", "start"])

    children: list[subprocess.Popen[Any]] = []
    if truthy(values, "PUBLIC_VIEWER_AUTO_START"):
        if not truthy(values, "PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA"):
            raise DeploymentError(
                "PUBLIC_VIEWER_AUTO_START=true 时必须同时显式确认 "
                "PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA=true。"
            )
        children.append(
            subprocess.Popen(
                [
                    str(python),
                    "scripts/start_public_viewer.py",
                    "--from-env",
                    "--reconnect",
                ],
                cwd=PROJECT_ROOT,
            )
        )
    children.append(
        subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "backend.app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            cwd=PROJECT_ROOT,
        )
    )
    children.append(
        subprocess.Popen([*pnpm, "run", "dev"], cwd=PROJECT_ROOT)
    )

    def stop_children(*_: object) -> None:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop_children)
    signal.signal(signal.SIGTERM, stop_children)
    deadline = time.monotonic() + max(30, args.wait_seconds)
    announced = False
    try:
        while True:
            exited = [child for child in children if child.poll() is not None]
            if exited:
                code = exited[0].returncode
                raise DeploymentError(f"服务进程提前退出（exit={code}）。")
            if not announced:
                api = request_json("http://127.0.0.1:8000/health")
                web = request_status("http://127.0.0.1:3000/")
                if api and web == 200:
                    print("\n控制台已就绪：http://localhost:3000/", flush=True)
                    print("API 文档：http://127.0.0.1:8000/docs", flush=True)
                    announced = True
                elif time.monotonic() >= deadline:
                    raise DeploymentError(
                        "服务在等待窗口内未通过健康检查；请运行 deploy check。"
                    )
            time.sleep(0.5)
    finally:
        stop_children()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()


def model_state(values: dict[str, str]) -> dict[str, Any]:
    provider = values.get("PHOTO_STYLE_PROVIDER", "pic-style-http").lower()
    native_lock = PROJECT_ROOT / values.get(
        "PHOTO_STYLE_MODEL_LOCK", "backend/models/photo-style/model-lock.json"
    )
    remote_status = request_json(
        "http://127.0.0.1:8000/api/photo-style-transfers/provider"
    )
    managed_service: dict[str, Any] | None = None
    service_url = values.get("PHOTO_STYLE_SERVICE_URL", "")
    if provider in {"pic-style-http", "http"} and service_url.startswith(
        ("http://127.0.0.1", "http://localhost")
    ):
        root_value = values.get("PHOTO_STYLE_SERVICE_ROOT", ".capabilities/pic-style")
        service_root = Path(root_value).expanduser()
        if not service_root.is_absolute():
            service_root = PROJECT_ROOT / service_root
        process_state = read_json_file(service_root / ".web-agent-service.json")
        deployment_state = read_json_file(
            service_root / ".web-agent-deployment.json"
        )
        pid_value = process_state.get("worker_pid", process_state.get("pid"))
        pid = (
            int(pid_value)
            if isinstance(pid_value, int | str) and str(pid_value).isdigit()
            else 0
        )
        managed_service = {
            "profile": deployment_state.get("profile"),
            "managed_process_running": process_running(pid),
        }
    runtime_quality = bool(
        remote_status
        and remote_status.get("ready")
        and (remote_status.get("details") or {}).get("production_quality")
    )
    if (
        managed_service
        and managed_service.get("profile") == "windows-nvidia-real"
    ):
        runtime_quality = runtime_quality and bool(
            managed_service.get("managed_process_running")
        )
    return {
        "configured_provider": provider,
        "native_model_lock": native_lock.is_file(),
        "runtime": remote_status,
        "managed_service": managed_service,
        "production_quality": runtime_quality,
    }


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def doctor_payload() -> dict[str, Any]:
    values = read_env()
    node_version: str | None = None
    pnpm_version: str | None = None
    try:
        node_version = capture(["node", "--version"])
        pnpm_version = capture([*resolve_pnpm(), "--version"])
    except DeploymentError:
        pass
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "node": node_version,
        "pnpm": pnpm_version,
        "env_exists": (PROJECT_ROOT / ".env").is_file(),
        "venv_exists": project_python().is_file(),
        "frontend_dependencies": (PROJECT_ROOT / "node_modules").is_dir(),
        "segmentation_dependencies": dependency_available(
            "cv2", "backend/requirements-segmentation.txt"
        ),
        "memory_model": (
            PROJECT_ROOT
            / "backend/models/embeddings/granite-embedding-97m-multilingual-r2"
            / "embedding-manifest.json"
        ).is_file(),
        "spatial_models": (
            PROJECT_ROOT / "backend/models/spatial/spatial-model-lock.json"
        ).is_file(),
        "web_http": request_status("http://127.0.0.1:3000/"),
        "api_health": request_json("http://127.0.0.1:8000/health"),
        "photo_style": model_state(values),
    }


def dependency_available(module: str, requirement: str) -> dict[str, Any]:
    python = project_python()
    if not python.is_file():
        return {"installed": False, "requirement": requirement}
    completed = subprocess.run(
        [python, "-c", f"import {module}"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"installed": completed.returncode == 0, "requirement": requirement}


def doctor(args: argparse.Namespace) -> None:
    payload = doctor_payload()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"平台：{payload['platform']}")
    print(f"Python：{payload['python']}；Node：{payload['node'] or '缺失'}")
    print(f"pnpm：{payload['pnpm'] or '缺失'}")
    print(f"隔离环境：{'就绪' if payload['venv_exists'] else '缺失'}")
    print(f"前端依赖：{'就绪' if payload['frontend_dependencies'] else '缺失'}")
    print(f"空间模型：{'就绪' if payload['spatial_models'] else '缺失'}")
    print(f"语义记忆模型：{'就绪' if payload['memory_model'] else '缺失'}")
    print(f"Web：HTTP {payload['web_http'] or '未运行'}")
    health = payload.get("api_health") or {}
    print(f"Agent API：{health.get('status', '未运行')}")
    style = payload["photo_style"]
    print(f"图片风格化配置：{style['configured_provider']}")
    print(
        "真实图片 Provider："
        + ("已就绪" if style["production_quality"] else "未完成真实质量门禁")
    )


def check(_: argparse.Namespace) -> None:
    payload = doctor_payload()
    failures: list[str] = []
    if payload.get("web_http") != 200:
        failures.append("Web 控制台未返回 HTTP 200")
    health = payload.get("api_health") or {}
    if health.get("status") != "ok":
        failures.append("Agent API 健康检查失败")
    if not payload.get("spatial_models"):
        failures.append("Depth Anything V2 / BiRefNet 固定模型未准备")
    if not (payload.get("segmentation_dependencies") or {}).get("installed"):
        failures.append("BiRefNet 分割依赖未安装")
    if not payload.get("memory_model"):
        failures.append("本地语义记忆模型未准备")
    style = payload.get("photo_style") or {}
    if not style.get("production_quality"):
        failures.append("图片风格化不是已就绪的真实 Provider")
    if failures:
        print("验收未通过：")
        for failure in failures:
            print(f"- {failure}")
        raise DeploymentError("完整部署验收失败。")
    print("完整部署验收通过：Web、Agent API 和真实图片风格化 Provider 均就绪。")


def ensure_services_stopped() -> None:
    running = [
        label
        for label, url in (
            ("Web", "http://127.0.0.1:3000/"),
            ("Agent API", "http://127.0.0.1:8000/health"),
            ("图片风格化服务", "http://127.0.0.1:18000/health/ready"),
        )
        if request_status(url, timeout=0.5) is not None
    ]
    if running:
        raise DeploymentError(
            "迁移前必须停止服务，避免复制到不一致的 SQLite/WAL 状态："
            + "、".join(running)
        )


def bundle_passphrase(*, confirm: bool, passphrase_file: Path | None) -> str:
    if passphrase_file:
        try:
            value = passphrase_file.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise DeploymentError("无法读取迁移口令文件。") from exc
    else:
        value = getpass.getpass("迁移包口令：")
        if confirm:
            repeated = getpass.getpass("再次输入迁移包口令：")
            if value != repeated:
                raise DeploymentError("两次迁移口令不一致。")
    if len(value) < 12:
        raise DeploymentError("迁移口令至少需要 12 个字符。")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_files(*, include_models: bool, include_capabilities: bool) -> list[Path]:
    roots = [PROJECT_ROOT / "backend/data"]
    if include_models:
        roots.append(PROJECT_ROOT / "backend/models")
    if include_capabilities:
        roots.append(PROJECT_ROOT / ".capabilities")
    files: list[Path] = []
    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        files.append(env_path)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(PROJECT_ROOT)
            if relative.as_posix() == "backend/data/viewer-public-url.json":
                continue
            files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(PROJECT_ROOT).as_posix())


def export_runtime_secrets() -> dict[str, str]:
    data_dir = PROJECT_ROOT / "backend/data"
    secrets: dict[str, str] = {}
    try:
        from backend.app.channel_settings import create_default_feishu_settings_service
        from backend.app.settings import create_default_settings_service

        llm = create_default_settings_service(data_dir / "settings.json").secret_store.get()
        feishu = create_default_feishu_settings_service(
            data_dir / "feishu_settings.json"
        ).secret_store.get()
        if llm:
            secrets["llm_api_key"] = llm
        if feishu:
            secrets["feishu_app_secret"] = feishu
    except Exception as exc:  # keyring backends differ by operating system
        print(f"警告：无法从系统钥匙串导出部分应用 Secret：{exc}", flush=True)
    memory_db = data_dir / "agent_memory.sqlite3"
    if memory_db.is_file():
        try:
            from backend.app.memory_crypto import load_memory_key

            secrets["memory_encryption_key"] = base64.urlsafe_b64encode(
                load_memory_key(memory_db)
            ).decode("ascii")
        except Exception as exc:
            raise DeploymentError("无法导出长期记忆加密密钥。") from exc
    return secrets


def encrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    header = json.dumps(
        {
            "version": 1,
            "kdf": "scrypt-n32768-r8-p1",
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with source.open("rb") as plain, destination.open("wb") as encrypted:
        encrypted.write(BUNDLE_MAGIC)
        encrypted.write(struct.pack(">I", len(header)))
        encrypted.write(header)
        for chunk in iter(lambda: plain.read(1024 * 1024), b""):
            encrypted.write(encryptor.update(chunk))
        encrypted.write(encryptor.finalize())
        encrypted.write(encryptor.tag)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass


def decrypt_file(source: Path, destination: Path, passphrase: str) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    with source.open("rb") as encrypted:
        if encrypted.read(len(BUNDLE_MAGIC)) != BUNDLE_MAGIC:
            raise DeploymentError("不是受支持的个人数字助手迁移包。")
        raw_length = encrypted.read(4)
        if len(raw_length) != 4:
            raise DeploymentError("迁移包头损坏。")
        header_length = struct.unpack(">I", raw_length)[0]
        if header_length > 16 * 1024:
            raise DeploymentError("迁移包头长度异常。")
        try:
            header = json.loads(encrypted.read(header_length).decode("utf-8"))
            salt = base64.b64decode(header["salt"])
            nonce = base64.b64decode(header["nonce"])
        except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
            raise DeploymentError("迁移包头无法解析。") from exc
        ciphertext_offset = encrypted.tell()
        encrypted.seek(0, os.SEEK_END)
        total = encrypted.tell()
        ciphertext_length = total - ciphertext_offset - 16
        if ciphertext_length < 0:
            raise DeploymentError("迁移包内容不完整。")
        encrypted.seek(total - 16)
        tag = encrypted.read(16)
        encrypted.seek(ciphertext_offset)
        key = hashlib.scrypt(
            passphrase.encode("utf-8"),
            salt=salt,
            n=2**15,
            r=8,
            p=1,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        )
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = ciphertext_length
        try:
            with destination.open("wb") as plain:
                while remaining:
                    chunk = encrypted.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DeploymentError("迁移包密文被截断。")
                    remaining -= len(chunk)
                    plain.write(decryptor.update(chunk))
                plain.write(decryptor.finalize())
        except InvalidTag as exc:
            destination.unlink(missing_ok=True)
            raise DeploymentError("迁移口令错误或迁移包已被篡改。") from exc


def backup(args: argparse.Namespace) -> None:
    ensure_services_stopped()
    python = project_python()
    if not python.is_file():
        raise DeploymentError("请先完成基础部署，再创建加密迁移包。")
    output = args.output.expanduser().resolve()
    if output.exists() and not args.force:
        raise DeploymentError("目标迁移包已存在；如确认覆盖请追加 --force。")
    output.parent.mkdir(parents=True, exist_ok=True)
    passphrase = bundle_passphrase(
        confirm=True, passphrase_file=args.passphrase_file
    )
    files = runtime_files(
        include_models=args.include_models,
        include_capabilities=args.include_capabilities,
    )
    with tempfile.TemporaryDirectory(prefix="pda-backup-") as temporary_name:
        temporary = Path(temporary_name)
        secrets_path = temporary / "secrets.json"
        secrets_path.write_text(
            json.dumps(export_runtime_secrets(), ensure_ascii=False), encoding="utf-8"
        )
        archive_path = temporary / "runtime.tar.gz"
        manifest = {
            "schema_version": "pda_runtime_bundle_v1",
            "created_at": int(time.time()),
            "source_platform": platform.platform(),
            "files": [
                {
                    "path": path.relative_to(PROJECT_ROOT).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
                for path in files
            ],
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(manifest_path, arcname="_migration/manifest.json")
            archive.add(secrets_path, arcname="_migration/secrets.json")
            for path in files:
                archive.add(path, arcname=path.relative_to(PROJECT_ROOT).as_posix())
        encrypt_file(archive_path, output, passphrase)
    print(f"已生成加密迁移包：{output}")
    print(f"文件数：{len(files)}；请将口令通过独立渠道保管。")


def restore_runtime_secrets(secrets: dict[str, str]) -> None:
    data_dir = PROJECT_ROOT / "backend/data"
    from backend.app.channel_settings import create_default_feishu_settings_service
    from backend.app.settings import create_default_settings_service

    if secrets.get("llm_api_key"):
        create_default_settings_service(data_dir / "settings.json").secret_store.set(
            secrets["llm_api_key"]
        )
    if secrets.get("feishu_app_secret"):
        create_default_feishu_settings_service(
            data_dir / "feishu_settings.json"
        ).secret_store.set(secrets["feishu_app_secret"])
    if secrets.get("memory_encryption_key"):
        memory_key_path = data_dir / "agent_memory.sqlite3.memory-key"
        memory_key_path.parent.mkdir(parents=True, exist_ok=True)
        memory_key_path.write_text(
            secrets["memory_encryption_key"], encoding="ascii"
        )
        try:
            os.chmod(memory_key_path, 0o600)
        except OSError:
            pass


def safe_member_destination(name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise DeploymentError(f"迁移包包含不安全路径：{name}")
    destination = (PROJECT_ROOT / relative).resolve()
    try:
        destination.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise DeploymentError(f"迁移包路径越界：{name}") from exc
    return destination


def restore(args: argparse.Namespace) -> None:
    ensure_services_stopped()
    source = args.bundle.expanduser().resolve()
    if not source.is_file():
        raise DeploymentError("迁移包不存在。")
    if not project_python().is_file():
        raise DeploymentError("请先运行 deploy install，再恢复运行数据。")
    passphrase = bundle_passphrase(
        confirm=False, passphrase_file=args.passphrase_file
    )
    with tempfile.TemporaryDirectory(prefix="pda-restore-") as temporary_name:
        archive_path = Path(temporary_name) / "runtime.tar.gz"
        decrypt_file(source, archive_path, passphrase)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if any(not member.isfile() for member in members):
                raise DeploymentError("迁移包包含非普通文件条目。")
            data_members = [
                member for member in members if not member.name.startswith("_migration/")
            ]
            destinations = [safe_member_destination(member.name) for member in data_members]
            existing = [path for path in destinations if path.exists()]
            if existing and not args.force:
                preview = "、".join(str(path.relative_to(PROJECT_ROOT)) for path in existing[:5])
                raise DeploymentError(
                    f"目标已有运行数据（{preview}）；确认覆盖时追加 --force。"
                )
            manifest_member = archive.getmember("_migration/manifest.json")
            secrets_member = archive.getmember("_migration/secrets.json")
            manifest_stream = archive.extractfile(manifest_member)
            secrets_stream = archive.extractfile(secrets_member)
            if manifest_stream is None or secrets_stream is None:
                raise DeploymentError("迁移包缺少清单或 Secret。")
            manifest = json.loads(manifest_stream.read().decode("utf-8"))
            expected = {
                item["path"]: item
                for item in manifest.get("files", [])
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            staged_files: list[tuple[Path, Path]] = []
            staging_root = Path(temporary_name) / "staging"
            for member, destination in zip(data_members, destinations, strict=True):
                source_stream = archive.extractfile(member)
                if source_stream is None:
                    raise DeploymentError(f"无法读取迁移文件：{member.name}")
                staged = staging_root / member.name
                staged.parent.mkdir(parents=True, exist_ok=True)
                with staged.open("wb") as output:
                    shutil.copyfileobj(source_stream, output)
                record = expected.get(member.name)
                if not record or sha256_file(staged) != record.get("sha256"):
                    raise DeploymentError(f"迁移文件校验失败：{member.name}")
                staged_files.append((staged, destination))
            for staged, destination in staged_files:
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(destination)
            secrets = json.loads(secrets_stream.read().decode("utf-8"))
            restore_runtime_secrets(secrets if isinstance(secrets, dict) else {})
    print("运行数据、配置和可迁移 Secret 已恢复。")
    print("请运行 deploy doctor，再启动服务完成验收。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="个人数字助手跨平台统一安装、启动与验收工具。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install", help="安装项目与所选能力")
    install_parser.add_argument(
        "--profile",
        choices=["core", "complete"],
        default="complete",
        help="默认 complete：Agent、空间分割、语义记忆和真实图片风格化",
    )
    install_parser.add_argument(
        "--photo-style",
        choices=["real", "test", "skip"],
        default="real",
        help="默认 real；test 仅显式开发测试，绝不代表真实效果",
    )
    install_parser.add_argument(
        "--photo-style-url",
        help="没有本地加速器时使用的受保护远程 GPU 服务 URL",
    )
    install_parser.add_argument("--accept-model-licenses", action="store_true")
    install_parser.add_argument("--skip-spatial-models", action="store_true")
    install_parser.add_argument("--skip-memory-model", action="store_true")
    install_parser.set_defaults(handler=install)

    start_parser = subparsers.add_parser("start", help="统一启动模型服务、API 与 Web")
    start_parser.add_argument("--wait-seconds", type=int, default=180)
    start_parser.set_defaults(handler=start)

    doctor_parser = subparsers.add_parser("doctor", help="检查安装、运行与模型状态")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=doctor)

    check_parser = subparsers.add_parser("check", help="执行完整运行验收")
    check_parser.set_defaults(handler=check)

    backup_parser = subparsers.add_parser(
        "backup", help="停止服务后导出加密的配置、会话、记忆与资产迁移包"
    )
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--include-models", action="store_true")
    backup_parser.add_argument("--include-capabilities", action="store_true")
    backup_parser.add_argument("--passphrase-file", type=Path)
    backup_parser.add_argument("--force", action="store_true")
    backup_parser.set_defaults(handler=backup)

    restore_parser = subparsers.add_parser(
        "restore", help="在新电脑恢复加密迁移包"
    )
    restore_parser.add_argument("bundle", type=Path)
    restore_parser.add_argument("--passphrase-file", type=Path)
    restore_parser.add_argument("--force", action="store_true")
    restore_parser.set_defaults(handler=restore)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except DeploymentError as exc:
        parser.exit(2, f"部署失败：{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
