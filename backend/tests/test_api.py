from __future__ import annotations

import json
import stat
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.assets import AssetError, SpatialSceneService
from backend.app.llm import OpenAICompatiblePlanner
from backend.app.main import create_app
from backend.app.memory import MemoryIsolationError
from backend.app.settings import (
    EncryptedFileSecretStore,
    SettingsRepository,
    SettingsService,
    StoredLLMSettings,
)


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
        "get_job_status",
    }


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
        "policy",
        "tool",
        "decision",
        "policy",
        "tool",
        "decision",
        "decision",
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
    assert "生成可拖动视角的空间照片" in response.json()["answer"]


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
        "policy",
        "tool",
        "decision",
        "decision",
        "final",
    ]
    assert len(request_bodies) == 2
    assert request_bodies[0]["tool_choice"] == "auto"
    assert request_bodies[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    assert request_bodies[1]["messages"][-1]["role"] == "tool"
    assert request_bodies[1]["messages"][-1]["tool_call_id"] == "call_test_1"


def test_agent_run_query_and_duplicate_decision_are_idempotent(
    tmp_path: Path,
) -> None:
    settings, _ = build_test_settings(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )
    created = client.post(
        "/api/agent/run",
        json={"message": "现在几点？", "session_id": "run-query"},
    )
    assert created.status_code == 200
    run = created.json()

    fetched = client.get(f"/api/agent/runs/{run['run_id']}")
    listed = client.get("/api/agent/runs", params={"status": "completed"})
    duplicate = client.post(
        f"/api/agent/runs/{run['run_id']}/decision",
        json={"approved": True},
    )

    assert fetched.status_code == 200
    assert fetched.json() == run
    assert run["run_id"] in {
        item["run_id"] for item in listed.json()["runs"]
    }
    assert duplicate.status_code == 200
    assert duplicate.json() == run


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
    assert stat.S_IMODE((tmp_path / "settings.json").stat().st_mode) == 0o600

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
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(master_key_path.stat().st_mode) == 0o600

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


def test_memory_and_threads_are_isolated_by_owner(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    app = create_app(
        tmp_path / "runs.jsonl",
        settings_service=settings,
        spatial_service=spatial,
    )
    runner = app.state.runner

    remembered = runner.run(
        "记住：我偏好绿色界面",
        owner_id="user-a",
        thread_id="thread-a",
        channel="test",
    )
    user_a = runner.run(
        "查看我的记忆",
        owner_id="user-a",
        thread_id="thread-a",
        channel="test",
    )
    user_b = runner.run(
        "查看我的记忆",
        owner_id="user-b",
        thread_id="thread-b",
        channel="test",
    )

    assert "已为你保存" in remembered.answer
    assert "绿色界面" in user_a.answer
    assert "没有为你保存" in user_b.answer
    with pytest.raises(MemoryIsolationError):
        runner.run(
            "你好",
            owner_id="user-b",
            thread_id="thread-a",
            channel="test",
        )
    spatial.close()


def test_web_conversations_are_server_persisted_with_run_metadata(
    tmp_path: Path,
) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    app = create_app(
        tmp_path / "runs.jsonl",
        settings_service=settings,
        spatial_service=spatial,
    )
    client = TestClient(app)

    created = client.post(
        "/api/conversations",
        json={"session_id": "research-1", "title": "新对话"},
    )
    assert created.status_code == 201
    assert created.json()["id"] == "research-1"
    assert created.json()["message_count"] == 0

    run = client.post(
        "/api/agent/run",
        json={
            "session_id": "research-1",
            "message": "分析这段文字：医学人工智能需要可靠评测。",
        },
    )
    assert run.status_code == 200

    conversations = client.get(
        "/api/conversations",
        params={"channel": "web"},
    )
    assert conversations.status_code == 200
    thread = conversations.json()["conversations"][0]
    assert thread["id"] == "research-1"
    assert thread["title"].startswith("分析这段文字")
    assert thread["message_count"] == 2

    messages = client.get("/api/conversations/research-1/messages")
    assert messages.status_code == 200
    history = messages.json()["messages"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[1]["run_id"] == run.json()["run_id"]
    assert history[1]["metadata"]["run"]["steps"]

    renamed = client.put(
        "/api/conversations/research-1",
        json={"title": "医学 AI 调研"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "医学 AI 调研"

    deleted = client.delete("/api/conversations/research-1")
    assert deleted.status_code == 204
    missing = client.get("/api/conversations/research-1/messages")
    assert missing.status_code == 404
    spatial.close()


def test_web_conversation_rejects_unsafe_session_id(tmp_path: Path) -> None:
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
        json={"session_id": "../other", "message": "你好"},
    )

    assert response.status_code == 422
    assert "会话 ID" in response.json()["detail"]
    spatial.close()


def test_spatial_assets_are_isolated_by_owner(tmp_path: Path) -> None:
    spatial = build_test_spatial(tmp_path)
    image = Image.new("RGB", (120, 80), "#5c8b77")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")

    created = spatial.create_scene(
        image_buffer.getvalue(),
        original_name="private.png",
        owner_id="user-a",
    )
    spatial.wait_for_idle()

    assert [asset.id for asset in spatial.list_assets(owner_id="user-a")] == [
        created.asset.id
    ]
    assert spatial.list_assets(owner_id="user-b") == []
    assert spatial.get_asset(
        created.asset.id,
        owner_id="user-a",
    ).id == created.asset.id
    with pytest.raises(AssetError, match="找不到"):
        spatial.get_asset(created.asset.id, owner_id="user-b")
    spatial.close()
