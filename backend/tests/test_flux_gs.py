from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.flux_gs import (
    FluxGSError,
    FluxGSHttpProvider,
    FluxGSParameters,
    FluxGSService,
    LocalFluxGSPreviewProvider,
    register_flux_gs_tools,
)
from backend.app.main import create_app
from backend.app.tools import ToolExecutionContext, ToolRegistry
from backend.tests.test_api import build_test_settings, build_test_spatial


def _colmap_dataset(root: Path, name: str = "scene-a") -> Path:
    dataset = root / name
    images = dataset / "images"
    sparse = dataset / "sparse" / "0"
    images.mkdir(parents=True)
    sparse.mkdir(parents=True)
    for index in range(1, 9):
        (images / f"{index:03d}.png").write_bytes(b"png")
    (sparse / "cameras.txt").write_text("# camera\n", encoding="utf-8")
    (sparse / "images.txt").write_text("# images\n", encoding="utf-8")
    (sparse / "points3D.txt").write_text("# points\n", encoding="utf-8")
    return dataset


def test_flux_gs_tools_validate_and_create_preview_job(tmp_path: Path) -> None:
    _colmap_dataset(tmp_path)
    registry = ToolRegistry()
    service = FluxGSService(
        LocalFluxGSPreviewProvider(),
        dataset_root=tmp_path,
        provider_dataset_root=tmp_path,
    )
    register_flux_gs_tools(registry, service)

    registry.validate_call("create_flux_gs_demo", {"dataset_id": "scene-a"})
    validation = registry.execute(
        "validate_flux_gs_dataset",
        {"dataset_id": "scene-a"},
        context=ToolExecutionContext(owner_id="user-a"),
    )
    created = registry.execute(
        "create_flux_gs_demo",
        {
            "dataset_id": "scene-a",
            "scene_name": "demo_scene",
            "iterations": 4000,
            "training_profile": "quick",
        },
        context=ToolExecutionContext(
            owner_id="user-a",
            idempotency_key="idem-demo-scene",
        ),
    )

    assert validation["valid"] is True
    assert validation["dataset_id"] == "scene-a"
    assert "dataset_path" not in validation
    assert created["kind"] == "flux_gs_demo"
    assert created["job_id"] == "idem-demo-scene"
    assert created["status"] == "completed"
    capability = next(
        item for item in registry.list_capabilities() if item.id == "flux-gs-demo"
    )
    assert capability.author == "Zuheng Zhao"


def test_flux_gs_rejects_filesystem_paths_from_agent(tmp_path: Path) -> None:
    service = FluxGSService(
        LocalFluxGSPreviewProvider(),
        dataset_root=tmp_path,
        provider_dataset_root=tmp_path,
    )

    for unsafe in ("../secret", "/etc", "nested/scene", "C:\\data\\scene"):
        with pytest.raises(FluxGSError):
            service.validate_dataset(unsafe, owner_id="user-a")


def test_flux_gs_http_provider_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "personal-agent"
        assert request.headers["X-Owner-ID"] == "user-a"
        if request.url.path.endswith("/datasets/validate"):
            assert json.loads(request.content) == {"dataset_path": "/srv/scene-a"}
            return httpx.Response(200, json={"valid": True, "errors": []})
        if request.url.path.endswith("/jobs") and request.method == "POST":
            assert request.headers["Idempotency-Key"] == "idem-1"
            payload = json.loads(request.content)
            assert payload["dataset_path"] == "/srv/scene-a"
            return httpx.Response(
                202,
                json={
                    "job_id": "job-1",
                    "asset_id": "asset-1",
                    "status": "queued",
                    "progress": 0,
                    "scene_name": "demo",
                    "demo_url": None,
                },
            )
        if request.url.path.endswith("/jobs/job-1"):
            return httpx.Response(
                200,
                json={
                    "job_id": "job-1",
                    "status": "completed",
                    "progress": 100,
                    "demo_url": "https://example.com/render_demo/",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = FluxGSHttpProvider("http://flux-gs.local", client=client)
    try:
        assert provider.validate_dataset("/srv/scene-a", owner_id="user-a")["valid"]
        created = provider.create_job(
            FluxGSParameters(
                dataset_path="/srv/scene-a",
                scene_name="demo",
                iterations=4000,
                training_profile="quick",
            ),
            owner_id="user-a",
            idempotency_key="idem-1",
        )
        assert created["job_id"] == "job-1"
        assert provider.get_job("job-1", owner_id="user-a")["demo_url"].startswith(
            "https://"
        )
    finally:
        client.close()


def test_flux_gs_api_is_local_root_only(tmp_path: Path) -> None:
    dataset_root = tmp_path / "datasets"
    _colmap_dataset(dataset_root)
    service = FluxGSService(
        LocalFluxGSPreviewProvider(),
        dataset_root=dataset_root,
        provider_dataset_root=dataset_root,
    )
    settings_service, _ = build_test_settings(tmp_path)
    app = create_app(
        trace_path=tmp_path / "runs.jsonl",
        settings_service=settings_service,
        spatial_service=build_test_spatial(tmp_path),
        flux_gs_service=service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/flux-gs/datasets/validate",
            json={"dataset_id": "scene-a"},
        )
        assert response.status_code == 200
        assert response.json()["valid"] is True

    with TestClient(app, client=("198.51.100.10", 50000)) as remote:
        response = remote.get("/api/flux-gs/provider")
        assert response.status_code == 403
