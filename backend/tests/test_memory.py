from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.app.memory import MemoryPolicyError, SQLiteMemoryStore


def test_typed_memory_supersedes_stale_temporal_fact(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")

    old_id = store.remember(
        owner_id="user-a",
        content="我现在住在上海",
        source="web:first",
        memory_type="profile",
        valid_from=1_000,
    )
    new_id = store.remember(
        owner_id="user-a",
        content="我现在住在北京",
        source="web:second",
        memory_type="profile",
        valid_from=2_000,
    )

    old = store.get_memory(owner_id="user-a", memory_id=old_id)
    new = store.get_memory(owner_id="user-a", memory_id=new_id)
    assert old is not None and old.status == "superseded"
    assert old.valid_to == 2_000
    assert new is not None and new.status == "active"
    assert new.supersedes_id == old_id

    historical_id = store.remember(
        owner_id="user-a",
        content="我现在住在杭州",
        source="web:historical",
        memory_type="profile",
        valid_from=1_500,
    )
    historical = store.get_memory(owner_id="user-a", memory_id=historical_id)
    assert historical is not None and historical.status == "superseded"
    assert historical.valid_to == 2_000

    results = store.search_memories(
        owner_id="user-a",
        query="我目前的地址在哪里",
        limit=5,
    )
    assert [item.id for item in results] == [new_id]


def test_memory_search_is_scoped_and_rejects_credentials(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    memory_id = store.remember(
        owner_id="user-a",
        content="我偏好绿色界面",
        source="web:settings",
    )
    private_id = store.remember(
        owner_id="user-a",
        content="我的家庭住址是测试路 12 号",
        source="web:profile",
        memory_type="profile",
    )

    selected = store.search_memories(
        owner_id="user-a",
        query="我喜欢什么颜色的主题",
        limit=3,
    )
    assert selected[0].id == memory_id
    assert private_id not in {item.id for item in selected}
    assert store.search_memories(
        owner_id="user-a",
        query="帮我分析项目进度",
        limit=3,
    ) == []
    assert store.search_memories(
        owner_id="user-b",
        query="绿色界面",
        limit=3,
    ) == []

    with pytest.raises(MemoryPolicyError, match="拒绝写入"):
        store.remember(
            owner_id="user-a",
            content="API Key: test-private-example-value",
            source="web:unsafe",
        )


def test_session_summary_preserves_decisions_and_open_loops(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    for index in range(8):
        store.append_message(
            owner_id="user-a",
            thread_id="web:long-thread",
            role="user" if index % 2 == 0 else "assistant",
            content=(
                "我们决定采用本地 SQLite 保存记忆"
                if index == 0
                else "下一步还需完成记忆导出界面"
                if index == 2
                else f"普通对话 {index}"
            ),
        )

    context = store.context(
        owner_id="user-a",
        thread_id="web:long-thread",
        channel="web",
        query="下一步做什么",
    )
    assert "SQLite" in context.session_summary
    assert any("决定" in item for item in context.decisions)
    assert any("下一步" in item for item in context.open_loops)


def test_memory_usage_updates_utility_and_records_episode(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    memory_id = store.remember(
        owner_id="user-a",
        content="空间照片失败后优先复用原始图片重试",
        source="web:explicit",
        memory_type="procedure",
    )
    selected = store.search_memories(
        owner_id="user-a",
        query="空间照片失败如何处理",
        limit=3,
    )
    before = store.get_memory(owner_id="user-a", memory_id=memory_id)
    assert before is not None

    store.record_memory_usage(
        owner_id="user-a",
        run_id="run-1",
        memories=selected,
    )
    used = store.get_memory(owner_id="user-a", memory_id=memory_id)
    assert used is not None and used.access_count == 1
    store.complete_memory_usage(run_id="run-1", outcome="failed")
    after = store.get_memory(owner_id="user-a", memory_id=memory_id)
    assert after is not None
    assert after.utility_score < before.utility_score

    episode_id = store.record_episode(
        owner_id="user-a",
        thread_id="web:long-thread",
        run_id="run-2",
        task="重新生成空间照片",
        answer="模型加载失败",
        status="failed",
        tool_names=["create_spatial_scene"],
    )
    assert episode_id is not None
    episode = store.get_memory(owner_id="user-a", memory_id=episode_id)
    assert episode is not None
    assert episode.memory_type == "episode"
    assert episode.metadata["status"] == "failed"


def test_summary_rebuilds_when_resumed_run_updates_old_message(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    thread_id = "web:resume"
    store.upsert_assistant_run_message(
        owner_id="user-a",
        thread_id=thread_id,
        run_id="run-approval",
        content="下一步尚未完成，等待批准",
        metadata={},
    )
    for index in range(5):
        store.append_message(
            owner_id="user-a",
            thread_id=thread_id,
            role="user",
            content=f"后续消息 {index}",
        )
    before = store.get_session_summary(owner_id="user-a", thread_id=thread_id)
    assert before is not None and "等待批准" in before.summary

    store.upsert_assistant_run_message(
        owner_id="user-a",
        thread_id=thread_id,
        run_id="run-approval",
        content="任务已完成并确认结果",
        metadata={},
    )

    after = store.get_session_summary(owner_id="user-a", thread_id=thread_id)
    assert after is not None
    assert "已完成" in after.summary
    assert "等待批准" not in after.summary


def test_memory_rejects_invalid_validity_window(tmp_path: Path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    with pytest.raises(ValueError, match="失效时间"):
        store.remember(
            owner_id="user-a",
            content="临时事实",
            source="web:test",
            valid_from=2_000,
            valid_to=1_000,
        )


def test_legacy_memory_database_migrates_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE agent_memories (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(owner_id, normalized_content)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_memories
            VALUES ('legacy-1', 'user-a', '旧记忆', '旧记忆', 'legacy', 10, 20)
            """
        )

    store = SQLiteMemoryStore(path)
    record = store.get_memory(owner_id="user-a", memory_id="legacy-1")
    assert record is not None
    assert record.content == "旧记忆"
    assert record.memory_type == "fact"
    assert record.status == "active"
    assert record.valid_from == 10
