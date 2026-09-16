from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_JOB_STATES = {"queued", "checking", "downloading", "installing", "validating"}
TERMINAL_JOB_STATES = {"ready", "failed", "cancelled"}
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}

CapabilityState = Literal[
    "not_installed",
    "checking",
    "incompatible",
    "license_required",
    "ready_to_install",
    "downloading",
    "installing",
    "validating",
    "ready",
    "degraded",
    "failed",
    "update_available",
]
JobState = Literal[
    "queued",
    "checking",
    "downloading",
    "installing",
    "validating",
    "ready",
    "failed",
    "cancelled",
]


class CapabilitySetupError(ValueError):
    """Raised when a local capability cannot be safely installed."""


class HostDependency(BaseModel):
    id: str
    name: str
    status: Literal["ready", "missing", "outdated", "optional"]
    detected: str | None = None
    required: str
    required_for: str
    blocking: bool = True
    repair_command: str | None = None
    repair_steps: list[str] = Field(default_factory=list)
    docs_url: str | None = None


class HostFacts(BaseModel):
    system: str
    release: str
    architecture: str
    python_version: str
    node_version: str | None = None
    profile: str
    profile_label: str
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    nvidia_driver: str | None = None
    cuda_toolkit: str | None = None
    docker_available: bool
    memory_gb: float | None = None
    disk_free_gb: float
    is_dgx_spark: bool = False
    validation_note: str
    runtime_dependencies: list[HostDependency] = Field(default_factory=list)
    quickstart_command: str = "./scripts/quickstart.sh"


class InstallStepPublic(BaseModel):
    id: str
    title: str
    detail: str
    progress: int


class CapabilityInstallPlan(BaseModel):
    capability_id: str
    title: str
    supported: bool
    compatibility: Literal["supported", "experimental", "manual", "blocked"]
    state: CapabilityState
    summary: str
    reason: str | None = None
    download_gb: float | None = None
    disk_required_gb: float | None = None
    requires_license_acceptance: bool = False
    license_urls: list[str]
    steps: list[InstallStepPublic]
    validation_checks: list[str]
    recovery: list[str]
    guide_path: str
    device_profile: str


class CapabilityInstallView(BaseModel):
    id: str
    name: str
    description: str
    state: CapabilityState
    state_label: str
    runtime: str
    model: str
    installed: bool
    can_install: bool
    compatibility: Literal["supported", "experimental", "manual", "blocked"]
    reason: str | None = None
    guide_path: str
    active_job_id: str | None = None


class CapabilityInstallRequest(BaseModel):
    confirm_install: bool = False
    accept_licenses: bool = False


class CapabilityInstallJob(BaseModel):
    id: str
    capability_id: str
    status: JobState
    progress: int
    stage: str
    message: str
    error: str | None = None
    logs: list[str]
    created_at: float
    updated_at: float
    attempt: int = 1


@dataclass(frozen=True)
class _CommandStep:
    id: str
    title: str
    detail: str
    progress: int
    status: JobState
    command: tuple[str, ...] | None = None


class CapabilitySetupStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_install_jobs (
                id TEXT PRIMARY KEY,
                capability_id TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                error TEXT,
                logs_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        now = time.time()
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        self._connection.execute(
            f"""
            UPDATE capability_install_jobs
               SET status = 'failed', stage = 'interrupted', progress = 0,
                   message = '安装进程因 Web Agent 重启而中断，可以重新执行。',
                   error = 'service_restarted', updated_at = ?
             WHERE status IN ({placeholders})
            """,
            (now, *sorted(ACTIVE_JOB_STATES)),
        )
        self._connection.commit()

    def create(self, capability_id: str, *, attempt: int = 1) -> CapabilityInstallJob:
        now = time.time()
        job = CapabilityInstallJob(
            id=str(uuid4()),
            capability_id=capability_id,
            status="queued",
            progress=0,
            stage="queued",
            message="安装任务已进入本机队列。",
            logs=[],
            created_at=now,
            updated_at=now,
            attempt=attempt,
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO capability_install_jobs
                    (id, capability_id, status, progress, stage, message, error,
                     logs_json, created_at, updated_at, attempt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.capability_id,
                    job.status,
                    job.progress,
                    job.stage,
                    job.message,
                    job.error,
                    "[]",
                    job.created_at,
                    job.updated_at,
                    job.attempt,
                ),
            )
            self._connection.commit()
        return job

    def update(
        self,
        job_id: str,
        *,
        status: JobState | None = None,
        progress: int | None = None,
        stage: str | None = None,
        message: str | None = None,
        error: str | None = None,
        append_log: str | None = None,
    ) -> CapabilityInstallJob:
        current = self.get(job_id)
        logs = list(current.logs)
        if append_log:
            logs.extend(line[:500] for line in append_log.splitlines() if line.strip())
            logs = logs[-200:]
        payload = {
            "status": status or current.status,
            "progress": current.progress if progress is None else max(0, min(100, progress)),
            "stage": stage or current.stage,
            "message": message or current.message,
            "error": error,
            "logs_json": json.dumps(logs, ensure_ascii=False),
            "updated_at": time.time(),
        }
        with self._lock:
            self._connection.execute(
                """
                UPDATE capability_install_jobs
                   SET status = :status, progress = :progress, stage = :stage,
                       message = :message, error = :error, logs_json = :logs_json,
                       updated_at = :updated_at
                 WHERE id = :job_id
                """,
                {**payload, "job_id": job_id},
            )
            self._connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> CapabilityInstallJob:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM capability_install_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise CapabilitySetupError("找不到这个安装任务。")
        return self._public(row)

    def list(self, *, limit: int = 30) -> list[CapabilityInstallJob]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM capability_install_jobs
                 ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def active_for(self, capability_id: str) -> CapabilityInstallJob | None:
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT * FROM capability_install_jobs
                 WHERE capability_id = ? AND status IN ({placeholders})
                 ORDER BY created_at DESC LIMIT 1
                """,
                (capability_id, *sorted(ACTIVE_JOB_STATES)),
            ).fetchone()
        return self._public(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> CapabilityInstallJob:
        try:
            logs = json.loads(str(row["logs_json"]))
        except (TypeError, ValueError):
            logs = []
        return CapabilityInstallJob(
            id=str(row["id"]),
            capability_id=str(row["capability_id"]),
            status=str(row["status"]),
            progress=int(row["progress"]),
            stage=str(row["stage"]),
            message=str(row["message"]),
            error=str(row["error"]) if row["error"] else None,
            logs=[str(item) for item in logs if isinstance(item, str)],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            attempt=int(row["attempt"]),
        )


def _capture(command: list[str], timeout: float = 4) -> str | None:
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip()


def _homebrew_binary_version(
    formula: str,
    binary: str,
    *arguments: str,
) -> str | None:
    """Discover keg-only Homebrew runtimes that are not present in PATH."""

    if platform.system() != "Darwin":
        return None
    brew = shutil.which("brew")
    if not brew:
        for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(candidate).is_file():
                brew = candidate
                break
    if not brew:
        return None
    prefix = _capture([brew, "--prefix", formula])
    if not prefix:
        return None
    executable = Path(prefix) / "bin" / binary
    if not executable.is_file():
        return None
    return _capture([str(executable), *arguments])


def _parsed_version(value: str | None) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", value or "")
    if not match:
        return None
    return tuple(int(item or 0) for item in match.groups())


def _runtime_dependencies(
    *,
    system: str,
    python_version: str,
    node_version: str | None,
) -> list[HostDependency]:
    python_ok = (_parsed_version(python_version) or (0, 0, 0)) >= (3, 11, 0)
    node_ok = (_parsed_version(node_version) or (0, 0, 0)) >= (22, 13, 0)
    git_version = _capture(["git", "--version"])
    git_ok = git_version is not None

    if system == "Darwin":
        python_command = "brew install python@3.12"
        node_command = "brew install node@22"
        git_command = "brew install git"
        package_hint = "若未安装 Homebrew，请先打开 https://brew.sh/ 按官方说明安装。"
    elif system == "Windows":
        python_command = "winget install --id Python.Python.3.12 -e"
        node_command = "winget install --id OpenJS.NodeJS.LTS -e"
        git_command = "winget install --id Git.Git -e"
        package_hint = "安装后请重新打开 PowerShell，再运行一键启动脚本。"
    else:
        python_command = "请按发行版文档安装 Python 3.11/3.12 与 python3-venv"
        node_command = "请从 https://nodejs.org/ 安装 Node.js 22 LTS"
        git_command = "请使用系统包管理器安装 Git"
        package_hint = "Linux 发行版差异较大，先按链接完成运行时安装，再重新检测。"

    return [
        HostDependency(
            id="python",
            name="Python",
            status="ready" if python_ok else "outdated",
            detected=python_version,
            required="3.11–3.13（推荐 3.12）",
            required_for="Agent API、模型安装和本地任务",
            repair_command=None if python_ok else python_command,
            repair_steps=[] if python_ok else [package_hint, "安装完成后重新运行环境检测。"],
            docs_url="https://www.python.org/downloads/",
        ),
        HostDependency(
            id="node",
            name="Node.js + pnpm",
            status="ready" if node_ok else ("outdated" if node_version else "missing"),
            detected=node_version,
            required="Node.js 22.13+（项目通过 Corepack 调用 pnpm）",
            required_for="Web 控制台构建与运行",
            repair_command=None if node_ok else node_command,
            repair_steps=(
                []
                if node_ok
                else [package_hint, "安装后运行 corepack pnpm --version，再重新检测。"]
            ),
            docs_url="https://nodejs.org/en/download",
        ),
        HostDependency(
            id="git",
            name="Git",
            status="ready" if git_ok else "missing",
            detected=git_version,
            required="2.39+",
            required_for="从 GitHub 下载、更新和协作开发",
            repair_command=None if git_ok else git_command,
            repair_steps=[] if git_ok else [package_hint, "安装完成后重新打开终端。"],
            docs_url="https://git-scm.com/downloads",
        ),
    ]


def detect_host() -> HostFacts:
    system = platform.system()
    architecture = platform.machine().lower()
    gpu_name: str | None = None
    gpu_memory_mb: int | None = None
    driver: str | None = None
    nvidia = _capture(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if nvidia:
        first = nvidia.splitlines()[0]
        parts = [part.strip() for part in first.split(",")]
        if parts:
            gpu_name = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            gpu_memory_mb = int(parts[1])
        if len(parts) > 2:
            driver = parts[2]
    elif system == "Darwin" and architecture in {"arm64", "aarch64"}:
        gpu_name = "Apple Silicon 统一内存 GPU"

    cuda_output = _capture(["nvcc", "--version"])
    cuda_match = re.search(r"release\s+([0-9.]+)", cuda_output or "")
    cuda_toolkit = cuda_match.group(1) if cuda_match else None
    docker_available = shutil.which("docker") is not None
    python_version = platform.python_version()
    node_version = _capture(["node", "--version"]) or _homebrew_binary_version(
        "node@22", "node", "--version"
    )

    memory_gb: float | None = None
    try:
        if system == "Darwin":
            value = _capture(["sysctl", "-n", "hw.memsize"])
            memory_gb = round(int(value or 0) / (1024**3), 1) if value else None
        elif hasattr(os, "sysconf"):
            memory_gb = round(
                (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
                / (1024**3),
                1,
            )
    except (OSError, TypeError, ValueError):
        memory_gb = None

    is_dgx_spark = (
        system == "Linux"
        and architecture in {"aarch64", "arm64"}
        and bool(gpu_name)
        and any(token in gpu_name.lower() for token in ("gb10", "dgx spark"))
    )
    if is_dgx_spark:
        profile = "linux-aarch64-dgx-spark"
        profile_label = "NVIDIA DGX Spark（ARM64）"
        validation_note = "已识别 Spark 硬件；模型扩展仍需在本机逐项编译和实机验收。"
    elif system == "Darwin" and architecture in {"arm64", "aarch64"}:
        profile = "macos-apple-silicon"
        profile_label = "macOS Apple Silicon"
        validation_note = "支持 MPS 路径；真实图片风格化仍需完成设备级质量门禁。"
    elif system == "Windows" and gpu_name:
        profile = "windows-nvidia"
        profile_label = "Windows NVIDIA"
        validation_note = "支持 CUDA/WSL2 路径；需要 Docker Desktop 与目标显卡实测。"
    elif system == "Linux" and architecture in {"x86_64", "amd64"} and gpu_name:
        profile = "linux-x86_64-nvidia"
        profile_label = "Linux x86_64 NVIDIA"
        validation_note = "适合容器化 GPU Provider；每项能力仍需独立质量验收。"
    else:
        profile = f"{system.lower()}-{architecture or 'unknown'}"
        profile_label = f"{system or '未知系统'} {architecture or '未知架构'}"
        validation_note = "控制面可运行；当前没有经过验证的重型模型安装档位。"

    return HostFacts(
        system=system,
        release=platform.release(),
        architecture=architecture,
        python_version=python_version,
        node_version=node_version,
        profile=profile,
        profile_label=profile_label,
        gpu_name=gpu_name,
        gpu_memory_mb=gpu_memory_mb,
        nvidia_driver=driver,
        cuda_toolkit=cuda_toolkit,
        docker_available=docker_available,
        memory_gb=memory_gb,
        disk_free_gb=round(shutil.disk_usage(PROJECT_ROOT).free / (1024**3), 1),
        is_dgx_spark=is_dgx_spark,
        validation_note=validation_note,
        runtime_dependencies=_runtime_dependencies(
            system=system,
            python_version=python_version,
            node_version=node_version,
        ),
        quickstart_command=(
            ".\\scripts\\quickstart.ps1" if system == "Windows" else "./scripts/quickstart.sh"
        ),
    )


class CapabilitySetupService:
    def __init__(
        self,
        data_path: Path,
        *,
        host: HostFacts | None = None,
        style_probe: Callable[[], dict[str, Any]] | None = None,
        flux_probe: Callable[[], dict[str, Any]] | None = None,
        command_runner: Callable[[tuple[str, ...], Callable[[str], None]], None]
        | None = None,
    ) -> None:
        self.host = host or detect_host()
        self.store = CapabilitySetupStore(data_path / "capability_setup.sqlite3")
        self.style_probe = style_probe or (lambda: {})
        self.flux_probe = flux_probe or (lambda: {})
        self._command_runner = command_runner or self._run_subprocess
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.RLock()
        self._thread_local = threading.local()

    def list_capabilities(self) -> list[CapabilityInstallView]:
        return [self._view(capability_id) for capability_id in self._capability_ids()]

    def plan(self, capability_id: str) -> CapabilityInstallPlan:
        if capability_id not in self._capability_ids():
            raise CapabilitySetupError("未知能力，安装器已拒绝执行。")
        return self._plan(capability_id)

    def start_install(
        self,
        capability_id: str,
        request: CapabilityInstallRequest,
    ) -> CapabilityInstallJob:
        plan = self.plan(capability_id)
        installed, _ = self._installed_state(capability_id)
        partially_installed, _ = self._partial_installed_state(capability_id)
        if installed or partially_installed:
            raise CapabilitySetupError("这项能力已经就绪，无需重复安装。")
        if not request.confirm_install:
            raise CapabilitySetupError("请先查看安装计划并确认磁盘、网络和运行影响。")
        if not plan.supported or plan.compatibility in {"manual", "blocked"}:
            raise CapabilitySetupError(plan.reason or "此设备暂不支持自动安装。")
        if not plan.steps:
            raise CapabilitySetupError("当前档位没有经过审计的自动安装步骤。")
        if plan.requires_license_acceptance and not request.accept_licenses:
            raise CapabilitySetupError("必须由本机所有者阅读并接受第三方模型许可证。")
        if self.host.disk_free_gb < (plan.disk_required_gb or 0):
            raise CapabilitySetupError(
                f"可用磁盘仅 {self.host.disk_free_gb:.1f} GiB，"
                f"本次至少需要 {plan.disk_required_gb:.1f} GiB。"
            )
        active = self.store.active_for(capability_id)
        if active is not None:
            return active
        another_active = next(
            (
                job
                for job in self.store.list(limit=50)
                if job.status in ACTIVE_JOB_STATES
            ),
            None,
        )
        if another_active is not None:
            raise CapabilitySetupError(
                "另一项本机能力正在安装。为避免虚拟环境和磁盘缓存冲突，"
                "请等待它结束或先停止该任务。"
            )
        previous = next(
            (job for job in self.store.list(limit=50) if job.capability_id == capability_id),
            None,
        )
        job = self.store.create(
            capability_id,
            attempt=(previous.attempt + 1) if previous else 1,
        )
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._execute,
            args=(job.id, plan, cancel_event),
            name=f"capability-install-{capability_id}",
            daemon=True,
        )
        with self._lock:
            self._cancel_events[job.id] = cancel_event
            self._threads[job.id] = thread
        thread.start()
        return self.store.get(job.id)

    def get_job(self, job_id: str) -> CapabilityInstallJob:
        return self.store.get(job_id)

    def list_jobs(self) -> list[CapabilityInstallJob]:
        return self.store.list()

    def cancel(self, job_id: str) -> CapabilityInstallJob:
        job = self.store.get(job_id)
        if job.status in TERMINAL_JOB_STATES:
            return job
        with self._lock:
            event = self._cancel_events.get(job_id)
            process = self._processes.get(job_id)
        if event is not None:
            event.set()
        if process is not None and process.poll() is None:
            process.terminate()
        return self.store.update(
            job_id,
            status="cancelled",
            stage="cancelled",
            message="已请求停止安装；已下载文件会保留，便于下次续传。",
            error="cancelled_by_owner",
        )

    def close(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
            events = list(self._cancel_events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for thread in threads:
            thread.join(timeout=5)
        self.store.close()

    @staticmethod
    def _capability_ids() -> tuple[str, ...]:
        return (
            "agent-core",
            "spatial-photo",
            "semantic-memory",
            "photo-style-transfer",
            "flux-gs",
        )

    def _view(self, capability_id: str) -> CapabilityInstallView:
        plan = self._plan(capability_id)
        active = self.store.active_for(capability_id)
        installed, runtime_reason = self._installed_state(capability_id)
        partially_installed, partial_reason = self._partial_installed_state(
            capability_id
        )
        state: CapabilityState = plan.state
        if active is not None:
            state = active.status if active.status != "queued" else "checking"
        elif installed:
            state = "ready"
        elif partially_installed:
            state = "degraded"
        elif plan.compatibility == "blocked":
            state = "incompatible"
        elif (
            not installed
            and (latest := self._latest_job(capability_id)) is not None
            and latest.status == "ready"
        ):
            state = "degraded"
        elif (
            (latest := self._latest_job(capability_id)) is not None
            and latest.status == "failed"
        ):
            state = "failed"
        elif plan.requires_license_acceptance:
            state = "license_required"
        elif plan.supported:
            state = "ready_to_install"
        labels = {
            "not_installed": "未安装",
            "checking": "正在检查",
            "incompatible": "需要适配",
            "license_required": "待确认许可",
            "ready_to_install": "可安装",
            "downloading": "正在下载",
            "installing": "正在安装",
            "validating": "正在验证",
            "ready": "已就绪",
            "degraded": "部分可用",
            "failed": "安装失败",
            "update_available": "可更新",
        }
        metadata = {
            "agent-core": (
                "Web Agent 基础",
                "对话、编排、工具策略和本地数据服务。",
                "本机 Python / Node.js",
                "FastAPI + LangGraph + SQLite",
            ),
            "spatial-photo": (
                "空间照片模型",
                "下载深度估计与语义主体分割权重，并完成离线校验。",
                "MPS / CUDA / CPU",
                "Depth Anything V2 Small + BiRefNet",
            ),
            "semantic-memory": (
                "语义记忆模型",
                "为长期记忆提供本地多语种向量召回。",
                "CPU / MPS / CUDA",
                "Granite Embedding 97M Multilingual",
            ),
            "photo-style-transfer": (
                "图片风格化模型",
                "部署真实 SDXL + IP-Adapter，不使用低质量模拟 Provider。",
                "MPS 或 NVIDIA CUDA",
                "SDXL + IP-Adapter",
            ),
            "flux-gs": (
                "Flux-GS 3D 建模",
                "部署多视角训练、压缩与 WebGL 发布服务。",
                "Linux + NVIDIA CUDA",
                "Flux-GS + COLMAP + CUDA extensions",
            ),
        }
        name, description, runtime, model = metadata[capability_id]
        return CapabilityInstallView(
            id=capability_id,
            name=name,
            description=description,
            state=state,
            state_label=labels[state],
            runtime=runtime,
            model=model,
            installed=installed or partially_installed,
            can_install=(
                not installed
                and not partially_installed
                and plan.supported
                and plan.compatibility not in {"manual", "blocked"}
            ),
            compatibility=plan.compatibility,
            reason=partial_reason if partially_installed else plan.reason or runtime_reason,
            guide_path=plan.guide_path,
            active_job_id=active.id if active else None,
        )

    def _latest_job(self, capability_id: str) -> CapabilityInstallJob | None:
        return next(
            (job for job in self.store.list(limit=50) if job.capability_id == capability_id),
            None,
        )

    def _installed_state(self, capability_id: str) -> tuple[bool, str | None]:
        if capability_id == "agent-core":
            return True, None
        if capability_id == "spatial-photo":
            ready = (
                PROJECT_ROOT / "backend/models/spatial/spatial-model-lock.json"
            ).is_file()
            return ready, None if ready else "尚未找到经过哈希校验的空间模型锁文件。"
        if capability_id == "semantic-memory":
            ready = (
                PROJECT_ROOT
                / "backend/models/embeddings/granite-embedding-97m-multilingual-r2"
                / "embedding-manifest.json"
            ).is_file()
            return ready, None if ready else "本地语义向量模型尚未准备。"
        if capability_id == "photo-style-transfer":
            try:
                status = self.style_probe() or {}
            except Exception:
                status = {}
            details = status.get("details") or {}
            ready = bool(status.get("ready") and details.get("production_quality"))
            return ready, None if ready else "真实 SDXL Provider 尚未通过运行时质量门禁。"
        try:
            status = self.flux_probe() or {}
        except Exception:
            status = {}
        ready = bool(status.get("ready") and status.get("production_quality"))
        return ready, None if ready else "真实 Flux-GS 训练 Provider 尚未就绪。"

    def _partial_installed_state(
        self, capability_id: str
    ) -> tuple[bool, str | None]:
        if capability_id != "photo-style-transfer":
            return False, None
        try:
            status = self.style_probe() or {}
        except Exception:
            return False, None
        details = status.get("details") or {}
        smoke_passed = (
            details.get("local_quality_validation") == "engineering_smoke_passed"
        )
        ready = bool(
            status.get("ready")
            and details.get("models_ready")
            and details.get("dependencies_ready")
            and smoke_passed
            and not details.get("production_quality")
        )
        reason = (
            "真实模型已下载并通过当前设备的 MPS 工程冒烟；"
            "尚未完成真实图片质量验收，因此按部分可用展示。"
        )
        return ready, reason if ready else None

    def _plan(self, capability_id: str) -> CapabilityInstallPlan:
        profile = self.host.profile
        common_recovery = [
            "安装记录保存在 SQLite，刷新页面不会丢失进度。",
            "Web Agent 重启会把未完成任务标为中断，可由本机所有者重新执行。",
            "安装器只运行仓库内白名单动作，不接受模型生成的命令或任意 Shell。",
        ]
        if capability_id == "agent-core":
            return CapabilityInstallPlan(
                capability_id=capability_id,
                title="Web Agent 基础运行时",
                supported=True,
                compatibility="supported",
                state="ready",
                summary="当前页面已经由完整 Web Agent 节点提供，基础运行时已就绪。",
                license_urls=[],
                steps=[],
                validation_checks=["API /health", "Web HTTP 200", "SQLite 可写"],
                recovery=common_recovery,
                guide_path="docs/guides/full-node-migration.md",
                device_profile=profile,
            )
        if capability_id == "spatial-photo":
            spark_blocked = profile == "linux-aarch64-dgx-spark"
            return CapabilityInstallPlan(
                capability_id=capability_id,
                title="安装空间照片模型",
                supported=not spark_blocked,
                compatibility="blocked" if spark_blocked else "supported",
                state="incompatible" if spark_blocked else "license_required",
                summary="在当前节点下载固定权重、校验哈希并启用离线读取。",
                reason=(
                    "空间模型及 BiRefNet 依赖尚未在 DGX Spark ARM64/CUDA 13 上完成"
                    "固定 wheel、真实加载与推理验收。"
                    if spark_blocked
                    else None
                ),
                download_gb=0.7,
                disk_required_gb=2.0,
                requires_license_acceptance=True,
                license_urls=[
                    "https://github.com/DepthAnything/Depth-Anything-V2",
                    "https://github.com/ZhengPeng7/BiRefNet",
                ],
                steps=(
                    self._public_steps(self._recipe(capability_id))
                    if not spark_blocked
                    else []
                ),
                validation_checks=["权重 SHA-256", "离线加载", "深度与主体分割 smoke test"],
                recovery=common_recovery,
                guide_path="docs/spatial-scene-device-deployment.md",
                device_profile=profile,
            )
        if capability_id == "semantic-memory":
            spark_blocked = profile == "linux-aarch64-dgx-spark"
            return CapabilityInstallPlan(
                capability_id=capability_id,
                title="安装语义记忆模型",
                supported=not spark_blocked,
                compatibility="blocked" if spark_blocked else "supported",
                state="incompatible" if spark_blocked else "license_required",
                summary="在当前节点准备多语种 Embedding，并保留词法检索降级路径。",
                reason=(
                    "本地 Transformer 与 PyTorch 组合尚未在 DGX Spark ARM64 上完成"
                    "固定 wheel、向量一致性和内存验收。"
                    if spark_blocked
                    else None
                ),
                download_gb=0.4,
                disk_required_gb=1.0,
                requires_license_acceptance=True,
                license_urls=["https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2"],
                steps=(
                    self._public_steps(self._recipe(capability_id))
                    if not spark_blocked
                    else []
                ),
                validation_checks=["模型清单", "本地只读加载", "中英文向量 smoke test"],
                recovery=common_recovery,
                guide_path="docs/architecture/layered-memory.md",
                device_profile=profile,
            )
        if capability_id == "photo-style-transfer":
            if profile == "macos-apple-silicon":
                supported, compatibility = True, "experimental"
                reason = "MPS 工程路径可自动部署，但必须在目标 Mac 上完成真实质量和峰值内存验收。"
            elif profile == "windows-nvidia":
                supported, compatibility = True, "supported"
                reason = None if self.host.docker_available else "请先安装 Docker Desktop 并启用 WSL2。"
                supported = supported and self.host.docker_available
                if not supported:
                    compatibility = "blocked"
            elif profile == "linux-x86_64-nvidia":
                supported, compatibility = False, "manual"
                reason = "Linux Provider 需按固定容器清单部署；当前 UI 只提供审计过的手动步骤。"
            elif profile == "linux-aarch64-dgx-spark":
                supported, compatibility = False, "blocked"
                reason = (
                    "当前 SDXL 安装档位尚未通过 DGX Spark ARM64、CUDA 13 与统一内存实机门禁；"
                    "应先制作并验证 Spark 专用 NGC 容器，不能复用 x86/cu126 安装脚本。"
                )
            else:
                supported, compatibility = False, "blocked"
                reason = "当前设备没有经过验证的 MPS 或 NVIDIA CUDA 图片风格化路径。"
            return CapabilityInstallPlan(
                capability_id=capability_id,
                title="安装真实图片风格化",
                supported=supported,
                compatibility=compatibility,
                state="license_required" if supported else "incompatible",
                summary="部署真实 SDXL + IP-Adapter Provider，并在通过 smoke test 后才标记可用。",
                reason=reason,
                download_gb=9.84,
                disk_required_gb=18.0,
                requires_license_acceptance=True,
                license_urls=[
                    "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0",
                    "https://huggingface.co/h94/IP-Adapter",
                    "https://huggingface.co/latent-consistency/lcm-lora-sdxl",
                ],
                steps=self._public_steps(self._recipe(capability_id)) if supported else [],
                validation_checks=["固定模型清单", "本地只读加载", "真实推理", "质量门禁", "峰值内存"],
                recovery=common_recovery,
                guide_path="docs/guides/photo-style-deployment.md",
                device_profile=profile,
            )
        if profile == "linux-x86_64-nvidia":
            compatibility = "manual"
            reason = "可按固定上游版本手动部署；商业使用前还必须完成 CUDA 子模块许可审查。"
        elif profile == "linux-aarch64-dgx-spark":
            compatibility = "blocked"
            reason = (
                "上游环境固定 Python 3.11/cu126 和多个 CUDA 扩展；DGX Spark 是 ARM64/GB10，"
                "需先验证 ARM64 wheels、按 SM 12.1 重编译扩展并构建 tmc3。"
            )
        else:
            compatibility = "blocked"
            reason = "Flux-GS 真实训练要求 Linux NVIDIA CUDA；当前设备只可使用 Preview 契约。"
        return CapabilityInstallPlan(
            capability_id=capability_id,
            title="部署 Flux-GS 训练服务",
            supported=False,
            compatibility=compatibility,
            state="incompatible",
            summary="Flux-GS 是多视角训练后半段，不等同于单图 2D 转 3D。",
            reason=reason,
            download_gb=None,
            disk_required_gb=30.0,
            requires_license_acceptance=True,
            license_urls=["https://github.com/zhaozuheng0726/Flux-gs-skill"],
            steps=[],
            validation_checks=["COLMAP 数据", "CUDA 扩展", "真实训练", "WebGL 发布", "许可证"],
            recovery=common_recovery,
            guide_path="docs/guides/flux-gs-capability.md",
            device_profile=profile,
        )

    def _recipe(self, capability_id: str) -> list[_CommandStep]:
        python = str(Path(sys.executable))
        if capability_id == "spatial-photo":
            return [
                _CommandStep("preflight", "检查空间模型计划", "确认磁盘与固定清单。", 8, "checking"),
                _CommandStep(
                    "dependencies",
                    "安装主体分割依赖",
                    "在当前 Web Agent 虚拟环境安装固定范围的 BiRefNet 依赖。",
                    16,
                    "installing",
                    (
                        python,
                        "-m",
                        "pip",
                        "install",
                        "-r",
                        "backend/requirements-segmentation.txt",
                    ),
                ),
                _CommandStep(
                    "download",
                    "下载并校验空间模型",
                    "准备 Depth Anything V2 Small 与 BiRefNet。",
                    30,
                    "downloading",
                    (python, "backend/scripts/prepare_spatial_models.py", "--download", "--verify"),
                ),
                _CommandStep(
                    "validate",
                    "执行离线推理验证",
                    "实际加载深度与主体分割模型，并对固定合成图执行 smoke test。",
                    88,
                    "validating",
                    (python, "backend/scripts/smoke_spatial_models.py"),
                ),
            ]
        if capability_id == "semantic-memory":
            return [
                _CommandStep("preflight", "检查记忆模型计划", "确认本机存储空间。", 8, "checking"),
                _CommandStep(
                    "download",
                    "下载并校验语义模型",
                    "准备 Granite 多语种 Embedding。",
                    30,
                    "downloading",
                    (
                        python,
                        "backend/scripts/prepare_memory_embedding_model.py",
                        "--download",
                        "--smoke-test",
                    ),
                ),
                _CommandStep("validate", "启用并验证", "写入本机配置并保留词法降级。", 90, "validating"),
            ]
        if capability_id == "photo-style-transfer" and self.host.profile == "macos-apple-silicon":
            return [
                _CommandStep(
                    "plan", "核对固定模型与许可", "生成真实模型下载计划。", 5, "checking",
                    (python, "scripts/manage_photo_style.py", "plan-real"),
                ),
                _CommandStep(
                    "download", "准备 SDXL + IP-Adapter", "下载约 9.84 GiB 固定权重并执行哈希校验。",
                    18, "downloading",
                    (python, "scripts/manage_photo_style.py", "prepare-macos-mps", "--accept-model-licenses"),
                ),
                _CommandStep(
                    "validate", "验证真实 Provider", "检查本机 MPS smoke 记录、非 Fake 与本地只读状态。",
                    92, "validating",
                ),
            ]
        if capability_id == "photo-style-transfer" and self.host.profile == "windows-nvidia":
            return [
                _CommandStep(
                    "source", "安装隔离 Provider", "拉取固定源码并创建隔离环境。", 10, "installing",
                    (python, "scripts/manage_photo_style.py", "install-service"),
                ),
                _CommandStep(
                    "download", "下载 GPU 模型", "在 WSL2/Docker 环境准备真实权重。", 32, "downloading",
                    (python, "scripts/manage_photo_style.py", "prepare-windows-gpu", "--accept-model-licenses"),
                ),
                _CommandStep(
                    "configure", "配置真实 Provider", "只绑定本机回环地址并写入安全配置。", 82, "installing",
                    (python, "scripts/manage_photo_style.py", "configure-real-windows"),
                ),
                _CommandStep("validate", "验证真实 Provider", "执行健康、模型来源与质量门禁。", 94, "validating"),
            ]
        return []

    @staticmethod
    def _public_steps(steps: list[_CommandStep]) -> list[InstallStepPublic]:
        return [
            InstallStepPublic(id=step.id, title=step.title, detail=step.detail, progress=step.progress)
            for step in steps
        ]

    def _execute(
        self,
        job_id: str,
        plan: CapabilityInstallPlan,
        cancel_event: threading.Event,
    ) -> None:
        try:
            for step in self._recipe(plan.capability_id):
                if cancel_event.is_set():
                    return
                self.store.update(
                    job_id,
                    status=step.status,
                    progress=step.progress,
                    stage=step.id,
                    message=step.detail,
                )
                if step.command is not None:
                    self._thread_local.job_id = job_id
                    self._command_runner(
                        step.command,
                        lambda line: self.store.update(job_id, append_log=self._redact(line)),
                    )
            if cancel_event.is_set():
                return
            self._apply_post_install_config(plan.capability_id)
            installed, reason = self._installed_state(plan.capability_id)
            if not installed and plan.capability_id in {"spatial-photo", "semantic-memory"}:
                raise CapabilitySetupError(reason or "安装后验证未通过。")
            style_restart_required = (
                plan.capability_id == "photo-style-transfer" and not installed
            )
            restart_required = plan.capability_id in {
                "spatial-photo",
                "semantic-memory",
            } or style_restart_required
            if style_restart_required and not self._photo_style_artifacts_ready():
                raise CapabilitySetupError(reason or "图片风格化模型安装后验证未通过。")
            self.store.update(
                job_id,
                status="ready",
                progress=100,
                stage="ready",
                message=(
                    "模型文件和配置已验证。请重启 Web Agent 使新运行时生效；"
                    "生成类模型还需完成真实图片质量验收。"
                    if restart_required
                    else "安装与验证已完成，这项能力已可由 Agent 调用。"
                ),
            )
        except Exception as exc:
            current = self.store.get(job_id)
            if current.status != "cancelled":
                self.store.update(
                    job_id,
                    status="failed",
                    stage="failed",
                    message="安装未通过验证，可查看日志后重新执行。",
                    error=self._redact(str(exc))[:800],
                )
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._threads.pop(job_id, None)

    def _run_subprocess(
        self,
        command: tuple[str, ...],
        on_log: Callable[[str], None],
    ) -> None:
        job_id = getattr(self._thread_local, "job_id", "")
        process = subprocess.Popen(
            list(command),
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        with self._lock:
            if job_id:
                self._processes[job_id] = process
        assert process.stdout is not None
        for line in process.stdout:
            on_log(line.rstrip())
        return_code = process.wait()
        if return_code != 0:
            raise CapabilitySetupError(f"白名单安装动作执行失败（exit={return_code}）。")

    def _photo_style_artifacts_ready(self) -> bool:
        if self.host.profile == "macos-apple-silicon":
            return (
                PROJECT_ROOT / "backend/models/photo-style/model-lock.json"
            ).is_file()
        if self.host.profile == "windows-nvidia":
            return (
                PROJECT_ROOT
                / ".capabilities/pic-style/.web-agent-deployment.json"
            ).is_file()
        return False

    def _apply_post_install_config(self, capability_id: str) -> None:
        updates: dict[str, str] = {}
        if capability_id == "spatial-photo":
            updates = {
                "SPATIAL_DEPTH_MODEL": "backend/models/spatial/depth-anything-v2-small",
                "SPATIAL_BIREFNET_MODEL": "backend/models/spatial/birefnet",
                "SPATIAL_BIREFNET_LOCAL_FILES_ONLY": "true",
            }
        elif capability_id == "semantic-memory":
            updates = {
                "AGENT_MEMORY_SEMANTIC_ENABLED": "true",
                "AGENT_MEMORY_EMBEDDING_PROVIDER": "transformer",
                "AGENT_MEMORY_EMBEDDING_MODEL_PATH": (
                    "backend/models/embeddings/granite-embedding-97m-multilingual-r2"
                ),
            }
        if updates:
            self._update_env(updates)

    @staticmethod
    def _update_env(updates: dict[str, str]) -> None:
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
        temporary = path.with_suffix(".setup.tmp")
        temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @staticmethod
    def _redact(value: str) -> str:
        redacted = re.sub(
            r"(?i)(api[_-]?key|token|secret|authorization)(\s*[:=]\s*)\S+",
            r"\1\2[REDACTED]",
            value,
        )
        redacted = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", redacted)
        redacted = re.sub(r"\b(?:hf_|sk-)[A-Za-z0-9._-]{8,}\b", "[REDACTED]", redacted)
        return redacted.replace(str(Path.home()), "[USER_HOME]")


def _require_local_owner(request: Request) -> None:
    client_host = request.client.host if request.client is not None else ""
    if client_host not in LOCAL_CLIENTS:
        raise HTTPException(
            status_code=403,
            detail="模型安装与主机信息仅允许从当前设备的所有者设置中心访问。",
        )


def create_capability_setup_router(service: CapabilitySetupService) -> APIRouter:
    router = APIRouter(prefix="/api/setup", tags=["local setup"])

    @router.get("/host", response_model=HostFacts)
    def host(request: Request) -> HostFacts:
        _require_local_owner(request)
        return service.host

    @router.get("/capabilities", response_model=list[CapabilityInstallView])
    def capabilities(request: Request) -> list[CapabilityInstallView]:
        _require_local_owner(request)
        return service.list_capabilities()

    @router.post(
        "/capabilities/{capability_id}/plan",
        response_model=CapabilityInstallPlan,
    )
    def plan(capability_id: str, request: Request) -> CapabilityInstallPlan:
        _require_local_owner(request)
        try:
            return service.plan(capability_id)
        except CapabilitySetupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post(
        "/capabilities/{capability_id}/install",
        response_model=CapabilityInstallJob,
        status_code=202,
    )
    def install(
        capability_id: str,
        payload: CapabilityInstallRequest,
        request: Request,
    ) -> CapabilityInstallJob:
        _require_local_owner(request)
        try:
            return service.start_install(capability_id, payload)
        except CapabilitySetupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/jobs", response_model=list[CapabilityInstallJob])
    def jobs(request: Request) -> list[CapabilityInstallJob]:
        _require_local_owner(request)
        return service.list_jobs()

    @router.get("/jobs/{job_id}", response_model=CapabilityInstallJob)
    def job(job_id: str, request: Request) -> CapabilityInstallJob:
        _require_local_owner(request)
        try:
            return service.get_job(job_id)
        except CapabilitySetupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/jobs/{job_id}/cancel", response_model=CapabilityInstallJob)
    def cancel(job_id: str, request: Request) -> CapabilityInstallJob:
        _require_local_owner(request)
        try:
            return service.cancel(job_id)
        except CapabilitySetupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router
