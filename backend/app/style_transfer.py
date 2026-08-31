from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .assets import AssetError, MAX_UPLOAD_BYTES, SpatialSceneService
from .models import (
    CapabilityInfo,
    CapabilityRequirements,
    JobPublic,
    PhotoStyleCreateResponse,
)
from .tools import ToolError, ToolRegistry, ToolSpec, current_tool_context


LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str, str], None]
STYLE_MODES = {"preserve_layout", "recompose"}
STYLE_QUALITIES = {"preview", "standard", "high"}


@dataclass(frozen=True)
class StyleParameters:
    mode: str = "preserve_layout"
    quality: str = "standard"
    style_strength: float = 0.7
    content_strength: float = 0.8
    detail_strength: float = 0.7
    prompt: str = ""
    seed: int | None = None

    def validated(self) -> StyleParameters:
        if self.mode not in STYLE_MODES:
            raise AssetError("风格化模式必须是保留布局或重新构图。")
        if self.quality not in STYLE_QUALITIES:
            raise AssetError("质量档位必须是预览、标准或高质量。")
        for label, value in (
            ("风格强度", self.style_strength),
            ("内容保持", self.content_strength),
            ("细节保持", self.detail_strength),
        ):
            if not 0 <= value <= 1:
                raise AssetError(f"{label}必须在 0 到 1 之间。")
        if len(self.prompt) > 1000:
            raise AssetError("补充描述不能超过 1000 个字符。")
        if self.seed is not None and not 0 <= self.seed <= 2**63 - 1:
            raise AssetError("随机种子必须是非负 63 位整数。")
        return self

    def public_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "quality": self.quality,
            "style_strength": self.style_strength,
            "content_strength": self.content_strength,
            "detail_strength": self.detail_strength,
            "prompt": self.prompt,
            "seed": self.seed,
        }


@dataclass(frozen=True)
class ProviderResult:
    image: Image.Image
    provider_name: str
    model_name: str
    metadata: dict[str, Any]


class StyleTransferProvider(Protocol):
    name: str
    model_name: str

    def stylize(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult: ...

    def close(self) -> None: ...


class LocalColorStyleProvider:
    """Deterministic CPU preview provider adapted from frogi-m/pic-style."""

    name = "local-preview"
    model_name = "Deterministic color transfer v1"

    def stylize(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult:
        progress(38, "encoding_style", "正在分析参考图的色彩与纹理。")
        target_edge = 640 if parameters.quality == "preview" else 768
        prepared = content.copy()
        prepared.thumbnail((target_edge, target_edge), Image.Resampling.LANCZOS)
        content_array = np.asarray(prepared, dtype=np.float32)
        style_samples = [
            np.asarray(style.resize((96, 96), Image.Resampling.LANCZOS), dtype=np.float32)
            for style in styles
        ]
        style_pixels = np.concatenate(
            [sample.reshape(-1, 3) for sample in style_samples], axis=0
        )
        content_pixels = content_array.reshape(-1, 3)
        content_mean = content_pixels.mean(axis=0)
        content_std = np.maximum(content_pixels.std(axis=0), 1.0)
        style_mean = style_pixels.mean(axis=0)
        style_std = np.maximum(style_pixels.std(axis=0), 1.0)
        color_matched = (
            (content_array - content_mean) * (style_std / content_std) + style_mean
        )
        color_matched = np.clip(color_matched, 0, 255).astype(np.uint8)
        color_image = Image.fromarray(color_matched, "RGB")

        progress(68, "generating", "正在迁移风格并保持主体结构。")
        color_mix = 0.12 + 0.58 * parameters.style_strength
        color_mix *= 1.0 - 0.32 * parameters.content_strength
        output = Image.blend(prepared, color_image, min(0.72, color_mix))

        if parameters.mode == "recompose":
            texture_layers = [ImageOps.fit(style, prepared.size) for style in styles]
            texture = texture_layers[0]
            for index, layer in enumerate(texture_layers[1:], start=2):
                texture = Image.blend(texture, layer, 1 / index)
            texture = texture.filter(ImageFilter.GaussianBlur(radius=1.2))
            output = Image.blend(
                output,
                texture,
                0.06 + 0.18 * parameters.style_strength,
            )

        sharpness = 0.75 + 0.75 * parameters.detail_strength
        output = ImageEnhance.Sharpness(output).enhance(sharpness)
        return ProviderResult(
            image=output,
            provider_name=self.name,
            model_name=self.model_name,
            metadata={
                "algorithm": "weighted_rgb_distribution_transfer_v1",
                "style_reference_count": len(styles),
                "model_download_required": False,
                "production_quality": False,
            },
        )

    def close(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "ready": True,
            "loaded": True,
            "models_ready": True,
            "dependencies_ready": True,
            "cuda_available": False,
            "gate": "not_required",
        }


class PicStyleHttpProvider:
    """Provider-neutral adapter for the frogi-m/pic-style HTTP Skill."""

    name = "pic-style-http"
    model_name = "SDXL + IP-Adapter (pic-style service)"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        tenant_id: str = "personal-agent",
        timeout_seconds: float = 900,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise AssetError("PHOTO_STYLE_SERVICE_URL 尚未配置。")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=60)

    def _headers(self) -> dict[str, str]:
        headers = {"X-Tenant-ID": self.tenant_id}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _upload(self, image: Image.Image, filename: str) -> str:
        buffer = BytesIO()
        image.save(buffer, "PNG", optimize=True)
        response = self.client.post(
            f"{self.base_url}/v1/assets",
            files={"file": (filename, buffer.getvalue(), "image/png")},
            headers=self._headers(),
        )
        response.raise_for_status()
        return str(response.json()["asset_id"])

    def stylize(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult:
        try:
            progress(22, "uploading_provider", "正在把私有任务发送到已配置的本地风格服务。")
            content_id = self._upload(content, "content.png")
            style_ids = [
                self._upload(style, f"style-{index}.png")
                for index, style in enumerate(styles, start=1)
            ]
            response = self.client.post(
                f"{self.base_url}/v1/skills/photo-style-transfer/jobs",
                json={
                    "content_image": {"asset_id": content_id},
                    "style_images": [
                        {"image": {"asset_id": style_id}} for style_id in style_ids
                    ],
                    **parameters.public_dict(),
                    "num_outputs": 1,
                },
                headers={**self._headers(), "Idempotency-Key": str(uuid4())},
            )
            response.raise_for_status()
            job_id = str(response.json()["job_id"])
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                status_response = self.client.get(
                    f"{self.base_url}/v1/skills/photo-style-transfer/jobs/{job_id}",
                    headers=self._headers(),
                )
                status_response.raise_for_status()
                payload = status_response.json()
                status = str(payload.get("status", ""))
                progress(
                    min(88, max(25, int(payload.get("progress", 25)))),
                    str(payload.get("stage", "provider_running")),
                    "本地风格模型正在生成结果。",
                )
                if status == "completed":
                    result = payload["result"]
                    output = result["outputs"][0]
                    upstream_provider = str(
                        result.get("provider") or self.model_name
                    )
                    production_quality = upstream_provider not in {
                        "fake",
                        "local-preview",
                        "preview",
                    }
                    output_url = urljoin(f"{self.base_url}/", str(output["url"]))
                    download = self.client.get(output_url, headers=self._headers())
                    download.raise_for_status()
                    image = SpatialSceneService._decode_image(download.content)
                    return ProviderResult(
                        image=image,
                        provider_name=self.name,
                        model_name=upstream_provider,
                        metadata={
                            "upstream_job_id": job_id,
                            "provider_version": result.get("provider_version"),
                            "model_versions": result.get("model_versions", {}),
                            "normalized_parameters": result.get(
                                "normalized_parameters", {}
                            ),
                            "production_quality": production_quality,
                        },
                    )
                if status in {"failed", "cancelled", "timed_out"}:
                    error = payload.get("error") or {}
                    raise AssetError(
                        str(error.get("message") or "外部风格服务未能完成任务。")
                    )
                time.sleep(0.5)
            raise AssetError("外部风格服务处理超时。")
        except AssetError:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise AssetError("无法调用已配置的图片风格化服务。") from exc

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def status(self) -> dict[str, Any]:
        try:
            response = self.client.get(
                f"{self.base_url}/health/ready",
                headers=self._headers(),
                timeout=3,
            )
            ready = response.status_code == 200
            upstream_status: Any = None
            try:
                upstream_status = response.json()
            except ValueError:
                upstream_status = None
            upstream_provider_status: Any = None
            if ready:
                try:
                    provider_response = self.client.get(
                        f"{self.base_url}/health/provider",
                        headers=self._headers(),
                        timeout=3,
                    )
                    if provider_response.status_code == 200:
                        upstream_provider_status = provider_response.json()
                except (httpx.HTTPError, ValueError):
                    upstream_provider_status = None
            upstream_provider = (
                str(upstream_provider_status.get("provider") or "")
                if isinstance(upstream_provider_status, dict)
                else ""
            )
            production_quality = bool(
                ready
                and upstream_provider
                and upstream_provider
                not in {"fake", "local-preview", "preview"}
            )
            return {
                "ready": ready,
                "loaded": ready,
                "remote": True,
                "configured": True,
                "gate": "managed_by_remote_service",
                "upstream_status": upstream_status,
                "upstream_provider_status": upstream_provider_status,
                "upstream_provider": upstream_provider or None,
                "production_quality": production_quality,
                "error": None if ready else "service_not_ready",
            }
        except httpx.HTTPError:
            return {
                "ready": False,
                "loaded": False,
                "remote": True,
                "configured": True,
                "gate": "managed_by_remote_service",
                "upstream_status": None,
                "upstream_provider_status": None,
                "upstream_provider": None,
                "production_quality": False,
                "error": "service_unreachable",
            }


def build_style_provider_from_env() -> StyleTransferProvider:
    selected = os.getenv("PHOTO_STYLE_PROVIDER", "pic-style-http").strip().lower()
    if selected in {"local-preview", "preview", "fake"}:
        return LocalColorStyleProvider()
    if selected in {"pic-style-http", "http"}:
        return PicStyleHttpProvider(
            os.getenv("PHOTO_STYLE_SERVICE_URL", "http://127.0.0.1:18000"),
            api_key=os.getenv("PHOTO_STYLE_SERVICE_API_KEY") or None,
            tenant_id=os.getenv("PHOTO_STYLE_TENANT_ID", "personal-agent"),
            timeout_seconds=float(os.getenv("PHOTO_STYLE_TIMEOUT_SECONDS", "900")),
        )
    if selected in {
        "sdxl-local",
        "sdxl-ip-adapter",
        "sdxl_ip_adapter_8gb_v1",
    }:
        from .sdxl_style_provider import NativeSDXLStyleProvider

        backend_root = Path(__file__).resolve().parents[1]
        project_root = backend_root.parent

        def configured_path(name: str, default: Path) -> Path:
            candidate = Path(os.getenv(name, str(default))).expanduser()
            if not candidate.is_absolute():
                candidate = project_root / candidate
            return candidate.resolve()

        model_root = configured_path(
            "PHOTO_STYLE_MODEL_ROOT",
            backend_root / "models" / "photo-style",
        )

        def enabled(name: str, default: str = "false") -> bool:
            return os.getenv(name, default).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        return NativeSDXLStyleProvider(
            model_root=model_root,
            manifest_path=configured_path(
                "PHOTO_STYLE_MODEL_MANIFEST",
                backend_root / "config" / "photo-style-models.json",
            ),
            gate_path=configured_path(
                "PHOTO_STYLE_PROVIDER_GATE",
                backend_root / "config" / "photo-style-provider-gate.json",
            ),
            lock_path=configured_path(
                "PHOTO_STYLE_MODEL_LOCK",
                model_root / "model-lock.json",
            ),
            lcm_preview_enabled=enabled("PHOTO_STYLE_LCM_PREVIEW_ENABLED"),
            verify_hashes=enabled("PHOTO_STYLE_VERIFY_MODEL_HASHES"),
            unload_after_generation=enabled(
                "PHOTO_STYLE_UNLOAD_AFTER_GENERATION",
                "true",
            ),
            accelerator=os.getenv("PHOTO_STYLE_ACCELERATOR", "auto"),
        )
    raise AssetError(f"未知图片风格化 Provider：{selected}")


class PhotoStyleService:
    def __init__(
        self,
        asset_library: SpatialSceneService,
        *,
        provider: StyleTransferProvider | None = None,
        max_workers: int = 1,
    ) -> None:
        self.asset_library = asset_library
        self.repository = asset_library.repository
        self.asset_dir = asset_library.asset_dir
        self.source_image_dir = asset_library.source_image_dir
        self.provider = provider or build_style_provider_from_env()
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="photo-style",
        )
        self._futures: set[Future[None]] = set()
        self._future_lock = threading.Lock()

    def create_transfer(
        self,
        content_bytes: bytes,
        style_bytes: list[bytes],
        *,
        original_name: str,
        title: str | None = None,
        parameters: StyleParameters | None = None,
        owner_id: str = "local",
        idempotency_key: str | None = None,
    ) -> PhotoStyleCreateResponse:
        existing = self._existing_idempotent_transfer(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        provider_status = self.provider_status()
        if provider_status["ready"] is False:
            raise AssetError(
                "独立 SDXL + IP-Adapter 服务尚未就绪，请先启动 GPU 服务并通过健康检查。"
            )
        if not 1 <= len(style_bytes) <= 3:
            raise AssetError("请选择一至三张风格参考图。")
        if not content_bytes or any(not item for item in style_bytes):
            raise AssetError("内容图和风格参考图不能为空。")
        if len(content_bytes) > MAX_UPLOAD_BYTES or any(
            len(item) > MAX_UPLOAD_BYTES for item in style_bytes
        ):
            raise AssetError("每张图片不能超过 20MB。")
        selected_parameters = (parameters or StyleParameters()).validated()
        if selected_parameters.seed is None:
            generated_seed = (
                uuid5(
                    NAMESPACE_URL,
                    f"agent-photo-style:{owner_id}:{idempotency_key}:seed",
                ).int
                & ((1 << 63) - 1)
                if idempotency_key
                else secrets.randbits(63)
            )
            selected_parameters = StyleParameters(
                **{
                    **selected_parameters.public_dict(),
                    "seed": generated_seed,
                }
            )

        content = SpatialSceneService._decode_image(content_bytes)
        styles = [SpatialSceneService._decode_image(item) for item in style_bytes]
        if idempotency_key:
            asset_id, job_id = self._idempotent_transfer_ids(
                owner_id,
                idempotency_key,
            )
        else:
            asset_id = str(uuid4())
            job_id = str(uuid4())
        directory = self.asset_dir / asset_id
        directory.mkdir(parents=True, exist_ok=bool(idempotency_key))
        os.chmod(directory, 0o700)
        try:
            content = SpatialSceneService._resize_for_output(content)
            content_path = directory / "source.webp"
            content.save(content_path, "WEBP", quality=94, method=6)
            os.chmod(content_path, 0o600)
            style_files: list[str] = []
            for index, style in enumerate(styles, start=1):
                style.thumbnail((768, 768), Image.Resampling.LANCZOS)
                style_path = directory / f"style-{index}.webp"
                style.save(style_path, "WEBP", quality=92, method=6)
                os.chmod(style_path, 0o600)
                style_files.append(style_path.name)

            fallback = Path(original_name).stem or "图片风格化"
            safe_title = " ".join((title or fallback).strip().split())[:80]
            metadata = {
                "width": content.width,
                "height": content.height,
                "source_file": content_path.name,
                "style_files": style_files,
                "preview_file": content_path.name,
                "result_file": None,
                "manifest_file": None,
                "model_name": self.provider.model_name,
                "provider_name": self.provider.name,
                "parameters": selected_parameters.public_dict(),
            }
            self.repository.create_asset_and_job(
                asset_id=asset_id,
                job_id=job_id,
                name=safe_title or "图片风格化",
                metadata=metadata,
                owner_id=owner_id,
                kind="photo_style_transfer",
                queued_message="图片风格化任务已进入本地处理队列。",
            )
        except Exception:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)
            raise

        future = self.executor.submit(self._process, asset_id, job_id)
        with self._future_lock:
            self._futures.add(future)
        future.add_done_callback(self._forget_future)
        return PhotoStyleCreateResponse(
            asset=self.asset_library.get_asset(asset_id),
            job=self.asset_library.get_job(job_id),
        )

    def provider_status(self) -> dict[str, Any]:
        status_method = getattr(self.provider, "status", None)
        details = status_method() if callable(status_method) else {}
        return {
            "name": self.provider.name,
            "model_name": self.provider.model_name,
            "ready": details.get("ready"),
            "details": details,
        }

    def create_transfer_from_sources(
        self,
        content_image_id: str,
        style_image_ids: list[str],
        *,
        title: str | None = None,
        parameters: StyleParameters | None = None,
        owner_id: str = "local",
        idempotency_key: str | None = None,
    ) -> PhotoStyleCreateResponse:
        existing = self._existing_idempotent_transfer(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return existing
        if content_image_id in style_image_ids or len(set(style_image_ids)) != len(
            style_image_ids
        ):
            raise AssetError("内容图和风格参考图必须使用不同的附件。")
        content = self.asset_library.get_source_image(
            content_image_id,
            owner_id=owner_id,
        )
        styles = [
            self.asset_library.get_source_image(
                source_id,
                owner_id=owner_id,
            )
            for source_id in style_image_ids
        ]
        content_path = self.source_image_dir / content_image_id / "source.webp"
        style_paths = [
            self.source_image_dir / source.id / "source.webp" for source in styles
        ]
        try:
            created = self.create_transfer(
                content_path.read_bytes(),
                [path.read_bytes() for path in style_paths],
                original_name=content.original_name,
                title=title,
                parameters=parameters,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
        except OSError as exc:
            raise AssetError("图片附件无法读取，请重新选择。") from exc
        for source_id in (content_image_id, *style_image_ids):
            try:
                self.asset_library.delete_source_image(
                    source_id,
                    owner_id=owner_id,
                )
            except AssetError:
                LOGGER.warning(
                    "Unable to remove consumed source image %s",
                    source_id,
                )
        return created

    def retry_job(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
    ) -> tuple[JobPublic, bool]:
        """Retry one failed style job using its persisted sanitized inputs."""
        job = self.asset_library.get_job(job_id, owner_id=owner_id)
        if job.kind != "photo_style_transfer":
            raise AssetError("这个任务不支持图片风格化重试。")
        if job.status in {"queued", "running", "completed"}:
            return job, False

        asset_row = self.repository.get_asset_row(job.asset_id, owner_id)
        if asset_row is None:
            raise AssetError("找不到这个任务对应的风格化资产。")
        metadata = json.loads(asset_row["metadata_json"])
        filenames = [metadata.get("source_file")]
        style_files = metadata.get("style_files")
        if not isinstance(style_files, list) or not style_files:
            raise AssetError("风格参考图已经丢失，请重新上传。")
        filenames.extend(style_files)
        directory = self.asset_dir / job.asset_id
        if any(
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not (directory / filename).is_file()
            for filename in filenames
        ):
            raise AssetError("原始图片已经丢失，请重新上传。")

        started = self.repository.claim_failed_job_retry(
            job_id,
            owner_id=owner_id,
        )
        refreshed = self.asset_library.get_job(job_id, owner_id=owner_id)
        if not started:
            return refreshed, False

        future = self.executor.submit(self._process, job.asset_id, job_id)
        with self._future_lock:
            self._futures.add(future)
        future.add_done_callback(self._forget_future)
        return refreshed, True

    @staticmethod
    def _idempotent_transfer_ids(
        owner_id: str,
        idempotency_key: str,
    ) -> tuple[str, str]:
        namespace = f"agent-photo-style:{owner_id}:{idempotency_key}"
        return (
            str(uuid5(NAMESPACE_URL, namespace + ":asset")),
            str(uuid5(NAMESPACE_URL, namespace + ":job")),
        )

    def _existing_idempotent_transfer(
        self,
        *,
        owner_id: str,
        idempotency_key: str | None,
    ) -> PhotoStyleCreateResponse | None:
        if not idempotency_key:
            return None
        asset_id, job_id = self._idempotent_transfer_ids(
            owner_id,
            idempotency_key,
        )
        asset_row = self.repository.get_asset_row(asset_id, owner_id)
        job_row = self.repository.get_job_row(job_id, owner_id)
        if asset_row is None or job_row is None:
            return None
        return PhotoStyleCreateResponse(
            asset=self.asset_library.get_asset(asset_id, owner_id=owner_id),
            job=self.asset_library.get_job(job_id, owner_id=owner_id),
        )

    def _forget_future(self, future: Future[None]) -> None:
        with self._future_lock:
            self._futures.discard(future)

    def _process(self, asset_id: str, job_id: str) -> None:
        def progress(value: int, stage: str, message: str) -> None:
            self.repository.update_job(
                job_id,
                status="running",
                progress=value,
                stage=stage,
                message=message,
            )

        try:
            progress(8, "preparing", "正在规范化图片并移除原始元数据。")
            row = self.repository.get_asset_row(asset_id)
            if row is None:
                return
            metadata = json.loads(row["metadata_json"])
            directory = self.asset_dir / asset_id
            with Image.open(directory / metadata["source_file"]) as source:
                content = source.convert("RGB")
            styles: list[Image.Image] = []
            for filename in metadata["style_files"]:
                with Image.open(directory / filename) as style:
                    styles.append(style.convert("RGB"))
            parameters = StyleParameters(**metadata["parameters"]).validated()
            result = self.provider.stylize(content, styles, parameters, progress)

            progress(90, "packaging", "正在保存风格化结果与可复现参数。")
            output = result.image.convert("RGB")
            result_path = directory / "result.webp"
            output.save(result_path, "WEBP", quality=94, method=6)
            os.chmod(result_path, 0o600)
            manifest_path = directory / "style.json"
            manifest = {
                "schema": "personal-agent.photo-style-transfer",
                "version": 1,
                "author": "Ma Xianggang",
                "content": metadata["source_file"],
                "styles": metadata["style_files"],
                "result": result_path.name,
                "width": output.width,
                "height": output.height,
                "provider": result.provider_name,
                "model": result.model_name,
                "parameters": parameters.public_dict(),
                "provider_metadata": result.metadata,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)
            metadata.update(
                {
                    "width": output.width,
                    "height": output.height,
                    "preview_file": result_path.name,
                    "result_file": result_path.name,
                    "manifest_file": manifest_path.name,
                    "model_name": result.model_name,
                    "provider_name": result.provider_name,
                    "provider_metadata": result.metadata,
                }
            )
            self.repository.complete_asset(asset_id, metadata)
            self.repository.update_job(
                job_id,
                status="completed",
                progress=100,
                stage="completed",
                message="图片风格化已完成，可以查看或下载结果。",
            )
        except Exception as exc:
            LOGGER.exception("Photo style job failed: %s", job_id)
            message = (
                str(exc)
                if isinstance(exc, AssetError)
                else "图片风格化失败，请检查本地 Provider 后重试。"
            )
            self.repository.fail_asset(asset_id)
            self.repository.update_job(
                job_id,
                status="failed",
                progress=100,
                stage="failed",
                message=message,
                error=message,
            )

    def wait_for_idle(self, timeout: float = 10) -> None:
        with self._future_lock:
            futures = list(self._futures)
        if futures:
            wait(futures, timeout=timeout)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.provider.close()


def register_style_tools(
    registry: ToolRegistry,
    service: PhotoStyleService,
) -> None:
    def create_photo_style_transfer(arguments: dict[str, Any]) -> dict[str, Any]:
        content_image_id = arguments.get("content_image_id")
        style_image_ids = arguments.get("style_image_ids")
        if not isinstance(content_image_id, str) or not content_image_id.strip():
            raise ToolError("create_photo_style_transfer requires content_image_id")
        if (
            not isinstance(style_image_ids, list)
            or not 1 <= len(style_image_ids) <= 3
            or not all(isinstance(item, str) and item.strip() for item in style_image_ids)
        ):
            raise ToolError("create_photo_style_transfer requires 1-3 style_image_ids")
        try:
            parameters = StyleParameters(
                mode=str(arguments.get("mode", "preserve_layout")),
                quality=str(arguments.get("quality", "standard")),
                style_strength=float(arguments.get("style_strength", 0.7)),
                content_strength=float(arguments.get("content_strength", 0.8)),
                detail_strength=float(arguments.get("detail_strength", 0.7)),
                prompt=str(arguments.get("prompt", "")),
                seed=arguments.get("seed"),
            ).validated()
            execution_context = current_tool_context()
            created = service.create_transfer_from_sources(
                content_image_id.strip(),
                [item.strip() for item in style_image_ids],
                title=(str(arguments["title"]).strip() if arguments.get("title") else None),
                parameters=parameters,
                owner_id=execution_context.owner_id,
                idempotency_key=execution_context.idempotency_key,
            )
        except (AssetError, TypeError, ValueError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "asset_id": created.asset.id,
            "job_id": created.job.id,
            "status": created.job.status,
            "progress": created.job.progress,
            "kind": created.asset.kind,
            "message": "图片风格化任务已创建。",
        }

    input_schema = {
        "type": "object",
        "properties": {
            "content_image_id": {
                "type": "string",
                "description": "系统提供的本地内容图附件 ID",
            },
            "style_image_ids": {
                "type": "array",
                "description": "系统提供的一至三个本地风格参考图附件 ID",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string"},
            },
            "title": {"type": "string", "maxLength": 80},
            "prompt": {"type": "string", "maxLength": 1000},
            "mode": {
                "type": "string",
                "enum": ["preserve_layout", "recompose"],
                "default": "preserve_layout",
            },
            "quality": {
                "type": "string",
                "enum": ["preview", "standard", "high"],
                "default": "standard",
            },
            "style_strength": {"type": "number", "minimum": 0, "maximum": 1},
            "content_strength": {"type": "number", "minimum": 0, "maximum": 1},
            "detail_strength": {"type": "number", "minimum": 0, "maximum": 1},
            "seed": {"type": "integer", "minimum": 0},
        },
        "required": ["content_image_id", "style_image_ids"],
        "additionalProperties": False,
    }
    registry.register(
        ToolSpec(
            "create_photo_style_transfer",
            (
                "把本地内容图按一至三张本地参考图进行风格化；仅在系统同时提供"
                " content_image_id 和 style_image_ids 时调用"
            ),
            input_schema,
            create_photo_style_transfer,
            risk_level="local_write",
            idempotent=True,
            capability=CapabilityInfo(
                id="photo-style-transfer",
                name="图片风格化",
                version="1.2.0",
                author="Ma Xianggang",
                description="将一至三张参考图的视觉风格迁移到内容图，并可调节结构与细节保持程度。",
                entrypoint="create_photo_style_transfer",
                input_schema=input_schema,
                requirements=CapabilityRequirements(
                    local_model=(
                        "产品默认连接独立 pic-style SDXL + IP-Adapter 服务；"
                        "GPU Worker 至少约 8GB 显存，CPU Provider 仅用于显式开发测试"
                    ),
                    storage="本机 SQLite 与 backend/data/assets 私有文件目录",
                    permissions=[
                        "读取临时本地图片",
                        "写入本地个人资产",
                        "模型准备阶段访问 Hugging Face（运行时强制离线）",
                        "启用 HTTP Provider 时访问已配置的风格服务",
                    ],
                    downloads=(
                        "预览 Provider 无下载；真实 Provider 约 9.84 GiB，必须先审阅并接受模型许可证"
                    ),
                ),
            ),
        )
    )
