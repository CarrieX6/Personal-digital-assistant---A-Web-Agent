from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from backend.app.main import create_app
from backend.app.style_transfer import LocalColorStyleProvider, PhotoStyleService
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
                "source_image_id": content.id,
                "style_image_ids": [reference.id],
            },
        )
        assert response.status_code == 200
        tool_output = next(
            step["output"]
            for step in response.json()["steps"]
            if step["label"] == "调用 create_photo_style_transfer"
        )
        assert tool_output["kind"] == "photo_style_transfer"
        assert not (spatial.source_image_dir / content.id).exists()
        assert not (spatial.source_image_dir / reference.id).exists()
    finally:
        style.close()
        spatial.close()
