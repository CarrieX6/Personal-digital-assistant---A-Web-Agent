from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from backend.app.llm import LLMError, OpenAICompatiblePlanner
from backend.app.token_usage import (
    TokenUsageStore,
    bind_usage_context,
    parse_provider_usage,
)


def test_provider_usage_is_persisted_without_prompt_content(tmp_path: Path) -> None:
    store = TokenUsageStore(tmp_path / "token_usage.sqlite3")
    assert parse_provider_usage(
        {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    ) == (12, 5, 17)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "total_tokens": 17,
                },
            },
        )

    planner = OpenAICompatiblePlanner(
        api_key="test",
        base_url="https://model.example/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        usage_store=store,
    )
    with bind_usage_context(
        owner_id="owner-1",
        run_id="run-1",
        thread_id="thread-1",
        channel="web",
        project_id=None,
        stage="plan",
    ):
        result = planner.plan("包含不能落库的秘密文本", [])

    assert result.direct_answer == "ok"
    summary = store.summary(run_id="run-1")
    assert summary.total_tokens == 17
    assert summary.actual_calls == 1
    assert store.events(run_id="run-1")[0]["stage"] == "plan"
    with sqlite3.connect(tmp_path / "token_usage.sqlite3") as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(token_usage)")
        }
        assert "prompt" not in columns
        assert "content" not in columns
    planner.close()
    store.close()


def test_missing_provider_usage_falls_back_to_explicit_estimate(tmp_path: Path) -> None:
    store = TokenUsageStore(tmp_path / "token_usage.sqlite3")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    planner = OpenAICompatiblePlanner(
        api_key="test",
        base_url="https://model.example/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        usage_store=store,
    )
    with bind_usage_context(
        owner_id="owner-1",
        run_id="run-estimate",
        thread_id="thread-1",
        channel="web",
        project_id=None,
        stage="final",
    ):
        planner.plan("hello", [])
    summary = store.summary(run_id="run-estimate")
    assert summary.estimated_calls == 1
    assert summary.total_tokens > 0
    planner.close()
    store.close()


def test_run_budget_blocks_request_before_transport(tmp_path: Path) -> None:
    store = TokenUsageStore(tmp_path / "token_usage.sqlite3")
    transport_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        return httpx.Response(200, json={"choices": []})

    planner = OpenAICompatiblePlanner(
        api_key="test",
        base_url="https://model.example/v1",
        model="test-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        usage_store=store,
        reserved_output_tokens=256,
    )
    with bind_usage_context(
        owner_id="owner-1",
        run_id="run-budget",
        thread_id="thread-1",
        channel="web",
        project_id=None,
        max_total_tokens=10,
    ):
        with pytest.raises(LLMError, match="Token 预算"):
            planner.plan("这条请求会超过预算", [])
    assert transport_calls == 0
    planner.close()
    store.close()
