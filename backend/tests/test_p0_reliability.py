from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from backend.app.assets import AssetRepository, SpatialSceneService
from backend.app.observability import MetricsRegistry


class FakeDepthEstimator:
    model_name = "Test Depth"

    def estimate(self, image: Image.Image, progress):
        progress(55, "estimating_depth", "测试深度估计")
        return Image.linear_gradient("L").resize(image.size)


class FakeForegroundSegmenter:
    def segment(self, image: Image.Image, depth: Image.Image, progress):
        from backend.app.spatial_segmentation import ForegroundMaskResult

        del depth
        progress(68, "segmenting", "测试主体分割")
        return ForegroundMaskResult(
            mask=Image.new("L", image.size, 255),
            model_name="Test Semantic Mask",
            quality_score=0.95,
        )


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (96, 64), "#7fae98").save(buffer, "PNG")
    return buffer.getvalue()


def test_asset_job_reservation_and_claim_are_atomic(tmp_path: Path) -> None:
    repository_a = AssetRepository(tmp_path / "assets.sqlite3")
    repository_b = AssetRepository(tmp_path / "assets.sqlite3")
    metadata = {"source_file": "source.webp", "idempotency_fingerprint": "fp-1"}

    assert repository_a.create_asset_and_job(
        asset_id="asset-1",
        job_id="job-1",
        name="scene",
        metadata=metadata,
        expected_fingerprint="fp-1",
    )
    assert not repository_b.create_asset_and_job(
        asset_id="asset-1",
        job_id="job-1",
        name="scene",
        metadata=metadata,
        expected_fingerprint="fp-1",
    )
    assert repository_a.claim_job("job-1", "worker-a")
    assert not repository_b.claim_job("job-1", "worker-b")
    assert repository_a.get_job_row("job-1")["attempt_count"] == 1


def test_spatial_idempotency_survives_two_service_instances(tmp_path: Path) -> None:
    data_dir = tmp_path / "assets"
    service_a = SpatialSceneService(
        data_dir,
        estimator=FakeDepthEstimator(),
        segmenter=FakeForegroundSegmenter(),
    )
    service_b = SpatialSceneService(
        data_dir,
        estimator=FakeDepthEstimator(),
        segmenter=FakeForegroundSegmenter(),
    )
    try:
        first = service_a.create_scene(
            _png(),
            original_name="cat.png",
            owner_id="owner-1",
            idempotency_key="feishu:chat:sender:message",
        )
        second = service_b.create_scene(
            _png(),
            original_name="cat.png",
            owner_id="owner-1",
            idempotency_key="feishu:chat:sender:message",
        )
        assert first.asset.id == second.asset.id
        assert first.job.id == second.job.id
        assert len(service_a.repository.list_asset_rows()) == 1
        service_a.wait_for_idle()
    finally:
        service_a.close()
        service_b.close()


def test_metrics_registry_exposes_aggregate_values() -> None:
    metrics = MetricsRegistry()
    metrics.increment("http_requests_total")
    metrics.observe_ms("http_request_duration", 10.5)
    metrics.observe_ms("http_request_duration", 20.5)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["http_requests_total"] == 1
    assert snapshot["durations"]["http_request_duration"]["count"] == 2
    assert snapshot["durations"]["http_request_duration"]["avg_ms"] == 15.5
