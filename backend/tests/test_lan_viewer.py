from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import time
import pytest
from PIL import Image

from backend.app.lan_viewer import (
    LanViewerService,
    ViewerLinkError,
    _lan_candidate_rank,
)
from backend.tests.test_api import build_test_spatial


def _png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (180, 120), "#6f8da8").save(buffer, "PNG")
    return buffer.getvalue()


def test_signed_lan_viewer_token_and_mobile_page(tmp_path: Path) -> None:
    spatial = build_test_spatial(tmp_path)
    created = spatial.create_scene(_png(), original_name="scene.png")
    spatial.wait_for_idle()
    service = LanViewerService(
        spatial,
        secret_path=tmp_path / "viewer-secret.key",
        bind_host="127.0.0.1",
        port=0,
        ttl_seconds=600,
    )
    try:
        asset = spatial.get_asset(created.asset.id)
        token = service._issue_token(asset.id)
        page = service._viewer_html(token, asset)
        assert "拖动或轻微转动手机" in page
        assert "deviceorientation" in page
        assert service.verify_token(token) == created.asset.id
        with pytest.raises(ViewerLinkError):
            service.verify_token(token[:-1] + ("A" if token[-1] != "A" else "B"))
    finally:
        service.close()
        spatial.close()


def test_lan_candidate_prefers_physical_private_network_over_vpn() -> None:
    candidates = [
        ("utun5", "10.8.0.3"),
        ("en10", "192.168.0.100"),
        ("bridge0", "172.20.0.1"),
    ]

    selected = min(candidates, key=_lan_candidate_rank)

    assert selected == ("en10", "192.168.0.100")


def test_runtime_public_url_overrides_lan_address(tmp_path: Path) -> None:
    spatial = build_test_spatial(tmp_path)
    created = spatial.create_scene(_png(), original_name="scene.png")
    spatial.wait_for_idle()
    public_path = tmp_path / "viewer-public-url.json"
    public_path.write_text(
        json.dumps(
            {
                "url": "https://example.trycloudflare.com",
                "created_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    service = LanViewerService(
        spatial,
        secret_path=tmp_path / "viewer-secret.key",
        bind_host="127.0.0.1",
        port=0,
        ttl_seconds=600,
        runtime_public_base_path=public_path,
    )
    try:
        service.start = lambda: None  # type: ignore[method-assign]
        link = service.create_link(created.asset.id)
        assert link.startswith("https://example.trycloudflare.com/v/")
    finally:
        service.close()
        spatial.close()
