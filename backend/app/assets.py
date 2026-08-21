from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator, Protocol
from uuid import uuid4

import numpy as np
from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

from .models import (
    AssetPublic,
    JobPublic,
    SourceImagePublic,
    SpatialSceneCreateResponse,
)
from .tools import ToolError, ToolRegistry, ToolSpec


LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_OUTPUT_EDGE = 1600
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
ProgressCallback = Callable[[int, str, str], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_title(value: str | None, fallback: str) -> str:
    title = (value or fallback).strip()
    title = " ".join(title.split())
    return title[:80] or "未命名空间照片"


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


def _nearest_background_fill(
    image: Image.Image,
    foreground_mask: Image.Image,
) -> Image.Image:
    working = image.copy()
    working.thumbnail((512, 512), Image.Resampling.LANCZOS)
    mask = foreground_mask.resize(working.size, Image.Resampling.NEAREST)
    colors = np.asarray(working, dtype=np.float32).copy()
    missing = np.asarray(mask, dtype=np.uint8) > 0
    valid = ~missing

    for _ in range(max(working.size)):
        if not missing.any():
            break
        neighbor_sum = np.zeros_like(colors)
        neighbor_count = np.zeros(missing.shape, dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            shifted_valid = np.roll(valid, (dy, dx), axis=(0, 1))
            shifted_colors = np.roll(colors, (dy, dx), axis=(0, 1))
            if dy < 0:
                shifted_valid[dy:, :] = False
            elif dy > 0:
                shifted_valid[:dy, :] = False
            if dx < 0:
                shifted_valid[:, dx:] = False
            elif dx > 0:
                shifted_valid[:, :dx] = False
            neighbor_sum += shifted_colors * shifted_valid[..., None]
            neighbor_count += shifted_valid

        fillable = missing & (neighbor_count > 0)
        if not fillable.any():
            break
        colors[fillable] = (
            neighbor_sum[fillable] / neighbor_count[fillable, None]
        )
        valid[fillable] = True
        missing[fillable] = False

    if missing.any():
        fallback = colors[valid].mean(axis=0) if valid.any() else np.zeros(3)
        colors[missing] = fallback

    filled = Image.fromarray(
        np.clip(colors, 0, 255).astype(np.uint8),
    ).filter(ImageFilter.GaussianBlur(radius=2.4))
    return filled.resize(image.size, Image.Resampling.LANCZOS)


def build_layered_scene(
    image: Image.Image,
    depth: Image.Image,
) -> tuple[Image.Image, Image.Image]:
    depth_values = np.asarray(depth.convert("L"), dtype=np.uint8)
    threshold = max(
        _otsu_threshold(depth_values),
        int(np.percentile(depth_values, 70)),
    )
    hard_mask = Image.fromarray(
        np.where(depth_values >= threshold, 255, 0).astype(np.uint8),
    )
    hard_mask = hard_mask.filter(ImageFilter.MaxFilter(7)).filter(
        ImageFilter.MinFilter(5)
    )
    alpha = hard_mask.filter(ImageFilter.GaussianBlur(radius=1.25))

    expanded_mask = hard_mask.filter(ImageFilter.MaxFilter(15))
    filled_background = _nearest_background_fill(image, expanded_mask)
    background = Image.composite(filled_background, image, expanded_mask)

    foreground = image.convert("RGBA")
    foreground.putalpha(alpha)
    return background, foreground


class AssetError(ValueError):
    """A safe validation or persistence error for personal assets."""


class DepthEstimator(Protocol):
    model_name: str

    def estimate(
        self,
        image: Image.Image,
        progress: ProgressCallback,
    ) -> Image.Image: ...


class DepthAnythingV2Estimator:
    """Lazily loads a local Depth Anything V2 Small model on first use."""

    model_name = "Depth Anything V2 Small"

    def __init__(
        self,
        model_id: str = "depth-anything/Depth-Anything-V2-Small-hf",
    ) -> None:
        self.model_id = model_id
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._torch = None
        self._device = "cpu"

    def _load(self, progress: ProgressCallback) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            progress(
                24,
                "loading_model",
                "正在加载本地深度模型，首次运行会下载约 100MB 权重…",
            )
            try:
                import torch
                from transformers import (
                    AutoImageProcessor,
                    AutoModelForDepthEstimation,
                )
            except ImportError as exc:
                raise AssetError(
                    "深度模型依赖未安装，请重新安装 backend/requirements.txt。"
                ) from exc

            if torch.backends.mps.is_available():
                self._device = "mps"
            elif torch.cuda.is_available():
                self._device = "cuda"

            try:
                processor = AutoImageProcessor.from_pretrained(
                    self.model_id,
                    use_fast=True,
                )
                model = AutoModelForDepthEstimation.from_pretrained(self.model_id)
                model = model.to(self._device).eval()
            except Exception as exc:
                raise AssetError(
                    "无法加载本地深度模型，请检查网络后重试首次下载。"
                ) from exc

            self._torch = torch
            self._processor = processor
            self._model = model

    def estimate(
        self,
        image: Image.Image,
        progress: ProgressCallback,
    ) -> Image.Image:
        self._load(progress)
        assert self._model is not None
        assert self._processor is not None
        assert self._torch is not None

        torch = self._torch
        progress(42, "estimating_depth", "正在解析画面远近与主体边缘…")
        inputs = self._processor(images=image, return_tensors="pt")
        inputs = {
            key: value.to(self._device)
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            outputs = self._model(**inputs)
            prediction = torch.nn.functional.interpolate(
                outputs.predicted_depth.unsqueeze(1),
                size=(image.height, image.width),
                mode="bicubic",
                align_corners=False,
            ).squeeze()

        depth = prediction.detach().float().cpu().numpy()
        import numpy as np

        low, high = np.percentile(depth, (2, 98))
        if high - low < 1e-6:
            normalized = np.zeros_like(depth)
        else:
            normalized = np.clip((depth - low) / (high - low), 0, 1)
        depth_u8 = (normalized * 255).astype("uint8")
        return Image.fromarray(depth_u8).filter(
            ImageFilter.GaussianBlur(radius=0.35)
        )


class AssetRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.database_path.parent, 0o700)
        self._lock = threading.Lock()
        self._initialize()
        if self.database_path.exists():
            os.chmod(self.database_path, 0o600)

    def _secure_database_files(self) -> None:
        for path in self.database_path.parent.glob(
            f"{self.database_path.name}*"
        ):
            try:
                if path.is_file():
                    os.chmod(path, 0o600)
            except FileNotFoundError:
                # SQLite may remove the transient WAL/SHM file between glob and chmod.
                continue

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_database_files()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT 'local',
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            asset_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(assets)"
                ).fetchall()
            }
            if "owner_id" not in asset_columns:
                connection.execute(
                    """
                    ALTER TABLE assets
                    ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES assets(id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS assets_created_idx "
                "ON assets(created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_created_idx "
                "ON jobs(created_at DESC)"
            )
            interrupted_at = _now()
            interrupted_jobs = connection.execute(
                "SELECT asset_id FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            if interrupted_jobs:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'failed',
                        stage = 'interrupted',
                        message = '任务因本地服务重启而中断，请重新上传。',
                        error = '本地服务重启中断任务。',
                        updated_at = ?
                    WHERE status IN ('queued', 'running')
                    """,
                    (interrupted_at,),
                )
                connection.executemany(
                    """
                    UPDATE assets
                    SET status = 'failed', updated_at = ?
                    WHERE id = ?
                    """,
                    [
                        (interrupted_at, row["asset_id"])
                        for row in interrupted_jobs
                    ],
                )

    def create_asset_and_job(
        self,
        *,
        asset_id: str,
        job_id: str,
        name: str,
        metadata: dict,
        owner_id: str = "local",
        kind: str = "spatial_scene",
        queued_message: str = "任务已进入本地处理队列。",
    ) -> None:
        timestamp = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assets
                (id, owner_id, kind, name, status, metadata_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, 'processing', ?, ?, ?)
                """,
                (
                    asset_id,
                    owner_id,
                    kind,
                    name,
                    json.dumps(metadata, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs
                (id, kind, status, progress, stage, message, asset_id, error,
                 created_at, updated_at)
                VALUES (?, ?, 'queued', 0, 'queued', ?, ?, NULL, ?, ?)
                """,
                (
                    job_id,
                    kind,
                    queued_message,
                    asset_id,
                    timestamp,
                    timestamp,
                ),
            )

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        stage: str | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[object] = [_now()]
        for column, value in (
            ("status", status),
            ("progress", progress),
            ("stage", stage),
            ("message", message),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if error is not None:
            assignments.append("error = ?")
            values.append(error)
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def complete_asset(
        self,
        asset_id: str,
        metadata: dict,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE assets
                SET status = 'ready', metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(metadata, ensure_ascii=False), _now(), asset_id),
            )

    def fail_asset(self, asset_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE assets SET status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), asset_id),
            )

    def get_asset_row(
        self,
        asset_id: str,
        owner_id: str | None = None,
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            if owner_id is not None:
                return connection.execute(
                    "SELECT * FROM assets WHERE id = ? AND owner_id = ?",
                    (asset_id, owner_id),
                ).fetchone()
            return connection.execute(
                "SELECT * FROM assets WHERE id = ?",
                (asset_id,),
            ).fetchone()

    def get_job_row(
        self,
        job_id: str,
        owner_id: str | None = None,
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            if owner_id is not None:
                return connection.execute(
                    """
                    SELECT jobs.* FROM jobs
                    JOIN assets ON assets.id = jobs.asset_id
                    WHERE jobs.id = ? AND assets.owner_id = ?
                    """,
                    (job_id, owner_id),
                ).fetchone()
            return connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

    def list_asset_rows(
        self,
        limit: int = 50,
        owner_id: str | None = None,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            if owner_id is not None:
                return connection.execute(
                    """
                    SELECT * FROM assets
                    WHERE owner_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
            return connection.execute(
                "SELECT * FROM assets ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def list_job_rows(
        self,
        limit: int = 50,
        owner_id: str | None = None,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            if owner_id is not None:
                return connection.execute(
                    """
                    SELECT jobs.* FROM jobs
                    JOIN assets ON assets.id = jobs.asset_id
                    WHERE assets.owner_id = ?
                    ORDER BY jobs.created_at DESC LIMIT ?
                    """,
                    (owner_id, limit),
                ).fetchall()
            return connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def delete_asset(
        self,
        asset_id: str,
        owner_id: str | None = None,
    ) -> bool:
        with self._lock, self._connect() as connection:
            if owner_id is None:
                exists = connection.execute(
                    "SELECT 1 FROM assets WHERE id = ?",
                    (asset_id,),
                ).fetchone()
            else:
                exists = connection.execute(
                    """
                    SELECT 1 FROM assets
                    WHERE id = ? AND owner_id = ?
                    """,
                    (asset_id, owner_id),
                ).fetchone()
            if not exists:
                return False
            connection.execute("DELETE FROM jobs WHERE asset_id = ?", (asset_id,))
            connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            return True


class SpatialSceneService:
    def __init__(
        self,
        data_dir: Path,
        *,
        estimator: DepthEstimator | None = None,
        max_workers: int = 1,
    ) -> None:
        self.data_dir = data_dir
        self.asset_dir = data_dir / "assets"
        self.source_image_dir = data_dir / "source-images"
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.source_image_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        os.chmod(self.asset_dir, 0o700)
        os.chmod(self.source_image_dir, 0o700)
        self.repository = AssetRepository(data_dir / "assets.sqlite3")
        self._upgrade_existing_assets()
        self.estimator = estimator or DepthAnythingV2Estimator(
            os.getenv(
                "SPATIAL_DEPTH_MODEL",
                "depth-anything/Depth-Anything-V2-Small-hf",
            )
        )
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="spatial-scene",
        )
        self._futures: set[Future[None]] = set()
        self._future_lock = threading.Lock()
        self._cleanup_stale_source_images()

    def _cleanup_stale_source_images(self, max_age_seconds: int = 24 * 60 * 60) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        for directory in self.source_image_dir.iterdir():
            try:
                if directory.is_dir() and directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                LOGGER.warning("Unable to inspect staged source image %s", directory)

    def stage_source_image(
        self,
        image_bytes: bytes,
        *,
        original_name: str,
        owner_id: str = "local",
    ) -> SourceImagePublic:
        if not image_bytes:
            raise AssetError("请选择一张图片。")
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise AssetError("图片不能超过 20MB。")

        image = self._resize_for_output(self._decode_image(image_bytes))
        source_image_id = str(uuid4())
        directory = self.source_image_dir / source_image_id
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, 0o700)
        source_path = directory / "source.webp"
        image.save(source_path, "WEBP", quality=94, method=6)
        os.chmod(source_path, 0o600)
        safe_original_name = Path(original_name).name[:160] or "空间照片"
        metadata = {
            "id": source_image_id,
            "original_name": safe_original_name,
            "width": image.width,
            "height": image.height,
            "size_bytes": source_path.stat().st_size,
            "owner_id": owner_id,
        }
        metadata_path = directory / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(metadata_path, 0o600)
        return SourceImagePublic(**metadata)

    def get_source_image(
        self,
        source_image_id: str,
        *,
        owner_id: str | None = None,
    ) -> SourceImagePublic:
        if (
            not source_image_id
            or Path(source_image_id).name != source_image_id
            or len(source_image_id) > 100
        ):
            raise AssetError("图片附件标识无效。")
        metadata_path = self.source_image_dir / source_image_id / "metadata.json"
        source_path = self.source_image_dir / source_image_id / "source.webp"
        if not metadata_path.is_file() or not source_path.is_file():
            raise AssetError("图片附件不存在或已经过期，请重新选择。")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if owner_id is not None and metadata.get("owner_id", "local") != owner_id:
                raise AssetError("图片附件不属于当前用户。")
            return SourceImagePublic(**metadata)
        except (OSError, ValueError, TypeError) as exc:
            raise AssetError("图片附件信息无法读取，请重新选择。") from exc

    def delete_source_image(
        self,
        source_image_id: str,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.get_source_image(source_image_id, owner_id=owner_id)
        shutil.rmtree(self.source_image_dir / source_image_id, ignore_errors=True)

    def create_scene_from_source(
        self,
        source_image_id: str,
        *,
        title: str | None = None,
        owner_id: str = "local",
    ) -> SpatialSceneCreateResponse:
        source = self.get_source_image(source_image_id, owner_id=owner_id)
        source_path = self.source_image_dir / source_image_id / "source.webp"
        try:
            created = self.create_scene(
                source_path.read_bytes(),
                original_name=source.original_name,
                title=title,
                owner_id=owner_id,
            )
        except OSError as exc:
            raise AssetError("图片附件无法读取，请重新选择。") from exc
        self.delete_source_image(source_image_id, owner_id=owner_id)
        return created

    def _write_layered_assets(
        self,
        directory: Path,
        image: Image.Image,
        depth: Image.Image,
    ) -> tuple[Path, Path]:
        background, foreground = build_layered_scene(image, depth)
        background_path = directory / "background.webp"
        foreground_path = directory / "foreground.webp"
        background.save(background_path, "WEBP", quality=92, method=6)
        foreground.save(foreground_path, "WEBP", quality=94, method=6)
        os.chmod(background_path, 0o600)
        os.chmod(foreground_path, 0o600)
        return background_path, foreground_path

    def _upgrade_existing_assets(self) -> None:
        for row in self.repository.list_asset_rows(limit=500):
            if row["status"] != "ready":
                continue
            metadata = json.loads(row["metadata_json"])
            if metadata.get("background_file") and metadata.get(
                "foreground_file"
            ):
                continue
            directory = self.asset_dir / row["id"]
            source_path = directory / str(metadata.get("source_file", ""))
            depth_path = directory / str(metadata.get("depth_file", ""))
            if not source_path.is_file() or not depth_path.is_file():
                continue
            try:
                with Image.open(source_path) as source:
                    image = source.convert("RGB")
                with Image.open(depth_path) as depth_source:
                    depth = depth_source.convert("L")
                background_path, foreground_path = self._write_layered_assets(
                    directory,
                    image,
                    depth,
                )
                metadata.update(
                    {
                        "background_file": background_path.name,
                        "foreground_file": foreground_path.name,
                    }
                )
                manifest_path = directory / str(
                    metadata.get("manifest_file", "scene.json")
                )
                manifest = {
                    "version": 2,
                    "representation": "layered-depth-image",
                    "width": image.width,
                    "height": image.height,
                    "image": source_path.name,
                    "depth": depth_path.name,
                    "background": background_path.name,
                    "foreground": foreground_path.name,
                    "near_is_white": True,
                    "recommended_strength": 0.26,
                    "model": metadata.get("model_name"),
                }
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o600)
                metadata["manifest_file"] = manifest_path.name
                self.repository.complete_asset(row["id"], metadata)
            except Exception:
                LOGGER.exception(
                    "Unable to upgrade spatial asset %s to layered depth",
                    row["id"],
                )

    def create_scene(
        self,
        image_bytes: bytes,
        *,
        original_name: str,
        title: str | None = None,
        owner_id: str = "local",
    ) -> SpatialSceneCreateResponse:
        if not image_bytes:
            raise AssetError("请选择一张图片。")
        if len(image_bytes) > MAX_UPLOAD_BYTES:
            raise AssetError("图片不能超过 20MB。")

        image = self._decode_image(image_bytes)
        asset_id = str(uuid4())
        job_id = str(uuid4())
        directory = self.asset_dir / asset_id
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, 0o700)
        source_path = directory / "source.webp"

        image = self._resize_for_output(image)
        image.save(source_path, "WEBP", quality=94, method=6)
        os.chmod(source_path, 0o600)
        fallback_title = Path(original_name).stem or "空间照片"
        metadata = {
            "width": image.width,
            "height": image.height,
            "source_file": source_path.name,
            "preview_file": source_path.name,
            "depth_file": None,
            "background_file": None,
            "foreground_file": None,
            "manifest_file": None,
            "model_name": None,
        }
        self.repository.create_asset_and_job(
            asset_id=asset_id,
            job_id=job_id,
            name=_safe_title(title, fallback_title),
            metadata=metadata,
            owner_id=owner_id,
        )
        future = self.executor.submit(self._process, asset_id, job_id)
        with self._future_lock:
            self._futures.add(future)
        future.add_done_callback(self._forget_future)
        return SpatialSceneCreateResponse(
            asset=self.get_asset(asset_id),
            job=self.get_job(job_id),
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
            progress(8, "preparing", "正在准备图片并移除原始元数据…")
            directory = self.asset_dir / asset_id
            source_path = directory / "source.webp"
            with Image.open(source_path) as source:
                image = source.convert("RGB")
            depth = self.estimator.estimate(image, progress)

            progress(76, "layering", "正在分离前景并补全遮挡背景…")
            depth_path = directory / "depth.png"
            depth.save(depth_path, "PNG", optimize=True)
            os.chmod(depth_path, 0o600)
            background_path, foreground_path = self._write_layered_assets(
                directory,
                image,
                depth,
            )
            progress(90, "packaging", "正在打包低功耗空间场景…")
            manifest_path = directory / "scene.json"
            manifest = {
                "version": 2,
                "representation": "layered-depth-image",
                "width": image.width,
                "height": image.height,
                "image": "source.webp",
                "depth": "depth.png",
                "background": background_path.name,
                "foreground": foreground_path.name,
                "near_is_white": True,
                "recommended_strength": 0.26,
                "model": self.estimator.model_name,
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(manifest_path, 0o600)

            row = self.repository.get_asset_row(asset_id)
            if row is None:
                return
            metadata = json.loads(row["metadata_json"])
            metadata.update(
                {
                    "depth_file": depth_path.name,
                    "background_file": background_path.name,
                    "foreground_file": foreground_path.name,
                    "manifest_file": manifest_path.name,
                    "model_name": self.estimator.model_name,
                }
            )
            self.repository.complete_asset(asset_id, metadata)
            self.repository.update_job(
                job_id,
                status="completed",
                progress=100,
                stage="completed",
                message="空间场景已生成，可以拖动视角查看。",
            )
        except Exception as exc:
            LOGGER.exception("Spatial scene job failed: %s", job_id)
            message = (
                str(exc)
                if isinstance(exc, AssetError)
                else "空间场景生成失败，请换一张图片后重试。"
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

    @staticmethod
    def _decode_image(image_bytes: bytes) -> Image.Image:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        try:
            with Image.open(BytesIO(image_bytes)) as candidate:
                if candidate.format not in ALLOWED_IMAGE_FORMATS:
                    raise AssetError("目前只支持 JPG、PNG 和 WebP 图片。")
                candidate.verify()
            with Image.open(BytesIO(image_bytes)) as candidate:
                image = ImageOps.exif_transpose(candidate).convert("RGB")
                image.load()
                return image
        except AssetError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
        ) as exc:
            raise AssetError("图片无法读取或尺寸过大。") from exc

    @staticmethod
    def _resize_for_output(image: Image.Image) -> Image.Image:
        if max(image.size) <= MAX_OUTPUT_EDGE:
            return image
        resized = image.copy()
        resized.thumbnail(
            (MAX_OUTPUT_EDGE, MAX_OUTPUT_EDGE),
            Image.Resampling.LANCZOS,
        )
        return resized

    def _asset_from_row(self, row: sqlite3.Row) -> AssetPublic:
        metadata = json.loads(row["metadata_json"])
        base = f"/api/assets/{row['id']}/files"

        def url_for(key: str) -> str | None:
            filename = metadata.get(key)
            return f"{base}/{filename}" if filename else None

        return AssetPublic(
            id=row["id"],
            kind=row["kind"],
            name=row["name"],
            status=row["status"],
            width=metadata.get("width"),
            height=metadata.get("height"),
            source_url=url_for("source_file") or "",
            preview_url=url_for("preview_file"),
            depth_url=url_for("depth_file"),
            background_url=url_for("background_file"),
            foreground_url=url_for("foreground_file"),
            manifest_url=url_for("manifest_file"),
            result_url=url_for("result_file"),
            style_reference_urls=[
                f"{base}/{filename}"
                for filename in metadata.get("style_files", [])
                if isinstance(filename, str)
            ],
            model_name=metadata.get("model_name"),
            provider_name=metadata.get("provider_name"),
            parameters=metadata.get("parameters", {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobPublic:
        return JobPublic(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            progress=row["progress"],
            stage=row["stage"],
            message=row["message"],
            asset_id=row["asset_id"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_asset(
        self,
        asset_id: str,
        *,
        owner_id: str | None = None,
    ) -> AssetPublic:
        row = self.repository.get_asset_row(asset_id, owner_id)
        if row is None:
            raise AssetError("找不到这个个人资产。")
        return self._asset_from_row(row)

    def get_job(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
    ) -> JobPublic:
        row = self.repository.get_job_row(job_id, owner_id)
        if row is None:
            raise AssetError("找不到这个任务。")
        return self._job_from_row(row)

    def list_assets(
        self,
        limit: int = 50,
        *,
        owner_id: str | None = None,
    ) -> list[AssetPublic]:
        return [
            self._asset_from_row(row)
            for row in self.repository.list_asset_rows(limit, owner_id)
        ]

    def list_jobs(
        self,
        limit: int = 50,
        *,
        owner_id: str | None = None,
    ) -> list[JobPublic]:
        return [
            self._job_from_row(row)
            for row in self.repository.list_job_rows(limit, owner_id)
        ]

    def resolve_asset_file(self, asset_id: str, filename: str) -> Path:
        row = self.repository.get_asset_row(asset_id)
        if row is None:
            raise AssetError("找不到这个个人资产。")
        metadata = json.loads(row["metadata_json"])
        allowed = {
            value
            for key, value in metadata.items()
            if key.endswith("_file") and isinstance(value, str)
        }
        allowed.update(
            filename
            for key, value in metadata.items()
            if key.endswith("_files") and isinstance(value, list)
            for filename in value
            if isinstance(filename, str)
        )
        if filename not in allowed or Path(filename).name != filename:
            raise AssetError("找不到这个资产文件。")
        path = self.asset_dir / asset_id / filename
        if not path.is_file():
            raise AssetError("资产文件尚未生成。")
        return path

    def delete_asset(
        self,
        asset_id: str,
        *,
        owner_id: str | None = None,
    ) -> None:
        if Path(asset_id).name != asset_id:
            raise AssetError("资产标识无效。")
        if not self.repository.delete_asset(asset_id, owner_id):
            raise AssetError("找不到这个个人资产。")
        shutil.rmtree(self.asset_dir / asset_id, ignore_errors=True)

    def wait_for_idle(self, timeout: float = 10) -> None:
        with self._future_lock:
            futures = list(self._futures)
        if futures:
            wait(futures, timeout=timeout)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


def register_asset_tools(
    registry: ToolRegistry,
    service: SpatialSceneService,
) -> None:
    def list_personal_assets(_: dict) -> dict:
        from .tools import current_tool_context

        assets = service.list_assets(
            limit=20,
            owner_id=current_tool_context().owner_id,
        )
        return {
            "assets": [
                {
                    "id": asset.id,
                    "name": asset.name,
                    "kind": asset.kind,
                    "status": asset.status,
                    "created_at": asset.created_at.isoformat(),
                }
                for asset in assets
            ]
        }

    def get_job_status(arguments: dict) -> dict:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ToolError("get_job_status requires a job_id")
        try:
            job = service.get_job(job_id.strip())
        except AssetError as exc:
            raise ToolError(str(exc)) from exc
        return job.model_dump(mode="json")

    def create_spatial_scene(arguments: dict) -> dict:
        from .tools import current_tool_context

        source_image_id = arguments.get("source_image_id")
        if not isinstance(source_image_id, str) or not source_image_id.strip():
            raise ToolError("create_spatial_scene requires a source_image_id")
        title = arguments.get("title")
        if title is not None and not isinstance(title, str):
            raise ToolError("title must be a string")
        try:
            created = service.create_scene_from_source(
                source_image_id.strip(),
                title=title.strip() if isinstance(title, str) else None,
                owner_id=current_tool_context().owner_id,
            )
        except AssetError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "asset_id": created.asset.id,
            "job_id": created.job.id,
            "status": created.job.status,
            "progress": created.job.progress,
            "kind": created.asset.kind,
            "message": "空间照片任务已在本机创建，正在进行深度估计。",
        }

    registry.register(
        ToolSpec(
            "list_personal_assets",
            "列出用户本机已经生成的空间照片等个人数字资产",
            {"type": "object", "properties": {}, "additionalProperties": False},
            list_personal_assets,
        )
    )
    registry.register(
        ToolSpec(
            "create_spatial_scene",
            (
                "把用户已经附加到本机的 2D 图片生成可拖动视角的空间照片。"
                "仅在上下文提供 source_image_id 且用户明确要求生成时调用"
            ),
            {
                "type": "object",
                "properties": {
                    "source_image_id": {
                        "type": "string",
                        "description": "系统提供的本地图片附件 ID，必须原样传入",
                    },
                    "title": {
                        "type": "string",
                        "description": "空间照片名称；用户未指定时可省略",
                        "maxLength": 80,
                    },
                },
                "required": ["source_image_id"],
                "additionalProperties": False,
            },
            create_spatial_scene,
            risk_level="local_write",
            idempotent=False,
        )
    )
    registry.register(
        ToolSpec(
            "get_job_status",
            "查询异步视觉任务的进度和状态",
            {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "异步任务 ID",
                    }
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            get_job_status,
        )
    )
