from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from PIL import Image

from .assets import AssetError
from .style_model_manifest import (
    ModelManifest,
    load_model_manifest,
    resolve_component_dir,
    verify_prepared_models,
    verify_provider_gate,
)
from .style_transfer import ProgressCallback, ProviderResult, StyleParameters


LOGGER = logging.getLogger(__name__)
DEFAULT_PROMPT = (
    "faithful stylization of the same subject and composition, preserve identity, "
    "layout, geometry and important details, adopt the reference visual style"
)
DEFAULT_NEGATIVE_PROMPT = (
    "warped geometry, changed identity, duplicate subject, extra limbs, text, letters, "
    "numbers, watermark, signature, logo, blur, low detail"
)
_QUALITY_DEFAULTS: dict[str, dict[str, int | str]] = {
    "preview": {
        "path": "base_preview_fallback_v1",
        "steps": 12,
        "max_long_edge": 640,
        "pixel_budget": 400_000,
    },
    "standard": {
        "path": "base_sdxl_v1",
        "steps": 16,
        "max_long_edge": 768,
        "pixel_budget": 600_000,
    },
    "high": {
        "path": "base_sdxl_v1",
        "steps": 22,
        "max_long_edge": 768,
        "pixel_budget": 600_000,
    },
}


def _clip(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def scheduler_steps_for_effective_steps(effective_steps: int, strength: float) -> int:
    scheduler_steps = max(effective_steps, math.ceil(effective_steps / strength))
    while int(scheduler_steps * strength) < effective_steps:
        scheduler_steps += 1
    return scheduler_steps


def lcm_scheduler_config(config: dict[str, Any]) -> dict[str, Any]:
    """Drop scheduler fields that LCMScheduler does not consume."""
    return {
        key: value
        for key, value in config.items()
        if key != "skip_prk_steps"
    }


def provider_gate_local_validation_status(gate_path: Path) -> str:
    try:
        payload = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "pending"
    local_validation = payload.get("local_validation")
    if not isinstance(local_validation, dict):
        return "pending"
    status = local_validation.get("status")
    return status if status == "engineering_smoke_passed" else "pending"


def style_layer_scale(scale: float) -> dict[str, dict[str, list[float]]]:
    return {
        "down": {"block_2": [0.0, scale]},
        "up": {"block_0": [0.0, scale, 0.0]},
    }


def probe_cuda_runtime() -> tuple[bool, int | None]:
    """Probe Torch/CUDA in a short-lived process so importing Torch cannot bloat the API."""
    script = (
        "import json, torch; "
        "available = bool(torch.cuda.is_available()); "
        "total = (round(torch.cuda.get_device_properties(0).total_memory / 1024**2) "
        "if available else None); "
        "print(json.dumps({'available': available, 'total_mib': total}))"
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            creationflags=creationflags,
        )
        if completed.returncode != 0:
            return False, None
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        available = bool(payload.get("available"))
        total_mib = payload.get("total_mib")
        return available, int(total_mib) if total_mib is not None else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError):
        return False, None


def map_native_parameters(parameters: StyleParameters) -> dict[str, Any]:
    mapped: dict[str, Any] = dict(_QUALITY_DEFAULTS[parameters.quality])
    style_scale = _clip(0.35 + 0.72 * parameters.style_strength, 0.35, 1.07)
    if parameters.mode == "preserve_layout":
        denoise = (
            0.24
            + 0.45 * (1.0 - parameters.content_strength)
            + 0.06 * (1.0 - parameters.detail_strength)
            + 0.10 * parameters.style_strength
        )
        denoise = _clip(denoise, 0.24, 0.52)
    else:
        denoise = (
            0.48
            + 0.27 * (1.0 - parameters.content_strength)
            + 0.08 * parameters.style_strength
        )
        denoise = _clip(denoise, 0.45, 0.82)
    mapped.update(
        {
            "mapping_version": "business_to_sdxl_v2_fixed_pair_tuned",
            "style_scale": round(style_scale, 4),
            "denoise_strength": round(denoise, 4),
            "guidance_scale": round(
                _clip(5.0 + 1.4 * parameters.style_strength, 5.0, 6.4),
                4,
            ),
            "controlnet_enabled": False,
            "lcm_preview_requested": parameters.quality == "preview",
        }
    )
    return mapped


def content_dimensions(
    width: int,
    height: int,
    *,
    max_long_edge: int,
    pixel_budget: int,
) -> tuple[int, int]:
    scale = min(
        1.0,
        max_long_edge / max(width, height),
        math.sqrt(pixel_budget / (width * height)),
    )
    scaled_width = max(64, round(width * scale))
    scaled_height = max(64, round(height * scale))
    if scaled_width >= scaled_height:
        target_width = max(64, min(max_long_edge, round(scaled_width / 64) * 64))
        target_height = max(
            64,
            round((target_width * height / width) / 64) * 64,
        )
    else:
        target_height = max(
            64,
            min(max_long_edge, round(scaled_height / 64) * 64),
        )
        target_width = max(
            64,
            round((target_height * width / height) / 64) * 64,
        )
    return min(768, target_width), min(768, target_height)


class NativeSDXLStyleProvider:
    """Pinned, local-only SDXL img2img + IP-Adapter provider for an 8 GB GPU."""

    name = "sdxl_ip_adapter_8gb_v1"
    model_name = "Stable Diffusion XL 1.0 + IP-Adapter SDXL ViT-H"
    version = "1.2.0"

    def __init__(
        self,
        *,
        model_root: Path,
        manifest_path: Path,
        gate_path: Path,
        lock_path: Path,
        lcm_preview_enabled: bool = False,
        verify_hashes: bool = False,
        embedding_cache_entries: int = 16,
        unload_after_generation: bool = True,
    ) -> None:
        self.model_root = model_root
        self.manifest_path = manifest_path
        self.gate_path = gate_path
        self.lock_path = lock_path
        self.lcm_preview_enabled = lcm_preview_enabled
        self.verify_hashes = verify_hashes
        self.embedding_cache_entries = max(1, min(128, embedding_cache_entries))
        self.unload_after_generation = unload_after_generation
        self._pipeline: Any | None = None
        self._torch: Any | None = None
        self._manifest: ModelManifest | None = None
        self._base_scheduler_config: dict[str, Any] | None = None
        self._lcm_loaded = False
        self._load_seconds: float | None = None
        self._last_unload_seconds: float | None = None
        self._unload_count = 0
        self._load_error: str | None = None
        self._generation_lock = threading.RLock()
        self._embedding_cache: OrderedDict[str, tuple[Any, Any]] = OrderedDict()
        self._embedding_cache_hits = 0

    def status(self) -> dict[str, Any]:
        gate_errors: list[str] = []
        model_errors: list[str] = []
        manifest: ModelManifest | None = None
        try:
            manifest = load_model_manifest(self.manifest_path)
            gate_errors.extend(verify_provider_gate(manifest, self.gate_path))
            model_errors.extend(
                verify_prepared_models(
                    manifest,
                    self.model_root,
                    lock_path=self.lock_path,
                    verify_hashes=False,
                )
            )
        except (OSError, ValueError, TypeError) as exc:
            gate_errors.append(f"manifest invalid: {type(exc).__name__}")
        cuda_available = False
        cuda_memory_mib: int | None = None
        if importlib.util.find_spec("torch") is not None:
            cuda_available, cuda_memory_mib = probe_cuda_runtime()
        dependencies_ready = all(
            importlib.util.find_spec(module) is not None
            for module in ("torch", "accelerate", "diffusers", "peft", "safetensors")
        )
        models_ready = manifest is not None and not model_errors
        ready = (
            not gate_errors
            and models_ready
            and dependencies_ready
            and cuda_available
        )
        return {
            "name": self.name,
            "model_name": self.model_name,
            "loaded": self._pipeline is not None,
            "ready": ready,
            "models_ready": models_ready,
            "dependencies_ready": dependencies_ready,
            "cuda_available": cuda_available,
            "cuda_memory_mib": cuda_memory_mib,
            "cuda_probe": "isolated_process",
            "gate": "accepted_upstream" if not gate_errors else "unverified",
            "local_quality_validation": (
                provider_gate_local_validation_status(self.gate_path)
            ),
            "lcm_preview_enabled": self.lcm_preview_enabled,
            "lcm_loaded": self._lcm_loaded,
            "load_seconds": self._load_seconds,
            "unload_after_generation": self.unload_after_generation,
            "execution_mode": (
                "isolated_process"
                if self.unload_after_generation
                else "resident_process"
            ),
            "last_unload_seconds": self._last_unload_seconds,
            "unload_count": self._unload_count,
            "load_error": self._load_error,
            "preflight_errors": [*gate_errors, *model_errors][:8],
        }

    def stylize(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult:
        if not 1 <= len(styles) <= 3:
            raise AssetError("真实 SDXL Provider 需要一至三张风格参考图。")
        if self.unload_after_generation:
            return self._stylize_isolated(content, styles, parameters, progress)
        self._load(progress)
        return self._stylize_loaded(content, styles, parameters, progress)

    def _stylize_isolated(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult:
        with self._generation_lock:
            with tempfile.TemporaryDirectory(prefix="photo-style-sdxl-") as temp_name:
                temp_dir = Path(temp_name)
                content_path = temp_dir / "content.png"
                style_paths = [
                    temp_dir / f"style-{index}.png"
                    for index in range(1, len(styles) + 1)
                ]
                result_path = temp_dir / "result.png"
                metadata_path = temp_dir / "metadata.json"
                request_path = temp_dir / "request.json"
                content.convert("RGB").save(content_path, "PNG")
                for style, style_path in zip(styles, style_paths, strict=True):
                    style.convert("RGB").save(style_path, "PNG")
                request_path.write_text(
                    json.dumps(
                        {
                            "model_root": str(self.model_root),
                            "manifest_path": str(self.manifest_path),
                            "gate_path": str(self.gate_path),
                            "lock_path": str(self.lock_path),
                            "lcm_preview_enabled": self.lcm_preview_enabled,
                            "verify_hashes": self.verify_hashes,
                            "embedding_cache_entries": self.embedding_cache_entries,
                            "content_path": str(content_path),
                            "style_paths": [str(path) for path in style_paths],
                            "result_path": str(result_path),
                            "metadata_path": str(metadata_path),
                            "parameters": parameters.public_dict(),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                environment = os.environ.copy()
                environment["PYTHONUNBUFFERED"] = "1"
                environment["PYTHONIOENCODING"] = "utf-8"
                environment["PYTHONUTF8"] = "1"
                creationflags = (
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                )
                command = [
                    sys.executable,
                    "-m",
                    "backend.app.sdxl_worker",
                    "--request",
                    str(request_path),
                ]
                diagnostics: list[str] = []
                worker_error = ""
                final_signal_at: float | None = None
                process: subprocess.Popen[str] | None = None
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=Path(__file__).resolve().parents[2],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=creationflags,
                    )
                    if process.stdout is None:
                        raise AssetError("无法读取隔离 SDXL 进程输出。")
                    for raw_line in process.stdout:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            diagnostics.append(line)
                            continue
                        if event.get("type") == "progress":
                            progress(
                                int(event["value"]),
                                str(event["stage"]),
                                str(event["message"]),
                            )
                        elif event.get("type") == "error":
                            worker_error = str(event.get("message") or "")
                        elif event.get("type") == "result":
                            final_signal_at = time.perf_counter()
                    return_code = process.wait()
                    if final_signal_at is None:
                        final_signal_at = time.perf_counter()
                    if return_code != 0 or worker_error:
                        if diagnostics:
                            LOGGER.error(
                                "Isolated SDXL worker diagnostics:\n%s",
                                "\n".join(diagnostics[-20:]),
                            )
                        raise AssetError(
                            worker_error or "隔离 SDXL 进程未能完成生成。"
                        )
                    if not result_path.is_file() or not metadata_path.is_file():
                        raise AssetError("隔离 SDXL 进程没有返回完整结果。")
                    with Image.open(result_path) as image:
                        output = image.convert("RGB")
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    runtime = metadata.setdefault("runtime", {})
                    runtime["unload_after_generation"] = True
                    runtime["process_isolation"] = True
                    load_seconds = runtime.get("model_load_seconds")
                    if isinstance(load_seconds, (int, float)):
                        self._load_seconds = float(load_seconds)
                    return ProviderResult(
                        image=output,
                        provider_name=self.name,
                        model_name=self.model_name,
                        metadata=metadata,
                    )
                except AssetError:
                    raise
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    LOGGER.exception("Unable to run isolated SDXL worker")
                    raise AssetError("无法启动隔离 SDXL 生成进程。") from exc
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    if process is not None:
                        self._unload_count += 1
                        self._last_unload_seconds = max(
                            0.0,
                            time.perf_counter()
                            - (final_signal_at or time.perf_counter()),
                        )

    def _stylize_loaded(
        self,
        content: Image.Image,
        styles: list[Image.Image],
        parameters: StyleParameters,
        progress: ProgressCallback,
    ) -> ProviderResult:
        with self._generation_lock:
            pipeline = self._require_pipeline()
            torch = self._require_torch()
            mapped = map_native_parameters(parameters)
            path = self._configure_inference_path(parameters.quality, mapped)
            effective_steps = int(mapped["steps"])
            guidance = float(mapped["guidance_scale"])
            if path == "lcm_lora_sdxl_preview_v1":
                effective_steps = min(6, effective_steps)
                guidance = 1.0
            denoise = float(mapped["denoise_strength"])
            scheduler_steps = scheduler_steps_for_effective_steps(
                effective_steps,
                denoise,
            )
            target = content_dimensions(
                content.width,
                content.height,
                max_long_edge=int(mapped["max_long_edge"]),
                pixel_budget=int(mapped["pixel_budget"]),
            )
            prepared_content = content.convert("RGB")
            if prepared_content.size != target:
                prepared_content = prepared_content.resize(
                    target,
                    Image.Resampling.LANCZOS,
                )
            prepared_styles = [self._prepare_style(style) for style in styles]
            pipeline.set_ip_adapter_scale(
                style_layer_scale(float(mapped["style_scale"]))
            )
            try:
                progress(30, "encoding_style", "正在编码风格参考并准备 SDXL 潜空间。")
                embeddings = self._weighted_style_embeddings(
                    prepared_styles,
                    do_classifier_free_guidance=guidance > 1.0,
                )
                actual_steps = 0

                def on_step_end(
                    _pipeline: Any,
                    _step_index: int,
                    _timestep: Any,
                    callback_kwargs: dict[str, Any],
                ) -> dict[str, Any]:
                    nonlocal actual_steps
                    actual_steps += 1
                    percent = 34 + round(52 * actual_steps / effective_steps)
                    progress(
                        min(86, percent),
                        "generating_sdxl",
                        f"SDXL 正在生成（{actual_steps}/{effective_steps}）…",
                    )
                    return callback_kwargs

                if hasattr(torch.cuda, "reset_peak_memory_stats"):
                    torch.cuda.reset_peak_memory_stats()
                started = time.perf_counter()
                generator = torch.Generator(device="cpu").manual_seed(
                    int(parameters.seed or 0)
                )
                generated = pipeline(
                    prompt=parameters.prompt.strip() or DEFAULT_PROMPT,
                    negative_prompt=DEFAULT_NEGATIVE_PROMPT,
                    image=prepared_content,
                    ip_adapter_image_embeds=embeddings,
                    strength=denoise,
                    num_inference_steps=scheduler_steps,
                    guidance_scale=guidance,
                    generator=generator,
                    callback_on_step_end=on_step_end,
                    callback_on_step_end_tensor_inputs=[],
                ).images[0]
                if actual_steps != effective_steps:
                    raise AssetError("SDXL 调度器未执行预期的有效步数。")
                peak_vram_mib = round(torch.cuda.max_memory_reserved() / 1024**2)
                manifest = self._require_manifest()
                return ProviderResult(
                    image=generated.convert("RGB"),
                    provider_name=self.name,
                    model_name=self.model_name,
                    metadata={
                        "provider_version": self.version,
                        "algorithm": "sdxl_img2img_ip_adapter_style_layers_v1",
                        "production_quality": False,
                        "quality_gate": "accepted_upstream",
                        "local_quality_validation": (
                            provider_gate_local_validation_status(self.gate_path)
                        ),
                        "model_download_required": True,
                        "model_versions": {
                            "base_model_id": manifest.base.id,
                            "base_model_revision": manifest.base.revision,
                            "adapter_id": manifest.ip_adapter.id,
                            "adapter_revision": manifest.ip_adapter.revision,
                            "accelerator_id": (
                                manifest.preview_accelerator.id
                                if path == "lcm_lora_sdxl_preview_v1"
                                else None
                            ),
                            "accelerator_revision": (
                                manifest.preview_accelerator.revision
                                if path == "lcm_lora_sdxl_preview_v1"
                                else None
                            ),
                        },
                        "inference_parameters": {
                            **mapped,
                            "path": path,
                            "seed": parameters.seed,
                            "scheduler": type(pipeline.scheduler).__name__,
                            "scheduler_steps": scheduler_steps,
                            "actual_steps": actual_steps,
                            "actual_width": generated.width,
                            "actual_height": generated.height,
                            "style_reference_count": len(prepared_styles),
                            "style_embedding_mode": "equal_weighted_mean_v1",
                            "controlnet_enabled": False,
                        },
                        "runtime": {
                            "generation_seconds": round(
                                time.perf_counter() - started,
                                3,
                            ),
                            "peak_reserved_vram_mib": peak_vram_mib,
                            "model_cpu_offload_sequence": (
                                "text_encoder->text_encoder_2->unet->vae"
                            ),
                            "unload_after_generation": self.unload_after_generation,
                        },
                        "warnings": self._warnings_for_path(path),
                    },
                )
            except AssetError:
                raise
            except Exception as exc:
                if isinstance(exc, torch.cuda.OutOfMemoryError):
                    torch.cuda.empty_cache()
                    raise AssetError(
                        "SDXL 生成时显存不足，请改用预览质量或缩小图片。"
                    ) from exc
                LOGGER.exception("Native SDXL style generation failed")
                raise AssetError("本机 SDXL Provider 生成失败。") from exc
            finally:
                try:
                    pipeline.set_ip_adapter_scale(style_layer_scale(0.85))
                    pipeline.maybe_free_model_hooks()
                except Exception:
                    LOGGER.warning("SDXL provider cleanup failed", exc_info=True)

    def unload(self) -> None:
        with self._generation_lock:
            started = time.perf_counter()
            pipeline = self._pipeline
            torch = self._torch
            had_runtime = pipeline is not None or torch is not None
            self._pipeline = None
            self._torch = None
            self._manifest = None
            self._base_scheduler_config = None
            self._lcm_loaded = False
            self._embedding_cache.clear()
            if pipeline is not None:
                try:
                    pipeline.maybe_free_model_hooks()
                except Exception:
                    LOGGER.warning("SDXL close hook cleanup failed", exc_info=True)
                del pipeline
            gc.collect()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
                ipc_collect = getattr(torch.cuda, "ipc_collect", None)
                if callable(ipc_collect):
                    ipc_collect()
            if had_runtime:
                self._unload_count += 1
                self._last_unload_seconds = time.perf_counter() - started

    def close(self) -> None:
        self.unload()

    def _load(self, progress: ProgressCallback) -> None:
        if self._pipeline is not None:
            return
        with self._generation_lock:
            if self._pipeline is not None:
                return
            started = time.perf_counter()
            progress(14, "verifying_models", "正在校验固定版本模型、许可记录与质量门禁。")
            try:
                manifest = load_model_manifest(self.manifest_path)
                errors = verify_provider_gate(manifest, self.gate_path)
                errors.extend(
                    verify_prepared_models(
                        manifest,
                        self.model_root,
                        lock_path=self.lock_path,
                        verify_hashes=self.verify_hashes,
                    )
                )
                if errors:
                    raise AssetError(
                        "本机 SDXL 模型尚未准备完成，请先运行模型准备脚本。"
                    )
                try:
                    import torch
                    from diffusers import AutoPipelineForImage2Image
                except ImportError as exc:
                    raise AssetError(
                        "缺少 SDXL GPU 依赖，请安装 backend/requirements-gpu.txt。"
                    ) from exc
                if not torch.cuda.is_available():
                    raise AssetError("本机 SDXL Provider 需要可用的 NVIDIA CUDA GPU。")
                total_memory = torch.cuda.get_device_properties(0).total_memory
                if total_memory < 7 * 1024**3:
                    raise AssetError("本机 SDXL Provider 至少需要约 8 GB 显存。")

                progress(20, "loading_models", "正在加载 SDXL 与 IP-Adapter 到本机 GPU。")
                base = resolve_component_dir(self.model_root, manifest.base)
                adapter = resolve_component_dir(self.model_root, manifest.ip_adapter)
                pipeline = AutoPipelineForImage2Image.from_pretrained(
                    base,
                    # Diffusers 0.35.2 still consumes torch_dtype here. The
                    # newer dtype alias is ignored by this AutoPipeline class.
                    torch_dtype=torch.float16,
                    variant=manifest.base.variant,
                    local_files_only=True,
                    use_safetensors=True,
                    add_watermarker=False,
                )
                base_scheduler_config = dict(pipeline.scheduler.config)
                pipeline.load_ip_adapter(
                    adapter,
                    subfolder=manifest.ip_adapter.subfolder,
                    weight_name=manifest.ip_adapter.weight_name,
                    image_encoder_folder=(
                        manifest.ip_adapter.image_encoder_subfolder
                    ),
                    local_files_only=True,
                )
                pipeline.set_ip_adapter_scale(style_layer_scale(0.85))
                if self.lcm_preview_enabled:
                    accelerator = resolve_component_dir(
                        self.model_root,
                        manifest.preview_accelerator,
                    )
                    pipeline.load_lora_weights(
                        accelerator,
                        weight_name=manifest.preview_accelerator.weight_name,
                        adapter_name="lcm-preview",
                        local_files_only=True,
                    )
                    pipeline.disable_lora()
                    self._lcm_loaded = True
                pipeline.enable_vae_tiling()
                pipeline.model_cpu_offload_seq = (
                    "text_encoder->text_encoder_2->unet->vae"
                )
                pipeline.enable_model_cpu_offload()
                self._pipeline = pipeline
                self._torch = torch
                self._manifest = manifest
                self._base_scheduler_config = base_scheduler_config
                self._load_seconds = time.perf_counter() - started
                self._load_error = None
            except AssetError as exc:
                self._load_error = str(exc)
                raise
            except Exception as exc:
                self._load_error = type(exc).__name__
                LOGGER.exception("Native SDXL model load failed")
                raise AssetError("无法加载固定版本的本机 SDXL 模型。") from exc

    def _configure_inference_path(
        self,
        quality: str,
        mapped: dict[str, Any],
    ) -> str:
        pipeline = self._require_pipeline()
        if self._base_scheduler_config is None:
            raise AssetError("SDXL 调度器尚未初始化。")
        if quality == "preview" and self._lcm_loaded:
            from diffusers import LCMScheduler

            pipeline.scheduler = LCMScheduler.from_config(
                lcm_scheduler_config(self._base_scheduler_config)
            )
            pipeline.enable_lora()
            pipeline.set_adapters("lcm-preview")
            return "lcm_lora_sdxl_preview_v1"
        from diffusers import DPMSolverMultistepScheduler

        if self._lcm_loaded:
            pipeline.disable_lora()
        pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
            self._base_scheduler_config
        )
        return str(mapped["path"])

    def _weighted_style_embeddings(
        self,
        styles: list[Image.Image],
        *,
        do_classifier_free_guidance: bool,
    ) -> list[Any]:
        pipeline = self._require_pipeline()
        torch = self._require_torch()
        manifest = self._require_manifest()
        projection_layers = pipeline.unet.encoder_hid_proj.image_projection_layers
        if len(projection_layers) != 1:
            raise AssetError("固定版本 Provider 只支持一个 IP-Adapter 投影层。")
        from diffusers.models.embeddings import ImageProjection

        output_hidden_state = not isinstance(projection_layers[0], ImageProjection)
        device = pipeline._execution_device
        cached: list[tuple[Any, Any]] = []
        encoded_any = False
        for style in styles:
            digest = hashlib.sha256()
            digest.update(style.mode.encode("ascii"))
            digest.update(f"{style.width}x{style.height}".encode("ascii"))
            digest.update(style.tobytes())
            digest.update(manifest.ip_adapter.revision.encode("ascii"))
            key = digest.hexdigest()
            entry = self._embedding_cache.get(key)
            if entry is None:
                positive, negative = pipeline.encode_image(
                    style,
                    device,
                    1,
                    output_hidden_state,
                )
                entry = (
                    positive.detach().to(device="cpu"),
                    negative.detach().to(device="cpu"),
                )
                self._embedding_cache[key] = entry
                encoded_any = True
                while len(self._embedding_cache) > self.embedding_cache_entries:
                    self._embedding_cache.popitem(last=False)
            else:
                self._embedding_cache_hits += 1
                self._embedding_cache.move_to_end(key)
            cached.append(entry)
        if encoded_any:
            pipeline.maybe_free_model_hooks()

        weight = 1.0 / len(cached)
        positive = cached[0][0] * weight
        negative = cached[0][1] * weight
        for item_positive, item_negative in cached[1:]:
            positive = positive + item_positive * weight
            negative = negative + item_negative * weight
        positive = positive.unsqueeze(0)
        negative = negative.unsqueeze(0)
        if do_classifier_free_guidance:
            return [torch.cat([negative, positive], dim=0)]
        return [positive]

    @staticmethod
    def _prepare_style(style: Image.Image, square_size: int = 512) -> Image.Image:
        source = style.convert("RGB")
        scale = min(square_size / source.width, square_size / source.height)
        resized = source.resize(
            (
                max(1, round(source.width * scale)),
                max(1, round(source.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
        average = resized.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        canvas = Image.new("RGB", (square_size, square_size), average)
        offset = (
            (square_size - resized.width) // 2,
            (square_size - resized.height) // 2,
        )
        canvas.paste(resized, offset)
        return canvas

    def _warnings_for_path(self, path: str) -> list[str]:
        if path == "base_preview_fallback_v1":
            return [
                "LCM preview is disabled; base-scheduler preview fallback was used."
            ]
        return []

    def _require_pipeline(self) -> Any:
        if self._pipeline is None:
            raise AssetError("本机 SDXL Pipeline 尚未加载。")
        return self._pipeline

    def _require_torch(self) -> Any:
        if self._torch is None:
            raise AssetError("本机 Torch CUDA 运行时尚未加载。")
        return self._torch

    def _require_manifest(self) -> ModelManifest:
        if self._manifest is None:
            raise AssetError("本机 SDXL 模型清单尚未加载。")
        return self._manifest
