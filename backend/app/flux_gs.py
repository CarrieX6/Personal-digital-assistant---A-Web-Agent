from __future__ import annotations

# Capability design and original PDA adapter contributed by Zuheng Zhao:
# https://github.com/zhaozuheng0726/Flux-gs-skill

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .models import CapabilityInfo, CapabilityRequirements
from .tools import ToolError, ToolRegistry, ToolSpec, current_tool_context


TRAINING_PROFILES = {"quick", "default", "tandt", "mipnerf360"}
SCENE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
DATASET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class FluxGSError(ValueError):
    """Raised when Flux-GS input or provider execution is invalid."""


@dataclass(frozen=True)
class FluxGSParameters:
    dataset_path: str
    scene_name: str | None = None
    title: str | None = None
    training_profile: str = "default"
    iterations: int = 30000
    web_base_url: str | None = None
    auto_validate: bool = True

    def validated(self) -> "FluxGSParameters":
        dataset_path = self.dataset_path.strip()
        if not dataset_path:
            raise FluxGSError("Flux-GS 需要非空 dataset_path。")
        if len(dataset_path) > 500:
            raise FluxGSError("dataset_path 不能超过 500 个字符。")
        scene_name = self.scene_name.strip() if self.scene_name else None
        if scene_name and not SCENE_NAME_PATTERN.fullmatch(scene_name):
            raise FluxGSError("scene_name 只能包含字母、数字、点、下划线和短横线，最长 80 字符。")
        if self.training_profile not in TRAINING_PROFILES:
            raise FluxGSError("training_profile 必须是 quick/default/tandt/mipnerf360。")
        if not 1000 <= int(self.iterations) <= 30000:
            raise FluxGSError("iterations 必须在 1000 到 30000 之间。")
        title = self.title.strip() if self.title else None
        if title and len(title) > 80:
            raise FluxGSError("title 不能超过 80 个字符。")
        web_base_url = self.web_base_url.strip() if self.web_base_url else None
        return FluxGSParameters(
            dataset_path=dataset_path,
            scene_name=scene_name,
            title=title,
            training_profile=self.training_profile,
            iterations=int(self.iterations),
            web_base_url=web_base_url,
            auto_validate=bool(self.auto_validate),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "dataset_path": self.dataset_path,
            "scene_name": self.scene_name,
            "title": self.title,
            "training_profile": self.training_profile,
            "iterations": self.iterations,
            "web_base_url": self.web_base_url,
            "auto_validate": self.auto_validate,
        }


class FluxGSProvider(Protocol):
    name: str
    model_name: str

    def validate_dataset(self, dataset_path: str, *, owner_id: str) -> dict[str, Any]: ...

    def create_job(
        self,
        parameters: FluxGSParameters,
        *,
        owner_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]: ...

    def get_job(self, job_id: str, *, owner_id: str) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class FluxGSHttpProvider:
    """HTTP adapter for an isolated GPU Flux-GS training/publishing service."""

    name = "flux-gs-http"
    model_name = "Flux-GS CUDA training service"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        tenant_id: str = "personal-agent",
        timeout_seconds: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise FluxGSError("FLUX_GS_SERVICE_URL 尚未配置。")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def _headers(self, *, owner_id: str | None = None) -> dict[str, str]:
        headers = {"X-Tenant-ID": self.tenant_id}
        if owner_id:
            headers["X-Owner-ID"] = owner_id
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def validate_dataset(self, dataset_path: str, *, owner_id: str) -> dict[str, Any]:
        try:
            response = self.client.post(
                f"{self.base_url}/v1/skills/flux-gs/datasets/validate",
                json={"dataset_path": dataset_path},
                headers=self._headers(owner_id=owner_id),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FluxGSError("无法调用 Flux-GS 数据集校验服务。") from exc

    def create_job(
        self,
        parameters: FluxGSParameters,
        *,
        owner_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        try:
            response = self.client.post(
                f"{self.base_url}/v1/skills/flux-gs/jobs",
                json=parameters.public_dict(),
                headers={
                    **self._headers(owner_id=owner_id),
                    "Idempotency-Key": idempotency_key or str(uuid4()),
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FluxGSError("无法创建 Flux-GS 训练任务。") from exc

    def get_job(self, job_id: str, *, owner_id: str) -> dict[str, Any]:
        try:
            response = self.client.get(
                f"{self.base_url}/v1/skills/flux-gs/jobs/{job_id}",
                headers=self._headers(owner_id=owner_id),
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FluxGSError("无法读取 Flux-GS 任务状态。") from exc

    def status(self) -> dict[str, Any]:
        try:
            ready_response = self.client.get(
                f"{self.base_url}/health/ready",
                headers=self._headers(),
                timeout=3,
            )
            ready_payload = _safe_json(ready_response)
            ready = ready_response.status_code == 200 and not (
                isinstance(ready_payload, dict)
                and (
                    ready_payload.get("ready") is False
                    or ready_payload.get("status") == "not_ready"
                )
            )
            provider_status: Any = None
            if ready:
                try:
                    provider_response = self.client.get(
                        f"{self.base_url}/health/provider",
                        headers=self._headers(),
                        timeout=3,
                    )
                    if provider_response.status_code == 200:
                        provider_status = provider_response.json()
                except (httpx.HTTPError, ValueError):
                    provider_status = None
            return {
                "ready": ready,
                "remote": True,
                "configured": True,
                "provider": self.name,
                "model_name": self.model_name,
                "upstream_status": ready_payload,
                "upstream_provider_status": provider_status,
                "production_quality": bool(ready),
                "error": None if ready else "service_not_ready",
            }
        except httpx.HTTPError:
            return {
                "ready": False,
                "remote": True,
                "configured": True,
                "provider": self.name,
                "model_name": self.model_name,
                "upstream_status": None,
                "upstream_provider_status": None,
                "production_quality": False,
                "error": "service_unreachable",
            }

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class LocalFluxGSPreviewProvider:
    """Deterministic provider for local PDA tests; it does not train a model."""

    name = "flux-gs-local-preview"
    model_name = "Flux-GS dataset validator preview"

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def validate_dataset(self, dataset_path: str, *, owner_id: str) -> dict[str, Any]:
        return validate_colmap_dataset(dataset_path)

    def create_job(
        self,
        parameters: FluxGSParameters,
        *,
        owner_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        validation = validate_colmap_dataset(parameters.dataset_path)
        if not validation["valid"]:
            raise FluxGSError("数据集未通过 Flux-GS 校验，不能开始训练。")
        job_id = idempotency_key or str(uuid4())
        scene_name = parameters.scene_name or validation["scene_name"]
        payload = {
            "job_id": job_id,
            "kind": "flux_gs_demo",
            "status": "completed",
            "progress": 100,
            "stage": "preview_complete",
            "message": "预览 Provider 只完成数据校验，不执行真实 GPU 训练。",
            "asset_id": f"flux-gs-{job_id}",
            "scene_name": scene_name,
            "demo_url": (
                f"{parameters.web_base_url.rstrip('/')}/render_{scene_name}/"
                if parameters.web_base_url
                else None
            ),
            "provider": self.name,
            "production_quality": False,
        }
        self.jobs[job_id] = payload
        return payload

    def get_job(self, job_id: str, *, owner_id: str) -> dict[str, Any]:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise FluxGSError("找不到 Flux-GS 任务。") from exc

    def status(self) -> dict[str, Any]:
        return {
            "ready": True,
            "remote": False,
            "configured": True,
            "provider": self.name,
            "model_name": self.model_name,
            "production_quality": False,
            "error": None,
        }

    def close(self) -> None:
        return None


def build_flux_gs_provider_from_env() -> FluxGSProvider:
    selected = os.getenv("FLUX_GS_PROVIDER", "http").strip().lower()
    if selected in {"preview", "local-preview", "fake"}:
        return LocalFluxGSPreviewProvider()
    if selected in {"http", "flux-gs-http"}:
        return FluxGSHttpProvider(
            os.getenv("FLUX_GS_SERVICE_URL", "http://127.0.0.1:18100"),
            api_key=os.getenv("FLUX_GS_API_KEY") or None,
            tenant_id=os.getenv("FLUX_GS_TENANT_ID", "personal-agent"),
            timeout_seconds=float(os.getenv("FLUX_GS_TIMEOUT_SECONDS", "30")),
        )
    raise FluxGSError(f"未知 Flux-GS Provider：{selected}")


class FluxGSService:
    """Resolve opaque dataset IDs inside one configured root before RPC.

    The LLM and chat channels never receive an arbitrary filesystem path. The
    GPU service may use a different mounted root, but must expose the same
    relative dataset IDs.
    """

    def __init__(
        self,
        provider: FluxGSProvider | None = None,
        *,
        dataset_root: Path | None = None,
        provider_dataset_root: Path | None = None,
    ) -> None:
        self.provider = provider or build_flux_gs_provider_from_env()
        self.dataset_root = (
            dataset_root
            or Path(
                os.getenv(
                    "FLUX_GS_DATASET_ROOT",
                    Path(__file__).resolve().parents[1]
                    / "data"
                    / "flux-gs-datasets",
                )
            )
        ).expanduser().resolve()
        self.provider_dataset_root = (
            provider_dataset_root
            or Path(
                os.getenv(
                    "FLUX_GS_PROVIDER_DATASET_ROOT",
                    str(self.dataset_root),
                )
            )
        ).expanduser()
        self.dataset_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _dataset_id(dataset_id: str) -> str:
        selected = dataset_id.strip()
        if not DATASET_ID_PATTERN.fullmatch(selected):
            raise FluxGSError(
                "dataset_id 只能包含字母、数字、点、下划线和短横线，最长 100 字符。"
            )
        return selected

    def local_dataset_path(self, dataset_id: str) -> Path:
        selected = self._dataset_id(dataset_id)
        path = (self.dataset_root / selected).resolve()
        if not path.is_relative_to(self.dataset_root):
            raise FluxGSError("dataset_id 超出允许的数据集目录。")
        return path

    def provider_dataset_path(self, dataset_id: str) -> str:
        selected = self._dataset_id(dataset_id)
        return str(self.provider_dataset_root / selected)

    def validate_dataset(self, dataset_id: str, *, owner_id: str = "local") -> dict[str, Any]:
        selected = self._dataset_id(dataset_id)
        result = self.provider.validate_dataset(
            self.provider_dataset_path(selected),
            owner_id=owner_id,
        )
        return {
            key: value
            for key, value in {**result, "dataset_id": selected}.items()
            if key != "dataset_path"
        }

    def create_demo(
        self,
        parameters: FluxGSParameters,
        *,
        owner_id: str = "local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        dataset_id = self._dataset_id(parameters.dataset_path)
        selected = FluxGSParameters(
            dataset_path=self.provider_dataset_path(dataset_id),
            scene_name=parameters.scene_name,
            title=parameters.title,
            training_profile=parameters.training_profile,
            iterations=parameters.iterations,
            web_base_url=parameters.web_base_url,
            auto_validate=parameters.auto_validate,
        ).validated()
        if selected.auto_validate:
            validation = self.validate_dataset(dataset_id, owner_id=owner_id)
            if not bool(validation.get("valid")):
                errors = validation.get("errors") or ["数据集结构不符合 Flux-GS 训练要求。"]
                raise FluxGSError(str(errors[0]))
        return self.provider.create_job(
            selected,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )

    def get_job_status(self, job_id: str, *, owner_id: str = "local") -> dict[str, Any]:
        if not job_id.strip():
            raise FluxGSError("job_id 不能为空。")
        return self.provider.get_job(job_id.strip(), owner_id=owner_id)

    def provider_status(self) -> dict[str, Any]:
        return self.provider.status()

    def close(self) -> None:
        self.provider.close()


def register_flux_gs_tools(registry: ToolRegistry, service: FluxGSService) -> None:
    def validate_flux_gs_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
        dataset_id = _require_string(arguments, "dataset_id")
        try:
            return service.validate_dataset(
                dataset_id,
                owner_id=current_tool_context().owner_id,
            )
        except FluxGSError as exc:
            raise ToolError(str(exc)) from exc

    def create_flux_gs_demo(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parameters = FluxGSParameters(
                dataset_path=_require_string(arguments, "dataset_id"),
                scene_name=_optional_string(arguments, "scene_name"),
                title=_optional_string(arguments, "title"),
                training_profile=str(arguments.get("training_profile", "default")),
                iterations=int(arguments.get("iterations", 30000)),
                auto_validate=bool(arguments.get("auto_validate", True)),
            ).validated()
            execution_context = current_tool_context()
            created = service.create_demo(
                parameters,
                owner_id=execution_context.owner_id,
                idempotency_key=execution_context.idempotency_key,
            )
        except (FluxGSError, TypeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "job_id": created.get("job_id"),
            "asset_id": created.get("asset_id"),
            "status": created.get("status", "queued"),
            "progress": int(created.get("progress", 0)),
            "kind": "flux_gs_demo",
            "scene_name": created.get("scene_name") or parameters.scene_name,
            "demo_url": created.get("demo_url"),
            "message": created.get("message") or "Flux-GS 训练任务已创建。",
        }

    def get_flux_gs_job_status(arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = _require_string(arguments, "job_id")
        try:
            return service.get_job_status(
                job_id,
                owner_id=current_tool_context().owner_id,
            )
        except FluxGSError as exc:
            raise ToolError(str(exc)) from exc

    validation_schema = {
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
                "description": "管理员放入受控数据集目录的 COLMAP 数据集 ID；不能是文件路径",
            },
        },
        "required": ["dataset_id"],
        "additionalProperties": False,
    }
    create_schema = {
        "type": "object",
        "properties": {
            "dataset_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$",
                "description": "管理员放入受控数据集目录的 COLMAP 数据集 ID；不能是文件路径",
            },
            "scene_name": {"type": "string", "maxLength": 80},
            "title": {"type": "string", "maxLength": 80},
            "training_profile": {
                "type": "string",
                "enum": ["quick", "default", "tandt", "mipnerf360"],
                "default": "default",
            },
            "iterations": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 30000,
                "default": 30000,
            },
            "auto_validate": {"type": "boolean", "default": True},
        },
        "required": ["dataset_id"],
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            "validate_flux_gs_dataset",
            "按受控 dataset_id 校验 COLMAP 数据集是否满足 Flux-GS 训练要求；训练前必须先调用。",
            validation_schema,
            validate_flux_gs_dataset,
            risk_level="read",
            idempotent=True,
        )
    )
    registry.register(
        ToolSpec(
            "create_flux_gs_demo",
            "在数据集合法后创建 Flux-GS GPU 训练和 Web 发布任务，完成后返回浏览器渲染 URL。",
            create_schema,
            create_flux_gs_demo,
            risk_level="external_write",
            requires_approval=True,
            idempotent=True,
            capability=CapabilityInfo(
                id="flux-gs-demo",
                name="Flux-GS Web Demo",
                version="0.3.0",
                author="Zuheng Zhao",
                description=(
                    "校验用户 COLMAP 数据集，提交独立 GPU 服务训练 Flux-GS，"
                    "发布 comp.json 到 WebGL Viewer，并返回可访问 URL。"
                ),
                entrypoint="create_flux_gs_demo",
                input_schema=create_schema,
                requirements=CapabilityRequirements(
                    local_model="独立 Flux-GS HTTP 服务；NVIDIA GPU，CUDA 12.x，建议显存 12GB+。",
                    storage="受控根目录只保存输入数据集；训练输出和 Web demo 由独立 Flux-GS 服务管理。",
                    permissions=[
                        "读取用户上传后落盘的数据集目录",
                        "向 Flux-GS GPU 服务创建训练任务",
                        "写入训练输出和静态 Web demo 目录",
                        "返回浏览器可访问的渲染 URL",
                    ],
                    downloads="Python/CUDA 依赖、Flux-GS CUDA 扩展、GPCC/TMC13 编码工具；不提交模型权重或用户数据。",
                ),
            ),
        )
    )
    registry.register(
        ToolSpec(
            "get_flux_gs_job_status",
            "查询 Flux-GS 训练/发布任务状态和最终 demo_url。",
            {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            get_flux_gs_job_status,
            risk_level="read",
            idempotent=True,
        )
    )


class FluxGSDatasetValidationRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=100)


class FluxGSDemoCreateRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=100)
    scene_name: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=80)
    training_profile: str = "default"
    iterations: int = Field(default=30000, ge=1000, le=30000)
    web_base_url: str | None = Field(default=None, max_length=500)
    auto_validate: bool = True


def create_flux_gs_router(service_attr: str = "flux_gs_service") -> APIRouter:
    router = APIRouter(prefix="/api/flux-gs", tags=["flux-gs"])

    def service_from(request: Request) -> FluxGSService:
        return getattr(request.app.state, service_attr)

    def require_local_root(request: Request) -> None:
        client_host = request.client.host if request.client is not None else ""
        if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(
                status_code=403,
                detail="Flux-GS 管理接口仅允许从本机 Root 控制台访问。",
            )

    @router.post("/datasets/validate")
    def api_validate_dataset(
        payload: FluxGSDatasetValidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        require_local_root(request)
        try:
            return service_from(request).validate_dataset(payload.dataset_id)
        except FluxGSError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/demos")
    def api_create_demo(
        payload: FluxGSDemoCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        require_local_root(request)
        try:
            return service_from(request).create_demo(
                FluxGSParameters(
                    dataset_path=payload.dataset_id,
                    scene_name=payload.scene_name,
                    title=payload.title,
                    training_profile=payload.training_profile,
                    iterations=payload.iterations,
                    web_base_url=payload.web_base_url,
                    auto_validate=payload.auto_validate,
                ),
                owner_id="local",
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        except FluxGSError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}")
    def api_get_job(job_id: str, request: Request) -> dict[str, Any]:
        require_local_root(request)
        try:
            return service_from(request).get_job_status(job_id)
        except FluxGSError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/provider")
    def api_provider_status(request: Request) -> dict[str, Any]:
        require_local_root(request)
        return service_from(request).provider_status()

    return router


def validate_colmap_dataset(dataset_path: str) -> dict[str, Any]:
    root = Path(dataset_path).expanduser()
    errors: list[str] = []
    warnings: list[str] = []
    if not root.exists():
        errors.append("数据集目录不存在。")
    elif not root.is_dir():
        errors.append("dataset_path 必须指向目录。")
    images_dir = root / "images"
    sparse_dir = root / "sparse" / "0"
    if not images_dir.is_dir():
        errors.append("缺少 images/ 图像目录。")
    if not sparse_dir.is_dir():
        errors.append("缺少 sparse/0/ COLMAP 重建目录。")
    if sparse_dir.exists():
        required_groups = [
            ("cameras.bin", "cameras.txt"),
            ("images.bin", "images.txt"),
            ("points3D.bin", "points3D.txt"),
        ]
        for binary_name, text_name in required_groups:
            if not (sparse_dir / binary_name).exists() and not (sparse_dir / text_name).exists():
                errors.append(f"缺少 {binary_name} 或 {text_name}。")
    image_count = 0
    if images_dir.is_dir():
        image_count = sum(
            1
            for path in images_dir.iterdir()
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        if image_count == 0:
            errors.append("images/ 中没有 jpg、jpeg、png 或 webp 图像。")
        elif image_count < 8:
            warnings.append("图像数量少于 8 张，训练质量可能不稳定。")
    if (sparse_dir / "images.txt").exists():
        referenced = _referenced_image_names(sparse_dir / "images.txt")
        missing = [name for name in referenced if not (images_dir / name).exists()]
        if missing:
            errors.append(f"images.txt 引用了不存在的图像：{missing[0]}")
    return {
        "valid": not errors,
        "dataset_path": str(root.resolve()) if root.exists() else str(root),
        "scene_name": _safe_scene_name(root.name or "scene"),
        "image_count": image_count,
        "errors": errors,
        "warnings": warnings,
        "next_actions": _next_actions(errors),
    }


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _safe_scene_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())[:80].strip("._-")
    return cleaned or "scene"


def _referenced_image_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 10 and any(
            parts[-1].lower().endswith(ext)
            for ext in (".jpg", ".jpeg", ".png", ".webp")
        ):
            names.append(parts[-1])
    return names


def _next_actions(errors: list[str]) -> list[str]:
    actions: list[str] = []
    for error in errors:
        if "images/" in error:
            actions.append("把训练图片放到 dataset/images/ 目录。")
        elif "sparse/0" in error:
            actions.append("先用 COLMAP 完成 sparse reconstruction，并保留 sparse/0/。")
        elif "cameras" in error or "points3D" in error:
            actions.append("确认 sparse/0/ 下存在 cameras、images、points3D 的 bin 或 txt 文件。")
        elif "不存在的图像" in error:
            actions.append("修正 sparse/0/images.txt 中的图像文件名，或补齐缺失图片。")
    return actions or ["修复 errors 后重新调用 validate_flux_gs_dataset。"]


def _require_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} 不能为空。")
    return value.strip()


def _optional_string(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} 必须是字符串。")
    selected = value.strip()
    return selected or None
