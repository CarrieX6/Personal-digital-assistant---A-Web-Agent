from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_REPOSITORY = "https://github.com/frogi-m/pic-style.git"
PINNED_SOURCE_REF = "c8e0b641f7f334faf73167211b5f0a8033e3b1dc"
DEFAULT_SERVICE_ROOT = PROJECT_ROOT / ".capabilities" / "pic-style"
DEFAULT_SERVICE_URL = "http://127.0.0.1:18000"
DEPLOYMENT_STATE_NAME = ".web-agent-deployment.json"
PROCESS_STATE_NAME = ".web-agent-service.json"
LOG_DIRECTORY_NAME = ".web-agent-logs"
SOURCE_STATE_NAME = ".web-agent-source.json"


class DeploymentError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    printable = [str(part) for part in command]
    try:
        return subprocess.run(
            printable,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DeploymentError(f"未找到命令：{printable[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(
            f"命令超过 {timeout:g} 秒未完成：{' '.join(printable)}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f"\n{detail}" if detail else ""
        raise DeploymentError(
            f"命令执行失败（exit={exc.returncode}）：{' '.join(printable)}{suffix}"
        ) from exc


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def update_env(path: Path, updates: dict[str, str]) -> None:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def service_python(service_root: Path) -> Path:
    if os.name == "nt":
        return service_root / ".venv" / "Scripts" / "python.exe"
    return service_root / ".venv" / "bin" / "python"


def git_commit(service_root: Path) -> str | None:
    if (service_root / ".git").is_dir():
        completed = run(
            ["git", "-C", service_root, "rev-parse", "HEAD"], capture=True
        )
        return completed.stdout.strip()
    try:
        payload = json.loads(
            (service_root / SOURCE_STATE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return None
    commit = payload.get("resolved_commit") if isinstance(payload, dict) else None
    return str(commit) if commit else None


def read_source_state(service_root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            (service_root / SOURCE_STATE_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_source_state(
    service_root: Path,
    *,
    requested_ref: str,
    resolved_commit: str,
    method: str,
) -> None:
    (service_root / SOURCE_STATE_NAME).write_text(
        json.dumps(
            {
                "schema_version": "photo_style_source_v1",
                "source_repository": SOURCE_REPOSITORY,
                "requested_ref": requested_ref,
                "resolved_commit": resolved_commit,
                "install_method": method,
                "installed_at": utc_now(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def resolve_source_ref(source_ref: str) -> str:
    url = f"https://api.github.com/repos/frogi-m/pic-style/commits/{source_ref}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "web-agent-installer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise DeploymentError(
            "Git 克隆失败，且无法通过 GitHub API 解析固定源码版本。"
        ) from exc
    commit = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(commit, str) or len(commit) != 40:
        raise DeploymentError("GitHub API 没有返回有效源码提交。")
    return commit


def install_source_archive(service_root: Path, source_ref: str) -> str:
    resolved_commit = resolve_source_ref(source_ref)
    archive_url = (
        "https://api.github.com/repos/frogi-m/pic-style/tarball/"
        + resolved_commit
    )
    service_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="pic-style-archive-",
        dir=service_root.parent,
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        archive_path = temporary_root / "source.tar.gz"
        request = urllib.request.Request(
            archive_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "web-agent-installer",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with archive_path.open("wb") as output:
                    shutil.copyfileobj(response, output)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DeploymentError("无法下载 GitHub API 固定源码归档。") from exc
        extract_root = temporary_root / "extracted"
        extract_root.mkdir()
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                members = archive.getmembers()
                if any(member.issym() or member.islnk() for member in members):
                    raise DeploymentError("源码归档包含不允许的链接条目。")
                archive.extractall(extract_root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise DeploymentError("无法校验或展开源码归档。") from exc
        roots = [path for path in extract_root.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise DeploymentError("源码归档目录结构不符合预期。")
        shutil.move(str(roots[0]), str(service_root))
    write_source_state(
        service_root,
        requested_ref=source_ref,
        resolved_commit=resolved_commit,
        method="github-api-archive",
    )
    return resolved_commit


def checkout_new_git(service_root: Path, source_ref: str) -> str:
    service_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = service_root.with_name(
        f".{service_root.name}.git-install-{os.getpid()}"
    )
    if temporary_root.exists():
        raise DeploymentError(f"临时安装目录已存在：{temporary_root}")
    try:
        run(
            [
                "git",
                "-c",
                "http.version=HTTP/1.1",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                SOURCE_REPOSITORY,
                temporary_root,
            ],
            timeout=30,
        )
        run(
            [
                "git",
                "-c",
                "http.version=HTTP/1.1",
                "-C",
                temporary_root,
                "fetch",
                "origin",
                source_ref,
                "--depth",
                "1",
            ],
            timeout=30,
        )
        run(["git", "-C", temporary_root, "checkout", "--detach", "FETCH_HEAD"])
        commit = git_commit(temporary_root)
        if not commit:
            raise DeploymentError("无法确认 pic-style 安装提交。")
        write_source_state(
            temporary_root,
            requested_ref=source_ref,
            resolved_commit=commit,
            method="git",
        )
        temporary_root.replace(service_root)
        return commit
    except DeploymentError:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise


def ensure_checkout(service_root: Path, source_ref: str) -> str:
    if service_root.exists() and not (service_root / ".git").is_dir():
        commit = git_commit(service_root)
        if commit:
            installed_ref = read_source_state(service_root).get("requested_ref")
            if source_ref not in {installed_ref, commit}:
                raise DeploymentError(
                    "归档安装已经固定到其他版本；为避免覆盖环境和数据，请先按迁移文档"
                    "创建备份，再安装到新的目录。"
                )
            return commit
        raise DeploymentError(
            f"安装目录已存在但来源不可验证：{service_root}。请人工检查后重试。"
        )
    if not service_root.exists():
        try:
            return checkout_new_git(service_root, source_ref)
        except DeploymentError as exc:
            print(
                f"标准 Git 安装失败，改用 GitHub API 固定源码归档：{exc}",
                flush=True,
            )
            return install_source_archive(service_root, source_ref)
    dirty = run(
        ["git", "-C", service_root, "status", "--porcelain"], capture=True
    ).stdout.strip()
    if dirty:
        raise DeploymentError(
            "pic-style 安装目录存在未提交修改；为避免覆盖，请先人工处理。"
        )
    run(
        [
            "git",
            "-c",
            "http.version=HTTP/1.1",
            "-C",
            service_root,
            "fetch",
            "origin",
            source_ref,
            "--depth",
            "1",
        ]
    )
    run(["git", "-C", service_root, "checkout", "--detach", "FETCH_HEAD"])
    commit = git_commit(service_root)
    if not commit:
        raise DeploymentError("无法确认 pic-style 安装提交。")
    return commit


def install_service_environment(service_root: Path) -> None:
    python = service_python(service_root)
    if not python.is_file():
        run([sys.executable, "-m", "venv", service_root / ".venv"])
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    run([python, "-m", "pip", "install", "--editable", service_root])


def configure_test_service(service_root: Path, port: int, commit: str) -> None:
    env_path = service_root / ".env"
    example = service_root / ".env.example"
    if not env_path.exists() and example.is_file():
        shutil.copyfile(example, env_path)
    update_env(
        env_path,
        {
            "PHOTO_STYLE_ENVIRONMENT": "local",
            "PHOTO_STYLE_DATABASE_URL": "sqlite:///./data/photo_style.sqlite3",
            "PHOTO_STYLE_ASSET_BACKEND": "local",
            "PHOTO_STYLE_ASSET_ROOT": "./data/assets",
            "PHOTO_STYLE_PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "PHOTO_STYLE_API_PORT": str(port),
            "PHOTO_STYLE_QUEUE_BACKEND": "inprocess",
            "PHOTO_STYLE_PROVIDER": "fake",
            "PHOTO_STYLE_API_KEY": "",
            "PHOTO_STYLE_ALLOW_INLINE_BASE64": "false",
            "PHOTO_STYLE_ALLOW_REMOTE_URLS": "false",
            "PHOTO_STYLE_ALLOW_MODEL_DOWNLOADS": "false",
            "PHOTO_STYLE_BUILD_VERSION": commit,
        },
    )


def configure_agent(service_root: Path, port: int, *, auto_start: bool) -> None:
    relative_root = os.path.relpath(service_root, PROJECT_ROOT)
    update_env(
        PROJECT_ROOT / ".env",
        {
            "PHOTO_STYLE_PROVIDER": "pic-style-http",
            "PHOTO_STYLE_SERVICE_URL": f"http://127.0.0.1:{port}",
            "PHOTO_STYLE_TENANT_ID": "personal-agent",
            "PHOTO_STYLE_TIMEOUT_SECONDS": "900",
            "PHOTO_STYLE_AUTO_START": "true" if auto_start else "false",
            "PHOTO_STYLE_SERVICE_ROOT": relative_root,
        },
    )


def write_deployment_state(
    service_root: Path,
    *,
    source_ref: str,
    commit: str,
    port: int,
    profile: str,
) -> None:
    payload = {
        "schema_version": "photo_style_deployment_v1",
        "installed_at": utc_now(),
        "source_repository": SOURCE_REPOSITORY,
        "requested_ref": source_ref,
        "resolved_commit": commit,
        "profile": profile,
        "service_url": f"http://127.0.0.1:{port}",
        "agent_project": str(PROJECT_ROOT),
        "runtime_downloads_allowed": False,
    }
    (service_root / DEPLOYMENT_STATE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def request_json(url: str, *, timeout: float = 3) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def process_state_path(service_root: Path) -> Path:
    return service_root / PROCESS_STATE_NAME


def read_process_state(service_root: Path) -> dict[str, Any]:
    path = process_state_path(service_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def process_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def start_service(service_root: Path, port: int, wait_seconds: int = 60) -> None:
    ready_url = f"http://127.0.0.1:{port}/health/ready"
    if request_json(ready_url):
        print(f"图片风格化服务已在运行：{ready_url}")
        return
    python = service_python(service_root)
    entrypoint = service_root / "scripts" / "run_local_api.py"
    if not python.is_file() or not entrypoint.is_file():
        raise DeploymentError(
            "服务尚未安装，请先运行 deploy-test 或 install-service。"
        )
    logs = service_root / LOG_DIRECTORY_NAME
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "api.log"
    log_handle = log_path.open("ab")
    command = [str(python), str(entrypoint), "--host", "127.0.0.1", "--port", str(port)]
    kwargs: dict[str, Any] = {
        "cwd": service_root,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    log_handle.close()
    process_state_path(service_root).write_text(
        json.dumps(
            {
                "pid": process.pid,
                "started_at": utc_now(),
                "port": port,
                "log_path": str(log_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + max(1, wait_seconds)
    while time.monotonic() < deadline:
        payload = request_json(ready_url)
        if payload and payload.get("status") == "ready":
            provider = request_json(f"http://127.0.0.1:{port}/health/provider") or {}
            print(f"图片风格化服务已启动：{ready_url}")
            print(f"Provider：{provider.get('provider', 'unknown')}")
            return
        if process.poll() is not None:
            break
        time.sleep(0.5)
    if process.poll() is None:
        process.terminate()
    raise DeploymentError(f"服务未能就绪，请查看日志：{log_path}")


def stop_service(service_root: Path) -> None:
    state = read_process_state(service_root)
    pid_value = state.get("pid")
    pid = int(pid_value) if isinstance(pid_value, int | str) and str(pid_value).isdigit() else 0
    if not process_is_running(pid):
        process_state_path(service_root).unlink(missing_ok=True)
        print("没有由部署管理器启动的图片风格化服务。")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise DeploymentError(f"无法停止进程 {pid}。") from exc
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process_is_running(pid):
        time.sleep(0.2)
    process_state_path(service_root).unlink(missing_ok=True)
    print(f"已停止图片风格化服务进程：{pid}")


def memory_bytes() -> int | None:
    if os.name != "nt" and hasattr(os, "sysconf"):
        try:
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            return pages * page_size
        except (OSError, ValueError):
            return None
    return None


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def nvidia_summary() -> str | None:
    if not command_available("nvidia-smi"):
        return None
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def torch_accelerator_summary() -> dict[str, Any]:
    script = (
        "import json, torch; "
        "cuda = bool(torch.cuda.is_available()); "
        "mps_backend = getattr(torch.backends, 'mps', None); "
        "mps = bool(mps_backend is not None and mps_backend.is_available()); "
        "print(json.dumps({'torch': torch.__version__, "
        "'cuda_available': cuda, 'mps_available': mps}))"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        return payload if isinstance(payload, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError):
        return {
            "torch": None,
            "cuda_available": False,
            "mps_available": False,
        }


def validate_remote_service_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DeploymentError("远程 Provider URL 必须是完整的 http(s) 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DeploymentError("远程 Provider URL 不能包含凭据、查询参数或片段。")
    if parsed.scheme == "http":
        host = parsed.hostname.lower()
        private = host == "localhost"
        try:
            private = private or ipaddress.ip_address(host).is_private
        except ValueError:
            pass
        if not private:
            raise DeploymentError("公网远程 Provider 必须使用 HTTPS。")
    return candidate


def configure_remote_agent(service_url: str, tenant_id: str) -> None:
    safe_url = validate_remote_service_url(service_url)
    safe_tenant = tenant_id.strip()
    if not safe_tenant or len(safe_tenant) > 128:
        raise DeploymentError("tenant 必须是 1 至 128 个字符。")
    update_env(
        PROJECT_ROOT / ".env",
        {
            "PHOTO_STYLE_PROVIDER": "pic-style-http",
            "PHOTO_STYLE_SERVICE_URL": safe_url,
            "PHOTO_STYLE_TENANT_ID": safe_tenant,
            "PHOTO_STYLE_AUTO_START": "false",
        },
    )
    print(f"Web Agent 已配置远程图片风格化 Provider：{safe_url}")
    print("API Key 不会通过命令行写入；请用本机 Secret 配置单独注入。")


def doctor(service_root: Path, port: int) -> dict[str, Any]:
    nvidia = nvidia_summary()
    torch_summary = torch_accelerator_summary()
    system = platform.system()
    windows_cuda = system == "Windows" and bool(nvidia)
    macos_mps = (
        system == "Darwin"
        and platform.machine().lower() in {"arm64", "aarch64"}
        and bool(torch_summary.get("mps_available"))
    )
    real_supported = windows_cuda or macos_mps
    supported_profiles = ["remote-http"]
    if windows_cuda:
        supported_profiles.append("windows-nvidia-service")
    if macos_mps:
        supported_profiles.append("macos-mps-native")
    if windows_cuda:
        reason = "verified_windows_nvidia_path"
    elif macos_mps:
        reason = "macos_mps_engineering_path_requires_local_quality_gate"
    else:
        reason = "no supported local CUDA or Apple MPS accelerator detected"
    state = read_process_state(service_root)
    pid_value = state.get("pid")
    pid = int(pid_value) if isinstance(pid_value, int | str) and str(pid_value).isdigit() else None
    return {
        "platform": system,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "memory_bytes": memory_bytes(),
        "disk_free_bytes": shutil.disk_usage(PROJECT_ROOT).free,
        "git_available": command_available("git"),
        "docker_available": command_available("docker"),
        "nvidia": nvidia,
        "torch_accelerators": torch_summary,
        "supported_real_profiles": supported_profiles,
        "real_provider_automatic_deployment_supported": real_supported,
        "real_provider_reason": reason,
        "service_root": str(service_root),
        "service_commit": git_commit(service_root),
        "service_environment_ready": service_python(service_root).is_file(),
        "managed_process_running": process_is_running(pid),
        "service_ready": request_json(
            f"http://127.0.0.1:{port}/health/ready"
        ),
        "provider": request_json(
            f"http://127.0.0.1:{port}/health/provider"
        ),
    }


def print_status(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"平台：{payload['platform']} {payload['architecture']}")
    print(f"Python：{payload['python']}")
    print(f"服务目录：{payload['service_root']}")
    print(f"服务提交：{payload['service_commit'] or '未安装'}")
    print(f"隔离环境：{'已安装' if payload['service_environment_ready'] else '未安装'}")
    print(f"服务就绪：{'是' if payload['service_ready'] else '否'}")
    provider = payload.get("provider") or {}
    print(f"Provider：{provider.get('provider', '不可用')}")
    torch_summary = payload.get("torch_accelerators") or {}
    print(
        "Torch 加速器："
        f"CUDA={'是' if torch_summary.get('cuda_available') else '否'}，"
        f"MPS={'是' if torch_summary.get('mps_available') else '否'}"
    )
    print(f"可用部署画像：{', '.join(payload['supported_real_profiles'])}")
    if payload["real_provider_reason"] == "verified_windows_nvidia_path":
        print(
            "本机真实模型准备：Windows NVIDIA 工程路径可用，仍需本机质量门禁"
        )
    elif payload["real_provider_reason"] == (
        "macos_mps_engineering_path_requires_local_quality_gate"
    ):
        print("本机真实模型准备：macOS MPS 工程路径可用，真实质量尚未验收")
    else:
        print("本机真实模型准备：没有检测到受支持的本地加速器")
    if not payload["real_provider_automatic_deployment_supported"]:
        print(f"原因：{payload['real_provider_reason']}")


def deploy_test(service_root: Path, source_ref: str, port: int) -> None:
    print("[1/5] 获取固定版本的独立 pic-style 服务", flush=True)
    commit = ensure_checkout(service_root, source_ref)
    print("[2/5] 创建独立 Python 环境并安装服务依赖", flush=True)
    install_service_environment(service_root)
    print(
        "[3/5] 配置 Fake Provider（只用于契约和端到端链路测试）",
        flush=True,
    )
    configure_test_service(service_root, port, commit)
    print("[4/5] 配置 Web Agent HTTP Provider 与自动启动", flush=True)
    configure_agent(service_root, port, auto_start=True)
    write_deployment_state(
        service_root,
        source_ref=source_ref,
        commit=commit,
        port=port,
        profile="test-service",
    )
    print("[5/5] 启动服务并执行健康检查", flush=True)
    start_service(service_root, port)
    print(
        "测试服务部署完成。注意：Fake Provider 不是 SDXL，不能用于效果验收。"
    )


def plan_real_models() -> None:
    print(
        "以下命令只输出固定权重、下载量和许可证链接，不会下载模型。",
        flush=True,
    )
    run(
        [
            sys.executable,
            PROJECT_ROOT / "backend" / "scripts" / "prepare_photo_style_models.py",
            "--plan",
        ],
        cwd=PROJECT_ROOT,
    )


def prepare_windows_gpu(
    service_root: Path,
    *,
    accept_model_licenses: bool,
) -> None:
    info = doctor(service_root, 18000)
    if "windows-nvidia-service" not in info["supported_real_profiles"]:
        raise DeploymentError(
            "该自动化路径只对 Windows + NVIDIA CUDA 开放。当前设备可运行测试服务，"
            "真实 SDXL 请部署到 Windows GPU 电脑或配置受保护的远端 Provider。"
        )
    python = service_python(service_root)
    if not python.is_file():
        raise DeploymentError("请先运行 install-service 安装独立服务。")
    run([python, "-m", "pip", "install", "--editable", f"{service_root}[gpu,s3]"])
    plan_command = [
        python,
        service_root / "scripts" / "prepare_models.py",
        "--manifest",
        service_root / "config" / "model-manifest.yaml",
        "--plan",
    ]
    run(plan_command, cwd=service_root)
    if not accept_model_licenses:
        raise DeploymentError(
            "已完成硬件与许可证计划展示。审阅所有许可证后，显式追加 "
            "--accept-model-licenses 才会下载约 9.84 GiB 权重。"
        )
    run(
        [
            python,
            service_root / "scripts" / "prepare_models.py",
            "--manifest",
            service_root / "config" / "model-manifest.yaml",
            "--download",
            "--accept-model-licenses",
            "--verify-checksums",
        ],
        cwd=service_root,
    )
    print(
        "模型已准备，但尚未自动启用真实 Provider。请继续执行 benchmark_local.py 的 "
        "preflight、gate 和人工质量确认，再切换 Provider。"
    )


def prepare_macos_mps(*, accept_model_licenses: bool) -> None:
    info = doctor(DEFAULT_SERVICE_ROOT, 18000)
    if "macos-mps-native" not in info["supported_real_profiles"]:
        raise DeploymentError(
            "该路径要求 Apple Silicon、可用的 PyTorch MPS 与 arm64 Python。"
        )
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            PROJECT_ROOT / "backend" / "requirements-gpu.txt",
        ],
        cwd=PROJECT_ROOT,
    )
    plan_real_models()
    if not accept_model_licenses:
        raise DeploymentError(
            "已完成 MPS 与依赖预检。审阅全部模型许可证后，显式追加 "
            "--accept-model-licenses 才会下载约 9.84 GiB 权重。"
        )
    run(
        [
            sys.executable,
            PROJECT_ROOT / "backend" / "scripts" / "prepare_photo_style_models.py",
            "--download",
            "--accept-model-licenses",
            "--verify-checksums",
        ],
        cwd=PROJECT_ROOT,
    )
    update_env(
        PROJECT_ROOT / ".env",
        {
            "PHOTO_STYLE_PROVIDER": "sdxl-local",
            "PHOTO_STYLE_ACCELERATOR": "mps",
            "PHOTO_STYLE_AUTO_START": "false",
            "PHOTO_STYLE_LCM_PREVIEW_ENABLED": "false",
            "PHOTO_STYLE_UNLOAD_AFTER_GENERATION": "true",
            "PYTORCH_ENABLE_MPS_FALLBACK": "1",
        },
    )
    print(
        "MPS 模型与配置已准备，但 production_quality 仍为 false。请重启 Agent，"
        "完成固定样本 smoke、十次稳定性、统一内存、功耗和人工质量门禁。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="图片风格化独立服务的跨平台安装、启动、诊断与迁移管理器。"
    )
    parser.add_argument(
        "--service-root",
        type=Path,
        default=Path(os.getenv("PHOTO_STYLE_SERVICE_ROOT", DEFAULT_SERVICE_ROOT)),
    )
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--source-ref", default=PINNED_SOURCE_REF)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="检查安装、进程、服务与 Provider")
    status_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("doctor", help="输出当前设备部署能力")
    subparsers.add_parser("deploy-test", help="一键部署安全的 Fake 契约测试服务")
    subparsers.add_parser("install-service", help="安装固定版本服务和隔离依赖")
    subparsers.add_parser("configure-agent", help="写入 Web Agent 的 HTTP Provider 配置")
    subparsers.add_parser("start", help="启动已安装的独立服务")
    subparsers.add_parser("stop", help="停止由本管理器启动的服务")
    subparsers.add_parser("plan-real", help="展示真实模型、下载量和许可证，不下载")
    gpu_parser = subparsers.add_parser(
        "prepare-windows-gpu",
        help="准备 Windows NVIDIA 依赖和模型；不会自动越过人工质量门禁",
    )
    gpu_parser.add_argument("--accept-model-licenses", action="store_true")
    mps_parser = subparsers.add_parser(
        "prepare-macos-mps",
        help="准备 Apple Silicon MPS 依赖与模型；不会自动越过质量门禁",
    )
    mps_parser.add_argument("--accept-model-licenses", action="store_true")
    remote_parser = subparsers.add_parser(
        "configure-remote",
        help="配置受保护的远程 GPU Provider；不在命令行接收密钥",
    )
    remote_parser.add_argument("--url", required=True)
    remote_parser.add_argument("--tenant", default="personal-agent")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    service_root = args.service_root.expanduser()
    if not service_root.is_absolute():
        service_root = (PROJECT_ROOT / service_root).resolve()
    try:
        if args.command in {"status", "doctor"}:
            print_status(
                doctor(service_root, args.port),
                as_json=bool(getattr(args, "json", False)),
            )
        elif args.command == "deploy-test":
            deploy_test(service_root, args.source_ref, args.port)
        elif args.command == "install-service":
            commit = ensure_checkout(service_root, args.source_ref)
            install_service_environment(service_root)
            configure_test_service(service_root, args.port, commit)
            write_deployment_state(
                service_root,
                source_ref=args.source_ref,
                commit=commit,
                port=args.port,
                profile="service-only",
            )
            print(f"服务环境已安装：{service_root}")
        elif args.command == "configure-agent":
            configure_agent(service_root, args.port, auto_start=True)
            print("Web Agent 配置已更新；后端进程需重启后加载新环境变量。")
        elif args.command == "start":
            start_service(service_root, args.port)
        elif args.command == "stop":
            stop_service(service_root)
        elif args.command == "plan-real":
            plan_real_models()
        elif args.command == "prepare-windows-gpu":
            prepare_windows_gpu(
                service_root,
                accept_model_licenses=args.accept_model_licenses,
            )
        elif args.command == "prepare-macos-mps":
            prepare_macos_mps(
                accept_model_licenses=args.accept_model_licenses,
            )
        elif args.command == "configure-remote":
            configure_remote_agent(args.url, args.tenant)
        else:
            parser.error(f"unknown command: {args.command}")
    except DeploymentError as exc:
        parser.exit(2, f"部署失败：{exc}\n")


if __name__ == "__main__":
    main()
