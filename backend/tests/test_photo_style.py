from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
from PIL import Image

from backend.app.main import create_app
from backend.app.style_transfer import (
    LocalColorStyleProvider,
    PhotoStyleService,
    PicStyleHttpProvider,
    build_style_provider_from_env,
    StyleParameters,
)
from backend.tests.test_api import build_test_settings, build_test_spatial


def _png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color).save(buffer, "PNG")
    return buffer.getvalue()


def test_photo_style_api_manifest_and_agent_tool(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
            style_service=style,
        )
    )
    try:
        capabilities = {item["id"]: item for item in client.get("/api/capabilities").json()}
        assert capabilities["photo-style-transfer"]["entrypoint"] == (
            "create_photo_style_transfer"
        )

        created = client.post(
            "/api/photo-style-transfers",
            files=[
                ("content_file", ("content.png", _png("#b88d72"), "image/png")),
                ("style_files", ("style.png", _png("#315a84"), "image/png")),
            ],
        )
        assert created.status_code == 202
        style.wait_for_idle()
        direct_asset = client.get(
            f"/api/assets/{created.json()['asset']['id']}"
        ).json()
        assert direct_asset["status"] == "ready"
        assert direct_asset["result_url"]

        content = spatial.stage_source_image(
            _png("#b88d72"), original_name="content.png"
        )
        reference = spatial.stage_source_image(
            _png("#315a84"), original_name="reference.png"
        )
        response = client.post(
            "/api/agent/run",
            json={
                "message": "把内容图按参考图进行图片风格化",
                "session_id": "style-test",
                "attachment_image_ids": [content.id, reference.id],
            },
        )
        assert response.status_code == 200
        tool_output = next(
            step["output"]
            for step in response.json()["steps"]
            if step["label"] == "调用 create_photo_style_transfer"
        )
        assert tool_output["kind"] == "photo_style_transfer"
        messages = client.get(
            "/api/conversations/style-test/messages"
        ).json()["messages"]
        user_metadata = next(
            item["metadata"] for item in messages if item["role"] == "user"
        )
        assert [item["name"] for item in user_metadata["attachments"]] == [
            "content.png",
            "reference.png",
        ]
        assert "attachment" not in user_metadata
        assert "style_attachments" not in user_metadata
        content_preview = user_metadata["attachments"][0]["preview_url"]
        style_preview = user_metadata["attachments"][1]["preview_url"]
        assert content_preview == (
            f"/api/assets/{tool_output['asset_id']}/files/source.webp"
        )
        assert style_preview == (
            f"/api/assets/{tool_output['asset_id']}/files/style-1.webp"
        )
        assert client.get(content_preview).status_code == 200
        assert client.get(style_preview).status_code == 200
        assert not (spatial.source_image_dir / content.id).exists()
        assert not (spatial.source_image_dir / reference.id).exists()
    finally:
        style.close()
        spatial.close()


def test_generic_attachment_router_requests_missing_style_reference(
    tmp_path: Path,
) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
            style_service=style,
        )
    )
    try:
        content = spatial.stage_source_image(
            _png("#b88d72"),
            original_name="only-content.png",
        )

        response = client.post(
            "/api/agent/run",
            json={
                "message": "把这张图片进行风格化",
                "session_id": "style-clarification",
                "attachment_image_ids": [content.id],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "deterministic-attachment-router"
        assert "至少 1 张参考图" in body["answer"]
        assert body["steps"][0]["output"]["attachment_action"] == (
            "clarification_required"
        )
        assert not (spatial.source_image_dir / content.id).exists()
        messages = client.get(
            "/api/conversations/style-clarification/messages"
        ).json()["messages"]
        user_metadata = next(
            item["metadata"] for item in messages if item["role"] == "user"
        )
        assert user_metadata["attachments"][0]["name"] == "only-content.png"
    finally:
        style.close()
        spatial.close()


def test_generic_attachment_router_clarifies_spatial_image_choice(
    tmp_path: Path,
) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
            style_service=style,
        )
    )
    try:
        first = spatial.stage_source_image(
            _png("#c69a73"), original_name="first.png"
        )
        second = spatial.stage_source_image(
            _png("#527aa3"), original_name="second.png"
        )

        response = client.post(
            "/api/agent/run",
            json={
                "message": "生成空间照片",
                "session_id": "spatial-clarification",
                "attachment_image_ids": [first.id, second.id],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "deterministic-attachment-router"
        assert "请说明使用第几张图片" in body["answer"]
        output = body["steps"][0]["output"]
        assert output["attachment_action"] == "clarification_required"
        assert output["release_image_ids"] == [first.id, second.id]
        assert not (spatial.source_image_dir / first.id).exists()
        assert not (spatial.source_image_dir / second.id).exists()
    finally:
        style.close()
        spatial.close()


def test_pic_style_http_provider_reports_real_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/ready"
        assert request.headers["X-Tenant-ID"] == "personal-agent"
        return httpx.Response(200, json={"status": "ready"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = PicStyleHttpProvider(
        "http://style-service.local",
        client=client,
    )
    try:
        status = provider.status()
        assert status["ready"] is True
        assert status["upstream_status"] == {"status": "ready"}
    finally:
        client.close()


def test_pic_style_http_is_product_default(monkeypatch) -> None:
    monkeypatch.delenv("PHOTO_STYLE_PROVIDER", raising=False)
    monkeypatch.delenv("PHOTO_STYLE_SERVICE_URL", raising=False)

    provider = build_style_provider_from_env()

    assert isinstance(provider, PicStyleHttpProvider)
    assert provider.base_url == "http://127.0.0.1:18000"
    provider.close()


def test_pic_style_http_provider_contract() -> None:
    upload_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_count
        if request.method == "POST" and request.url.path == "/v1/assets":
            upload_count += 1
            return httpx.Response(200, json={"asset_id": f"asset-{upload_count}"})
        if (
            request.method == "POST"
            and request.url.path == "/v1/skills/photo-style-transfer/jobs"
        ):
            assert request.headers["Idempotency-Key"]
            payload = json.loads(request.content)
            assert payload["content_image"] == {"asset_id": "asset-1"}
            assert payload["style_images"] == [
                {"image": {"asset_id": "asset-2"}}
            ]
            return httpx.Response(202, json={"job_id": "job-1"})
        if (
            request.method == "GET"
            and request.url.path == "/v1/skills/photo-style-transfer/jobs/job-1"
        ):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "progress": 100,
                    "result": {
                        "provider": "sdxl_ip_adapter_8gb_v1",
                        "provider_version": "1",
                        "outputs": [{"url": "/v1/assets/result-1"}],
                    },
                },
            )
        if request.method == "GET" and request.url.path == "/v1/assets/result-1":
            return httpx.Response(200, content=_png("#7a5c91"))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = PicStyleHttpProvider(
        "http://style-service.local",
        client=client,
        timeout_seconds=5,
    )
    try:
        result = provider.stylize(
            Image.new("RGB", (32, 32), "#304050"),
            [Image.new("RGB", (32, 32), "#998877")],
            parameters=StyleParameters(seed=1701),
            progress=lambda *_args: None,
        )
        assert result.model_name == "sdxl_ip_adapter_8gb_v1"
        assert result.metadata["production_quality"] is True
        assert result.image.size == (128, 96)
    finally:
        client.close()
