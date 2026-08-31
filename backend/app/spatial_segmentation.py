from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Protocol

import numpy as np
from PIL import Image, ImageFilter


LOGGER = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str, str], None]


@dataclass(frozen=True)
class ForegroundMaskResult:
    mask: Image.Image
    model_name: str
    quality_score: float
    warnings: tuple[str, ...] = ()


class ForegroundSegmenter(Protocol):
    def segment(
        self,
        image: Image.Image,
        depth: Image.Image,
        progress: ProgressCallback,
    ) -> ForegroundMaskResult: ...


def _otsu_threshold(values: np.ndarray) -> int:
    histogram = np.bincount(values.reshape(-1), minlength=256).astype(np.float64)
    total = values.size
    weighted_total = float(np.dot(np.arange(256), histogram))
    background_weight = 0.0
    background_sum = 0.0
    best_variance = -1.0
    best_threshold = 127
    for threshold in range(255):
        background_weight += histogram[threshold]
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * histogram[threshold]
        background_mean = background_sum / background_weight
        foreground_mean = (
            weighted_total - background_sum
        ) / foreground_weight
        variance = (
            background_weight
            * foreground_weight
            * (background_mean - foreground_mean) ** 2
        )
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _mask_quality(mask: Image.Image) -> tuple[float, tuple[str, ...]]:
    working = mask.convert("L")
    working.thumbnail((256, 256), Image.Resampling.BILINEAR)
    binary = np.asarray(working, dtype=np.uint8) >= 128
    area_ratio = float(binary.mean())
    warnings: list[str] = []
    score = 1.0
    if area_ratio < 0.02:
        warnings.append("foreground_too_small")
        score -= 0.7
    elif area_ratio < 0.05:
        warnings.append("foreground_small")
        score -= 0.25
    if area_ratio > 0.85:
        warnings.append("foreground_too_large")
        score -= 0.65
    elif area_ratio > 0.72:
        warnings.append("foreground_large")
        score -= 0.2

    height, width = binary.shape
    seen = np.zeros_like(binary, dtype=bool)
    component_sizes: list[int] = []
    for y, x in zip(*np.nonzero(binary)):
        if seen[y, x]:
            continue
        size = 0
        queue: deque[tuple[int, int]] = deque([(int(y), int(x))])
        seen[y, x] = True
        while queue:
            current_y, current_x = queue.popleft()
            size += 1
            for offset_y, offset_x in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                next_y = current_y + offset_y
                next_x = current_x + offset_x
                if (
                    0 <= next_y < height
                    and 0 <= next_x < width
                    and binary[next_y, next_x]
                    and not seen[next_y, next_x]
                ):
                    seen[next_y, next_x] = True
                    queue.append((next_y, next_x))
        if size >= max(8, int(binary.size * 0.001)):
            component_sizes.append(size)

    foreground_pixels = int(binary.sum())
    if foreground_pixels:
        largest_ratio = max(component_sizes, default=0) / foreground_pixels
        if largest_ratio < 0.55:
            warnings.append("foreground_fragmented")
            score -= 0.35
        elif largest_ratio < 0.75:
            warnings.append("foreground_multi_part")
            score -= 0.12
    if len(component_sizes) > 8:
        warnings.append("too_many_components")
        score -= 0.2
    return max(0.0, min(1.0, score)), tuple(warnings)


class DepthThresholdForegroundSegmenter:
    """Cross-platform fallback; depth is only used when semantic masking is absent."""

    model_name = "深度蒙版降级 v2"

    def segment(
        self,
        image: Image.Image,
        depth: Image.Image,
        progress: ProgressCallback,
    ) -> ForegroundMaskResult:
        del image
        progress(68, "segmenting", "语义分割不可用，正在使用深度降级模式…")
        values = np.asarray(depth.convert("L"), dtype=np.uint8)
        threshold = max(
            _otsu_threshold(values),
            int(np.percentile(values, 70)),
        )
        mask = Image.fromarray(
            np.where(values >= threshold, 255, 0).astype(np.uint8),
        )
        mask = mask.filter(ImageFilter.MaxFilter(7)).filter(
            ImageFilter.MinFilter(5)
        )
        quality, warnings = _mask_quality(mask)
        return ForegroundMaskResult(
            mask=mask,
            model_name=self.model_name,
            quality_score=min(quality, 0.52),
            warnings=("semantic_segmenter_unavailable", *warnings),
        )


class MacOSVisionForegroundSegmenter:
    model_name = "Apple Vision 主体分割"

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cache_dir, 0o700)
        self.binary_path = self.cache_dir / "macos-foreground-mask"
        self.source_path = (
            Path(__file__).resolve().parents[1]
            / "native"
            / "macos_foreground_mask.m"
        )
        self._compile_lock = threading.Lock()

    @staticmethod
    def supported() -> bool:
        if platform.system() != "Darwin":
            return False
        try:
            major = int(platform.mac_ver()[0].split(".")[0])
        except (ValueError, IndexError):
            return False
        return major >= 14 and shutil.which("clang") is not None

    def _ensure_binary(self) -> None:
        if (
            self.binary_path.is_file()
            and self.binary_path.stat().st_mtime >= self.source_path.stat().st_mtime
        ):
            return
        with self._compile_lock:
            if (
                self.binary_path.is_file()
                and self.binary_path.stat().st_mtime
                >= self.source_path.stat().st_mtime
            ):
                return
            clang = shutil.which("clang")
            if clang is None or not self.source_path.is_file():
                raise RuntimeError("macOS Vision helper source is unavailable")
            temporary_binary = self.binary_path.with_suffix(".tmp")
            result = subprocess.run(
                [
                    clang,
                    "-fobjc-arc",
                    "-framework",
                    "Foundation",
                    "-framework",
                    "CoreGraphics",
                    "-framework",
                    "CoreImage",
                    "-framework",
                    "CoreVideo",
                    "-framework",
                    "Vision",
                    str(self.source_path),
                    "-o",
                    str(temporary_binary),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                temporary_binary.unlink(missing_ok=True)
                raise RuntimeError(result.stderr[-1200:] or "Vision helper compile failed")
            os.chmod(temporary_binary, 0o700)
            temporary_binary.replace(self.binary_path)

    def segment(
        self,
        image: Image.Image,
        depth: Image.Image,
        progress: ProgressCallback,
    ) -> ForegroundMaskResult:
        del depth
        if not self.supported():
            raise RuntimeError("Apple Vision foreground masks are unsupported")
        progress(62, "segmenting", "正在识别完整主体并保持语义边界…")
        self._ensure_binary()
        with TemporaryDirectory(prefix="spatial-mask-", dir=self.cache_dir) as temp:
            input_path = Path(temp) / "input.png"
            output_path = Path(temp) / "mask.png"
            image.save(input_path, "PNG", optimize=True)
            result = subprocess.run(
                [str(self.binary_path), str(input_path), str(output_path)],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0 or not output_path.is_file():
                raise RuntimeError(result.stderr[-800:] or "Vision mask generation failed")
            with Image.open(output_path) as generated:
                mask = generated.convert("L")
                mask.load()
        if mask.size != image.size:
            mask = mask.resize(image.size, Image.Resampling.BILINEAR)
        quality, warnings = _mask_quality(mask)
        return ForegroundMaskResult(
            mask=mask,
            model_name=self.model_name,
            quality_score=quality,
            warnings=warnings,
        )


class BiRefNetForegroundSegmenter:
    """Cross-platform semantic foreground masking with local BiRefNet weights."""

    model_name = "BiRefNet 主体分割"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        image_size: int | None = None,
        local_files_only: bool | None = None,
    ) -> None:
        self.model_id = model_id or os.getenv(
            "SPATIAL_BIREFNET_MODEL",
            "ZhengPeng7/BiRefNet",
        )
        self.image_size = image_size or int(os.getenv("SPATIAL_BIREFNET_SIZE", "1024"))
        self.local_files_only = (
            local_files_only
            if local_files_only is not None
            else os.getenv("SPATIAL_BIREFNET_LOCAL_FILES_ONLY", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        self._lock = threading.Lock()
        self._model = None
        self._torch = None
        self._transforms = None
        self._device = "cpu"

    @staticmethod
    def supported() -> bool:
        return all(
            find_spec(module_name) is not None
            for module_name in ("torch", "torchvision", "transformers")
        )

    def _select_device(self, torch_module) -> str:
        if torch_module.cuda.is_available():
            return "cuda"
        mps = getattr(torch_module.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"

    def _load(self, progress: ProgressCallback) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            progress(
                60,
                "segmenting",
                "正在加载 BiRefNet 主体分割模型，首次运行可能需要下载权重…",
            )
            import torch
            from torchvision import transforms
            from transformers import AutoModelForImageSegmentation

            device = self._select_device(torch)
            try:
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass
            model = AutoModelForImageSegmentation.from_pretrained(
                self.model_id,
                trust_remote_code=True,
                local_files_only=self.local_files_only,
            )
            model.to(device)
            model.eval()
            self._torch = torch
            self._transforms = transforms.Compose(
                [
                    transforms.Resize((self.image_size, self.image_size)),
                    transforms.ToTensor(),
                    transforms.Normalize(
                        [0.485, 0.456, 0.406],
                        [0.229, 0.224, 0.225],
                    ),
                ]
            )
            self._model = model
            self._device = device

    def segment(
        self,
        image: Image.Image,
        depth: Image.Image,
        progress: ProgressCallback,
    ) -> ForegroundMaskResult:
        del depth
        if not self.supported():
            raise RuntimeError("BiRefNet dependencies are unavailable")
        self._load(progress)
        if self._model is None or self._torch is None or self._transforms is None:
            raise RuntimeError("BiRefNet model was not loaded")

        progress(66, "segmenting", "正在用 BiRefNet 识别完整主体…")
        tensor = self._transforms(image.convert("RGB")).unsqueeze(0).to(self._device)
        with self._torch.inference_mode():
            prediction = self._model(tensor)[-1].sigmoid().detach().cpu()
        pred = prediction[0].squeeze()
        transforms = __import__("torchvision.transforms", fromlist=["ToPILImage"])
        mask = transforms.ToPILImage()(pred).resize(image.size, Image.Resampling.BILINEAR)
        mask = mask.filter(ImageFilter.GaussianBlur(radius=0.35))
        quality, warnings = _mask_quality(mask)
        return ForegroundMaskResult(
            mask=mask,
            model_name=f"{self.model_name} ({self.model_id}, {self._device})",
            quality_score=quality,
            warnings=warnings,
        )


class AutoForegroundSegmenter:
    def __init__(self, cache_dir: Path) -> None:
        self.native = MacOSVisionForegroundSegmenter(cache_dir)
        self.birefnet = BiRefNetForegroundSegmenter()
        self.fallback = DepthThresholdForegroundSegmenter()
        self.mode = os.getenv("SPATIAL_FOREGROUND_SEGMENTER", "auto").strip().lower()

    def segment(
        self,
        image: Image.Image,
        depth: Image.Image,
        progress: ProgressCallback,
    ) -> ForegroundMaskResult:
        if self.mode not in {"auto", "vision", "birefnet", "depth"}:
            LOGGER.warning(
                "Unknown SPATIAL_FOREGROUND_SEGMENTER=%s; using auto",
                self.mode,
            )
        mode = (
            self.mode
            if self.mode in {"auto", "vision", "birefnet", "depth"}
            else "auto"
        )
        if mode in {"auto", "vision"} and self.native.supported():
            try:
                return self.native.segment(image, depth, progress)
            except Exception:
                LOGGER.exception("Apple Vision segmentation failed; using depth fallback")
                if mode == "vision":
                    raise
        if mode in {"auto", "birefnet"}:
            try:
                return self.birefnet.segment(image, depth, progress)
            except Exception:
                LOGGER.exception("BiRefNet segmentation failed; using depth fallback")
                if mode == "birefnet":
                    raise
        return self.fallback.segment(image, depth, progress)
