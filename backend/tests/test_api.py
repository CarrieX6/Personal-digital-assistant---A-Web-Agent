from __future__ import annotations

import json
import os
import stat
from io import BytesIO
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.assets import SpatialSceneService
from backend.app.llm import OpenAICompatiblePlanner
from backend.app.main import create_app
from backend.app.settings import (
    EncryptedFileSecretStore,
    SettingsRepository,
    SettingsService,
    StoredLLMSettings,
)
from backend.app.style_transfer import (
    LocalColorStyleProvider,
    PhotoStyleService,
    PicStyleHttpProvider,
    StyleParameters,
)


def assert_private_file_permissions(path: Path) -> None:
    """Require owner-only mode bits where the platform exposes POSIX modes."""
    assert path.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class MemorySecretStore:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self) -> None:
        self.value = None


class FakeDepthEstimator:
    model_name = "Test Depth"

    def estimate(self, image: Image.Image, progress) -> Image.Image:
        progress(55, "estimating_depth", "测试深度估计")
        return Image.linear_gradient("L").resize(image.size)


def build_test_spatial(tmp_path: Path) -> SpatialSceneService:
    return SpatialSceneService(
        tmp_path / "personal-assets",
        estimator=FakeDepthEstimator(),
    )


def build_test_settings(
    tmp_path: Path,
    http_client: httpx.Client | None = None,
) -> tuple[SettingsService, MemorySecretStore]:
    repository = SettingsRepository(tmp_path / "settings.json")
    repository.save(
        StoredLLMSettings(
            enabled=False,
            provider_id="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            timeout_seconds=30,
        )
    )
    secrets = MemorySecretStore()
    return SettingsService(repository, secrets, http_client), secrets


def test_health_and_tools(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    health = client.get("/health")
    tools = client.get("/api/tools")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["llm_configured"] is False
    assert health.json()["tool_count"] == 8
    assert tools.status_code == 200
    assert {tool["name"] for tool in tools.json()} >= {
        "text_stats",
        "extract_keywords",
        "current_time",
        "list_personal_assets",
        "create_spatial_scene",
        "create_photo_style_transfer",
        "get_job_status",
    }
    capabilities = client.get("/api/capabilities")
    assert capabilities.status_code == 200
    manifests = {item["id"]: item for item in capabilities.json()}
    assert manifests["photo-style-transfer"]["author"] == "Ma Xianggang"
    assert manifests["photo-style-transfer"]["entrypoint"] == (
        "create_photo_style_transfer"
    )
    assert "9.84 GiB" in manifests["photo-style-transfer"]["requirements"][
        "downloads"
    ]
    provider_status = client.get("/api/photo-style-transfers/provider")
    assert provider_status.status_code == 200
    assert provider_status.json()["name"] == "local-preview"
    assert provider_status.json()["ready"] is True


def test_analysis_runs_multiple_tools_and_writes_trace(tmp_path: Path) -> None:
    trace_path = tmp_path / "runs.jsonl"
    settings, _ = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            trace_path,
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    response = client.post(
        "/api/agent/run",
        json={"message": "分析这段文字：医学人工智能需要可靠评测。"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert [step["stage"] for step in body["steps"]] == [
        "planning",
        "tool",
        "tool",
        "final",
    ]
    assert "文本统计" in body["answer"]
    assert "演示关键词" in body["answer"]
    assert trace_path.exists()
    assert body["run_id"] in trace_path.read_text(encoding="utf-8")


def test_unknown_task_returns_guidance(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )
    response = client.post("/api/agent/run", json={"message": "你好"})

    assert response.status_code == 200
    assert "当前是无模型演示模式" in response.json()["answer"]


def test_real_llm_tool_calling_round_trip(tmp_path: Path) -> None:
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        request_bodies.append(body)
        if len(request_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_test_1",
                                        "type": "function",
                                        "function": {
                                            "name": "text_stats",
                                            "arguments": '{"text":"医学人工智能"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "这段文本包含 6 个中文字符。",
                        }
                    }
                ]
            },
        )

    model_client = httpx.Client(transport=httpx.MockTransport(handler))
    planner = OpenAICompatiblePlanner(
        api_key="test-key",
        base_url="https://model.example/v1",
        model="test-tool-model",
        client=model_client,
    )
    settings, _ = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            planner=planner,
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    response = client.post(
        "/api/agent/run",
        json={"message": "统计医学人工智能的字符数"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "llm:test-tool-model"
    assert body["answer"] == "这段文本包含 6 个中文字符。"
    assert [step["stage"] for step in body["steps"]] == [
        "planning",
        "tool",
        "final",
    ]
    assert len(request_bodies) == 2
    assert request_bodies[0]["tool_choice"] == "auto"
    assert request_bodies[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert request_bodies[1]["messages"][-1]["role"] == "tool"
    assert request_bodies[1]["messages"][-1]["tool_call_id"] == "call_test_1"


def test_provider_catalog_and_ui_settings_persistence(tmp_path: Path) -> None:
    settings, secrets = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    catalog_response = client.get("/api/settings/providers")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    providers = {provider["id"]: provider for provider in catalog["providers"]}
    assert providers["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert providers["openai"]["base_url"] == "https://api.openai.com/v1"
    assert providers["openai"]["default_model"] == "gpt-5.6"
    assert providers["openai"]["models"] == [
        "gpt-5.6",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert (
        providers["qwen"]["base_url"]
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert providers["glm"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"

    response = client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "provider_id": "qwen",
            "base_url": providers["qwen"]["base_url"],
            "model": providers["qwen"]["default_model"],
            "api_key": "sk-private-example",
            "timeout_seconds": 45,
        },
    )

    assert response.status_code == 200
    public = response.json()
    assert public["provider_id"] == "qwen"
    assert public["has_api_key"] is True
    assert public["masked_api_key"].endswith("mple")
    assert "sk-private-example" not in response.text
    assert secrets.value == "sk-private-example"

    settings_text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-private-example" not in settings_text
    assert json.loads(settings_text)["model"] == "qwen3.7-plus"
    assert_private_file_permissions(tmp_path / "settings.json")

    health = client.get("/health").json()
    assert health["llm_configured"] is True
    assert health["model"] == "qwen3.7-plus"

    wrong_provider_without_key = client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "provider_id": "glm",
            "base_url": providers["glm"]["base_url"],
            "model": providers["glm"]["default_model"],
            "timeout_seconds": 30,
        },
    )
    assert wrong_provider_without_key.status_code == 422
    assert "该供应商" in wrong_provider_without_key.json()["detail"]

    cleared = client.delete("/api/settings/llm/api-key")
    assert cleared.status_code == 200
    assert cleared.json()["has_api_key"] is False
    assert cleared.json()["enabled"] is False
    assert secrets.value is None
    assert client.get("/health").json()["llm_configured"] is False


def test_connection_check_uses_draft_without_saving_key(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_time",
                                    "type": "function",
                                    "function": {
                                        "name": "current_time",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    model_client = httpx.Client(transport=httpx.MockTransport(handler))
    settings, secrets = build_test_settings(tmp_path, model_client)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    response = client.post(
        "/api/settings/llm/test",
        json={
            "enabled": True,
            "provider_id": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key": "temporary-test-key",
            "timeout_seconds": 30,
        },
    )

    assert response.status_code == 200
    assert response.json()["selected_tools"] == ["current_time"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer temporary-test-key"
    request_body = json.loads(requests[0].content)
    assert request_body["tool_choice"] == "auto"
    assert secrets.value is None
    assert json.loads((tmp_path / "settings.json").read_text())["enabled"] is False


def test_encrypted_secret_store_never_writes_plaintext(tmp_path: Path) -> None:
    secret_path = tmp_path / "llm_api_key.enc"
    master_key_path = tmp_path / ".secret_master_key"
    store = EncryptedFileSecretStore(secret_path, master_key_path)

    store.set("sk-sensitive-value")

    assert store.get() == "sk-sensitive-value"
    assert b"sk-sensitive-value" not in secret_path.read_bytes()
    assert_private_file_permissions(secret_path)
    assert_private_file_permissions(master_key_path)

    store.delete()
    assert store.get() is None


def test_spatial_scene_pipeline_and_personal_asset_library(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )
    image = Image.new("RGB", (360, 240), "#8abda6")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")

    created = client.post(
        "/api/spatial-scenes",
        files={"file": ("window-cat.png", image_buffer.getvalue(), "image/png")},
        data={"title": "窗边的猫"},
    )

    assert created.status_code == 202
    created_body = created.json()
    asset_id = created_body["asset"]["id"]
    job_id = created_body["job"]["id"]
    assert created_body["asset"]["status"] in {"processing", "ready"}

    spatial.wait_for_idle()
    job = client.get(f"/api/jobs/{job_id}")
    asset = client.get(f"/api/assets/{asset_id}")

    assert job.status_code == 200
    assert job.json()["status"] == "completed"
    assert job.json()["progress"] == 100
    assert asset.status_code == 200
    assert asset.json()["status"] == "ready"
    assert asset.json()["name"] == "窗边的猫"
    assert asset.json()["model_name"] == "Test Depth"

    for url_key in (
        "source_url",
        "depth_url",
        "background_url",
        "foreground_url",
        "manifest_url",
    ):
        response = client.get(asset.json()[url_key])
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, max-age=3600"

    manifest = client.get(asset.json()["manifest_url"]).json()
    assert manifest["version"] == 2
    assert manifest["representation"] == "layered-depth-image"

    library = client.get("/api/assets")
    assert library.status_code == 200
    assert [item["id"] for item in library.json()["assets"]] == [asset_id]

    agent_response = client.post(
        "/api/agent/run",
        json={"message": "查看我的个人资产"},
    )
    assert agent_response.status_code == 200
    assert "窗边的猫" in agent_response.json()["answer"]

    deleted = client.delete(f"/api/assets/{asset_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/assets/{asset_id}").status_code == 404
    spatial.close()


def test_agent_creates_spatial_scene_from_local_attachment(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )
    image = Image.new("RGB", (320, 200), "#7698c4")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")

    staged = client.post(
        "/api/source-images",
        files={"file": ("desk.png", image_buffer.getvalue(), "image/png")},
    )

    assert staged.status_code == 201
    source = staged.json()
    assert source["original_name"] == "desk.png"
    assert source["width"] == 320
    assert source["height"] == 200

    response = client.post(
        "/api/agent/run",
        json={
            "message": "把这张图片生成可拖动视角的空间照片",
            "source_image_id": source["id"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    tool_step = next(
        step
        for step in body["steps"]
        if step["label"] == "调用 create_spatial_scene"
    )
    assert tool_step["output"]["asset_id"]
    assert tool_step["output"]["job_id"]
    assert "正在本机" in body["answer"]
    assert not (spatial.source_image_dir / source["id"]).exists()

    spatial.wait_for_idle()
    job = client.get(f"/api/jobs/{tool_step['output']['job_id']}").json()
    assert job["status"] == "completed"
    assert client.get(f"/api/assets/{job['asset_id']}").json()["status"] == "ready"
    spatial.close()


def test_agent_rejects_unknown_source_image_id(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )

    response = client.post(
        "/api/agent/run",
        json={
            "message": "生成空间照片",
            "source_image_id": "../../private.png",
        },
    )

    assert response.status_code == 422
    assert "标识无效" in response.json()["detail"]
    spatial.close()


def test_spatial_scene_rejects_invalid_upload(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )

    response = client.post(
        "/api/spatial-scenes",
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 422
    assert "图片" in response.json()["detail"]
    spatial.close()


def test_photo_style_pipeline_and_manifest(tmp_path: Path) -> None:
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

    def png(color: str, size: tuple[int, int] = (320, 224)) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, "PNG")
        return buffer.getvalue()

    created = client.post(
        "/api/photo-style-transfers",
        files=[
            ("content_file", ("portrait.png", png("#b88d72"), "image/png")),
            ("style_files", ("style-a.png", png("#315a84"), "image/png")),
            ("style_files", ("style-b.png", png("#c49a44"), "image/png")),
        ],
        data={
            "title": "蓝金肖像",
            "mode": "preserve_layout",
            "quality": "preview",
            "style_strength": "0.8",
            "content_strength": "0.85",
            "detail_strength": "0.7",
            "seed": "1701",
        },
    )

    assert created.status_code == 202
    body = created.json()
    assert body["asset"]["kind"] == "photo_style_transfer"
    style.wait_for_idle()
    job = client.get(f"/api/jobs/{body['job']['id']}").json()
    asset = client.get(f"/api/assets/{body['asset']['id']}").json()
    assert job["status"] == "completed"
    assert asset["status"] == "ready"
    assert asset["provider_name"] == "local-preview"
    assert asset["parameters"]["seed"] == 1701
    assert len(asset["style_reference_urls"]) == 2
    assert client.get(asset["result_url"]).status_code == 200
    for reference_url in asset["style_reference_urls"]:
        assert client.get(reference_url).status_code == 200
    manifest = client.get(asset["manifest_url"]).json()
    assert manifest["schema"] == "personal-agent.photo-style-transfer"
    assert manifest["author"] == "Ma Xianggang"
    assert manifest["provider_metadata"]["model_download_required"] is False
    style.close()
    spatial.close()


def test_pic_style_http_provider_matches_reference_contract() -> None:
    requests: list[httpx.Request] = []
    result_buffer = BytesIO()
    Image.new("RGB", (96, 64), "#527aa3").save(result_buffer, "PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/assets":
            return httpx.Response(201, json={"asset_id": f"asset-{len(requests)}"})
        if request.method == "POST" and request.url.path.endswith("/jobs"):
            payload = json.loads(request.content)
            assert payload["content_image"]["asset_id"] == "asset-1"
            assert payload["style_images"][0]["image"]["asset_id"] == "asset-2"
            assert payload["mode"] == "preserve_layout"
            assert payload["num_outputs"] == 1
            return httpx.Response(202, json={"job_id": "upstream-job"})
        if request.method == "GET" and request.url.path.endswith(
            "/jobs/upstream-job"
        ):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "progress": 100,
                    "stage": "completed",
                    "result": {
                        "outputs": [{"url": "/v1/assets/result.png"}],
                        "provider": "sdxl_ip_adapter_8gb_v1",
                        "provider_version": "1.0.0",
                        "model_versions": {"base": "test-checkpoint"},
                        "normalized_parameters": {"seed": 1701},
                    },
                },
            )
        if request.method == "GET" and request.url.path == "/v1/assets/result.png":
            return httpx.Response(
                200,
                content=result_buffer.getvalue(),
                headers={"content-type": "image/png"},
            )
        return httpx.Response(404)

    model_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = PicStyleHttpProvider(
        "http://pic-style.local",
        api_key="test-only-key",
        tenant_id="test-tenant",
        client=model_client,
    )
    result = provider.stylize(
        Image.new("RGB", (128, 96), "#b58a6d"),
        [Image.new("RGB", (128, 96), "#315a84")],
        StyleParameters(quality="preview", seed=1701),
        lambda *_: None,
    )

    assert result.image.size == (96, 64)
    assert result.model_name == "sdxl_ip_adapter_8gb_v1"
    assert result.metadata["normalized_parameters"]["seed"] == 1701
    assert all(request.headers["X-Tenant-ID"] == "test-tenant" for request in requests)
    assert all(request.headers["X-API-Key"] == "test-only-key" for request in requests)
    provider.close()
    model_client.close()


def test_agent_creates_photo_style_from_local_attachments(tmp_path: Path) -> None:
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

    def stage(name: str, color: str) -> dict:
        buffer = BytesIO()
        Image.new("RGB", (256, 192), color).save(buffer, "PNG")
        response = client.post(
            "/api/source-images",
            files={"file": (name, buffer.getvalue(), "image/png")},
        )
        assert response.status_code == 201
        return response.json()

    content = stage("content.png", "#b78b6c")
    reference = stage("reference.png", "#3d688c")
    response = client.post(
        "/api/agent/run",
        json={
            "message": "把内容图按参考图进行图片风格化",
            "source_image_id": content["id"],
            "style_image_ids": [reference["id"]],
        },
    )

    assert response.status_code == 200
    body = response.json()
    tool_step = next(
        step
        for step in body["steps"]
        if step["label"] == "调用 create_photo_style_transfer"
    )
    assert tool_step["output"]["kind"] == "photo_style_transfer"
    assert not (spatial.source_image_dir / content["id"]).exists()
    assert not (spatial.source_image_dir / reference["id"]).exists()
    style.wait_for_idle()
    assert client.get(f"/api/jobs/{tool_step['output']['job_id']}").json()[
        "status"
    ] == "completed"
    style.close()
    spatial.close()


def test_photo_style_rejects_missing_reference(tmp_path: Path) -> None:
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
    buffer = BytesIO()
    Image.new("RGB", (128, 128), "#708090").save(buffer, "PNG")
    response = client.post(
        "/api/photo-style-transfers",
        files={"content_file": ("content.png", buffer.getvalue(), "image/png")},
    )
    assert response.status_code == 422
    style.close()
    spatial.close()
