from __future__ import annotations

import json
import os
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
from backend.app.identity import IdentityBindingRegistry
from backend.app.memory import MemoryIsolationError
from backend.app.settings import (
    EncryptedFileSecretStore,
    SettingsRepository,
    SettingsService,
    StoredLLMSettings,
)
from backend.app.spatial_segmentation import ForegroundMaskResult


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


class FakeForegroundSegmenter:
    def segment(self, image: Image.Image, depth: Image.Image, progress):
        del depth
        progress(68, "segmenting", "测试主体分割")
        mask = Image.new("L", image.size, 0)
        inset = max(4, min(image.size) // 8)
        mask.paste(255, (inset, inset, image.width - inset, image.height - inset))
        return ForegroundMaskResult(
            mask=mask,
            model_name="Test Semantic Mask",
            quality_score=0.95,
        )


def build_test_spatial(tmp_path: Path) -> SpatialSceneService:
    return SpatialSceneService(
        tmp_path / "personal-assets",
        estimator=FakeDepthEstimator(),
        segmenter=FakeForegroundSegmenter(),
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


def test_real_llm_direct_answer_uses_multi_turn_context(tmp_path: Path) -> None:
    request_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        answer = "你好，小林。" if len(request_bodies) == 1 else "你叫小林。"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": answer,
                        }
                    }
                ]
            },
        )

    planner = OpenAICompatiblePlanner(
        api_key="test-key",
        base_url="https://model.example/v1",
        model="test-chat-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
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

    first = client.post(
        "/api/agent/run",
        json={"message": "我叫小林。", "session_id": "basic-qa"},
    )
    second = client.post(
        "/api/agent/run",
        json={"message": "我叫什么？", "session_id": "basic-qa"},
    )

    assert first.status_code == 200
    assert first.json()["answer"] == "你好，小林。"
    assert second.status_code == 200
    assert second.json()["answer"] == "你叫小林。"
    assert second.json()["mode"] == "llm:test-chat-model"
    roles = [item["role"] for item in request_bodies[1]["messages"]]
    assert roles[-3:] == ["user", "assistant", "user"]
    assert request_bodies[1]["messages"][-3]["content"] == "我叫小林。"


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
    assert run["status"] == "completed"
    assert "Asia/Shanghai" in run["answer"]

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
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body.get("tools"):
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
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "模型连接正常。",
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
    assert health["llm_status"] == "ready"
    assert health["model"] == "qwen3.7-plus"
    assert len(requests) == 2

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

    disabled = client.put(
        "/api/settings/llm",
        json={
            "enabled": False,
            "provider_id": "qwen",
            "base_url": providers["qwen"]["base_url"],
            "model": providers["qwen"]["default_model"],
            "timeout_seconds": 45,
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["has_api_key"] is True
    disabled_health = client.get("/health").json()
    assert disabled_health["llm_configured"] is False
    assert disabled_health["llm_status"] == "disabled"

    cleared = client.delete("/api/settings/llm/api-key")
    assert cleared.status_code == 200
    assert cleared.json()["has_api_key"] is False
    assert cleared.json()["enabled"] is False
    assert secrets.value is None
    assert client.get("/health").json()["llm_configured"] is False


def test_saved_key_without_activation_is_reported_explicitly(
    tmp_path: Path,
) -> None:
    settings, secrets = build_test_settings(tmp_path)
    secrets.value = "saved-key"
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )

    catalog = client.get("/api/settings/providers").json()
    health = client.get("/health").json()

    assert catalog["settings"]["has_api_key"] is True
    assert catalog["runtime"]["status"] == "configured_not_enabled"
    assert catalog["runtime"]["active"] is False
    assert health["llm_status"] == "configured_not_enabled"
    assert health["agent_mode"] == "demo-rule-planner"


def test_disabling_real_model_preserves_saved_key(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("tools"):
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
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "连接正常。",
                        }
                    }
                ]
            },
        )

    settings, secrets = build_test_settings(
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )
    payload = {
        "enabled": True,
        "provider_id": "custom",
        "base_url": "https://model.example/v1",
        "model": "working-model",
        "api_key": "saved-key",
        "timeout_seconds": 30,
    }
    assert client.put("/api/settings/llm", json=payload).status_code == 200

    disabled = client.put(
        "/api/settings/llm",
        json={**payload, "enabled": False, "api_key": None},
    )

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["has_api_key"] is True
    assert secrets.value == "saved-key"
    health = client.get("/health").json()
    assert health["llm_configured"] is False
    assert health["llm_status"] == "disabled"
    assert health["agent_mode"] == "demo-rule-planner"


def test_connection_check_uses_draft_without_saving_key(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if not body.get("tools"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "模型连接正常。",
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
    assert response.json()["qa_ok"] is True
    assert response.json()["tool_calling_ok"] is True
    assert response.json()["answer_preview"] == "模型连接正常。"
    assert len(requests) == 2
    assert all(
        str(request.url) == "https://api.deepseek.com/chat/completions"
        for request in requests
    )
    assert all(
        request.headers["Authorization"] == "Bearer temporary-test-key"
        for request in requests
    )
    qa_request = json.loads(requests[0].content)
    tool_request = json.loads(requests[1].content)
    assert "tools" not in qa_request
    assert "tool_choice" not in qa_request
    assert tool_request["tool_choice"] == "auto"
    assert secrets.value is None
    assert json.loads((tmp_path / "settings.json").read_text())["enabled"] is False


def test_failed_model_activation_keeps_previous_planner_and_settings(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "broken-model":
            return httpx.Response(401, json={"error": {"message": "invalid"}})
        if body.get("tools"):
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
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "连接正常。",
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
    working = {
        "enabled": True,
        "provider_id": "custom",
        "base_url": "https://working.example/v1",
        "model": "working-model",
        "api_key": "working-key",
        "timeout_seconds": 30,
    }
    assert client.put("/api/settings/llm", json=working).status_code == 200

    failed = client.put(
        "/api/settings/llm",
        json={
            **working,
            "model": "broken-model",
            "api_key": "replacement-key",
        },
    )

    assert failed.status_code == 502
    health = client.get("/health").json()
    assert health["llm_configured"] is True
    assert health["llm_status"] == "ready"
    assert health["model"] == "working-model"
    stored = json.loads((tmp_path / "settings.json").read_text())
    assert stored["model"] == "working-model"
    assert secrets.value == "working-key"


def test_model_without_tool_calling_still_enables_basic_qa(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        answer = "我会直接回答。" if body.get("tools") else "连接正常。"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": answer,
                        }
                    }
                ]
            },
        )

    settings, _ = build_test_settings(
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=build_test_spatial(tmp_path),
        )
    )
    activated = client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "provider_id": "custom",
            "base_url": "https://qa-only.example/v1",
            "model": "qa-only-model",
            "api_key": "qa-only-key",
            "timeout_seconds": 30,
        },
    )

    assert activated.status_code == 200
    status = client.get("/api/settings/llm/status").json()
    assert status["status"] == "degraded"
    assert status["active"] is True
    assert status["qa_available"] is True
    assert status["tool_calling_available"] is False
    answer = client.post(
        "/api/agent/run",
        json={"message": "请回答一个基础问题", "session_id": "qa-only"},
    )
    assert answer.status_code == 200
    assert answer.json()["mode"] == "llm:qa-only-model"
    assert answer.json()["answer"] == "我会直接回答。"


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
        "foreground_mask_url",
        "manifest_url",
    ):
        response = client.get(asset.json()[url_key])
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, max-age=3600"

    manifest = client.get(asset.json()["manifest_url"]).json()
    assert manifest["version"] == 2
    assert manifest["representation"] == "layered-depth-image"
    assert manifest["segmentation_model"] == "Test Semantic Mask"
    assert manifest["segmentation_quality"] == 0.95
    assert manifest["foreground_mask"] == "foreground-mask.png"
    assert manifest["recommended_strength"] == 0.26
    assert asset.json()["recommended_strength"] == 0.26

    library = client.get("/api/assets")
    assert library.status_code == 200
    assert [item["id"] for item in library.json()["assets"]] == [asset_id]

    agent_response = client.post(
        "/api/agent/run",
        json={"message": "查看我的个人资产"},
    )
    assert agent_response.status_code == 200
    assert "窗边的猫" in agent_response.json()["answer"]
    asset_tool_output = next(
        step["output"]
        for step in agent_response.json()["steps"]
        if step["stage"] == "tool"
        and isinstance(step.get("output", {}).get("assets"), list)
    )
    listed_asset = asset_tool_output["assets"][0]
    assert listed_asset["id"] == asset_id
    # The lightweight test pipeline has no dedicated preview file, so the
    # asset list intentionally falls back to the owner-scoped source image.
    assert listed_asset["thumbnail_url"].endswith("/source.webp")
    assert listed_asset["width"] == 360
    assert listed_asset["height"] == 240

    deleted = client.delete(f"/api/assets/{asset_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/assets/{asset_id}").status_code == 404
    spatial.close()


def test_failed_spatial_job_can_retry_without_creating_duplicate(
    tmp_path: Path,
) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )
    image = Image.new("RGB", (180, 120), "#8abda6")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    created = client.post(
        "/api/spatial-scenes",
        files={"file": ("retry.png", image_buffer.getvalue(), "image/png")},
        data={"title": "可重试空间照片"},
    ).json()
    spatial.wait_for_idle()
    job_id = created["job"]["id"]
    asset_id = created["asset"]["id"]
    spatial.repository.update_job(
        job_id,
        status="failed",
        progress=100,
        stage="interrupted",
        message="任务因本地服务重启而中断，可以重新生成。",
        error="本地服务重启中断任务。",
    )
    spatial.repository.fail_asset(asset_id)

    first_retry = client.post(f"/api/jobs/{job_id}/retry")
    second_retry = client.post(f"/api/jobs/{job_id}/retry")

    assert first_retry.status_code == 202
    assert second_retry.status_code == 202
    assert first_retry.json()["id"] == job_id
    assert second_retry.json()["id"] == job_id
    assert len(client.get("/api/jobs").json()["jobs"]) == 1
    spatial.wait_for_idle()
    completed_job = client.get(f"/api/jobs/{job_id}").json()
    assert completed_job["status"] == "completed"
    assert completed_job["error"] is None
    assert client.get(f"/api/assets/{asset_id}").json()["status"] == "ready"
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


def test_root_can_manage_feishu_identity_bindings(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    identity_registry = IdentityBindingRegistry(tmp_path / "identity.sqlite3")
    app = create_app(
        tmp_path / "runs.jsonl",
        settings_service=settings,
        spatial_service=spatial,
        identity_registry=identity_registry,
    )
    client = TestClient(app)

    created = client.post(
        "/api/admin/identity-bindings",
        json={
            "app_id": "cli_test",
            "open_id": "ou_user_a",
            "workspace_name": "成员 A 工作区",
        },
    )
    assert created.status_code == 201
    binding = created.json()
    assert binding["workspace_name"] == "成员 A 工作区"
    assert binding["status"] == "active"

    listed = client.get("/api/admin/identity-bindings")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["bindings"]] == [binding["id"]]

    suspended = client.put(
        f"/api/admin/identity-bindings/{binding['id']}/status",
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    renamed = client.put(
        f"/api/admin/identity-bindings/{binding['id']}/workspace",
        json={"name": "新的成员 A 工作区"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["workspace_name"] == "新的成员 A 工作区"

    remote_client = TestClient(app, client=("198.51.100.10", 50000))
    denied = remote_client.get("/api/admin/identity-bindings")
    assert denied.status_code == 403
    assert "仅允许从本机" in denied.json()["detail"]
    spatial.close()


def test_spatial_scene_adapter_reuses_real_idempotency_key(tmp_path: Path) -> None:
    spatial = build_test_spatial(tmp_path)
    image_buffer = BytesIO()
    Image.new("RGB", (32, 32), "green").save(image_buffer, format="PNG")

    first = spatial.create_scene(
        image_buffer.getvalue(),
        original_name="scene.png",
        owner_id="user-a",
        idempotency_key="run-7:call-1",
    )
    second = spatial.create_scene(
        b"this body is not decoded on replay",
        original_name="scene.png",
        owner_id="user-a",
        idempotency_key="run-7:call-1",
    )

    assert second.asset.id == first.asset.id
    assert second.job.id == first.job.id
    assert [asset.id for asset in spatial.list_assets(owner_id="user-a")] == [
        first.asset.id
    ]
    spatial.wait_for_idle()
    spatial.close()


def test_memory_center_crud_and_export_api(tmp_path: Path) -> None:
    settings, _ = build_test_settings(tmp_path)
    spatial = build_test_spatial(tmp_path)
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=settings,
            spatial_service=spatial,
        )
    )

    created = client.post(
        "/api/memories",
        json={
            "content": "我偏好在本机处理私人图片",
            "memory_type": "preference",
            "importance": 0.9,
        },
    )
    assert created.status_code == 201
    memory = created.json()
    assert memory["memory_type"] == "preference"
    assert memory["source"] == "web:memory-center"
    assert memory["status"] == "active"
    assert memory["retrieval_policy"] == "explicit_only"
    assert memory["evidence_count"] == 1
    assert memory["evidence_refs"] == ["web:memory-center"]

    listed = client.get(
        "/api/memories",
        params={"type": "preference", "query": "本机"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["memories"]] == [memory["id"]]

    rejected = client.put(
        f"/api/memories/{memory['id']}",
        json={"content": None},
    )
    assert rejected.status_code == 422

    updated = client.put(
        f"/api/memories/{memory['id']}",
        json={
            "content": "我偏好仅在本机处理私人图片",
            "importance": 1,
            "retrieval_policy": "explicit_only",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["importance"] == 1
    assert updated.json()["retrieval_policy"] == "explicit_only"
    assert "仅在本机" in updated.json()["content"]
    assert updated.json()["evidence_count"] == 2

    evidence = client.get(f"/api/memories/{memory['id']}/evidence")
    assert evidence.status_code == 200
    evidence_items = evidence.json()["evidence"]
    assert {item["source_type"] for item in evidence_items} == {
        "explicit",
        "revision",
    }
    assert all(item["content_hash"] for item in evidence_items)

    run = client.post(
        "/api/agent/run",
        json={
            "message": "请复述我处理私人图片的偏好",
            "session_id": "memory-recall",
        },
    )
    assert run.status_code == 200
    recalled = client.get(
        "/api/memories",
        params={"type": "preference", "query": "本机"},
    ).json()["memories"][0]
    assert recalled["access_count"] == 1
    assert recalled["utility_score"] > memory["utility_score"]

    exported = client.get("/api/memories/export")
    assert exported.status_code == 200
    assert exported.json()["version"] == "1"
    assert exported.json()["memories"][0]["id"] == memory["id"]

    deleted = client.delete(f"/api/memories/{memory['id']}")
    assert deleted.status_code == 204
    assert client.get(
        "/api/memories",
        params={"type": "preference"},
    ).json()["memories"] == []
    remaining = client.get("/api/memories").json()["memories"]
    assert any(item["memory_type"] == "episode" for item in remaining)
    spatial.close()


def test_atomic_memory_api_splits_compound_statement(tmp_path: Path) -> None:
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
        "/api/memories/atomic",
        json={"content": "我叫林舟，住在北京，喜欢简洁回答"},
    )

    assert response.status_code == 201
    memories = response.json()["memories"]
    assert [item["content"] for item in memories] == [
        "我叫林舟",
        "我住在北京",
        "我喜欢简洁回答",
    ]
    assert {item["retrieval_policy"] for item in memories} == {"always"}
    spatial.close()
