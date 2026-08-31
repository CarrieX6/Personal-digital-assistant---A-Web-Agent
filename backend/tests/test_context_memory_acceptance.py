from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.memory import MemoryIsolationError, SQLiteMemoryStore


class MarkerPreservingSummaryProvider:
    """Deterministic oracle for storage-path acceptance, not a model-quality proxy."""

    summary_provider_id = "acceptance:marker-preserving-oracle"

    def summarize_session(
        self,
        *,
        previous: dict,
        messages: list[dict],
        token_budget: int,
    ) -> dict:
        summary = str(previous.get("summary", ""))
        for message in messages:
            content = str(message.get("content", ""))
            if "EARLY_FACT:" in content:
                summary = content
        return {
            "summary": summary,
            "open_loops": list(previous.get("open_loops", [])),
            "decisions": list(previous.get("decisions", [])),
            "completed_actions": list(previous.get("completed_actions", [])),
            "active_assumptions": list(previous.get("active_assumptions", [])),
            "artifact_refs": list(previous.get("artifact_refs", [])),
            "blockers": list(previous.get("blockers", [])),
            "next_goal": previous.get("next_goal"),
        }


@pytest.mark.parametrize("turn_count", [500, 2_000])
def test_early_fact_survives_long_session_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn_count: int,
) -> None:
    monkeypatch.setenv("AGENT_RECENT_HISTORY_MESSAGES", "20")
    monkeypatch.setenv("AGENT_SESSION_SUMMARY_MODE", "structured")
    database_path = tmp_path / f"long-session-{turn_count}.sqlite3"
    store = SQLiteMemoryStore(
        database_path,
        summary_provider=MarkerPreservingSummaryProvider(),
    )
    early_fact = "EARLY_FACT: 客户批准的验收代号是海棠-7。"
    for turn in range(turn_count):
        store.append_message(
            owner_id="owner-a",
            thread_id=f"web:long-{turn_count}",
            role="user",
            content=early_fact if turn == 0 else f"用户轮次 {turn}",
        )
        store.append_message(
            owner_id="owner-a",
            thread_id=f"web:long-{turn_count}",
            role="assistant",
            content=f"助手轮次 {turn}",
        )
    store.close()

    reopened = SQLiteMemoryStore(database_path)
    context = reopened.context(
        owner_id="owner-a",
        thread_id=f"web:long-{turn_count}",
        channel="web",
        query="验收代号是什么",
        message_limit=20,
    )

    assert early_fact in context.session_summary
    assert len(context.messages) == 20
    assert context.messages[-1] == ("assistant", f"助手轮次 {turn_count - 1}")
    reopened.close()


def test_owner_isolation_has_zero_leakage_in_adversarial_matrix(
    tmp_path: Path,
) -> None:
    store = SQLiteMemoryStore(tmp_path / "owner-matrix.sqlite3")
    owner_count = 128
    for index in range(owner_count):
        store.remember(
            owner_id=f"owner-{index}",
            content=f"隔离标记 owner-{index}-secret",
            source="acceptance:owner-matrix",
        )

    leaks: list[tuple[int, int]] = []
    for requester in range(owner_count):
        other = (requester + 1) % owner_count
        results = store.search_memories(
            owner_id=f"owner-{requester}",
            query=f"owner-{other}-secret",
            thread_id=f"web:owner-{requester}",
            channel="web",
        )
        if any(f"owner-{other}-secret" in item.content for item in results):
            leaks.append((requester, other))
    assert leaks == []

    store.ensure_thread(
        owner_id="owner-0",
        thread_id="web:owned-thread",
        channel="web",
    )
    with pytest.raises(MemoryIsolationError):
        store.ensure_thread(
            owner_id="owner-1",
            thread_id="web:owned-thread",
            channel="web",
        )
    store.close()
