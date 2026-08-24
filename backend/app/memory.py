from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


MemoryType = Literal[
    "profile",
    "preference",
    "fact",
    "task_state",
    "episode",
    "procedure",
    "asset_relation",
]
MemoryScope = Literal["user", "channel", "thread", "project"]
MemoryStatus = Literal["candidate", "active", "superseded", "archived"]
MemorySensitivity = Literal["normal", "private", "sensitive"]


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    owner_id: str
    memory_type: MemoryType
    scope: MemoryScope
    scope_id: str | None
    content: str
    topic_key: str | None
    source: str
    source_message_id: int | None
    source_run_id: str | None
    confidence: float
    importance: float
    sensitivity: MemorySensitivity
    valid_from: float
    valid_to: float | None
    status: MemoryStatus
    supersedes_id: str | None
    created_at: float
    updated_at: float
    last_accessed_at: float | None
    access_count: int
    utility_score: float
    metadata: dict[str, Any]
    relevance_score: float = 0.0

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.memory_type,
            "scope": self.scope,
            "content": self.content,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "importance": round(self.importance, 3),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "relevance_score": round(self.relevance_score, 4),
        }


@dataclass(frozen=True)
class SessionMemorySummary:
    summary: str
    open_loops: list[str]
    decisions: list[str]
    last_message_id: int
    updated_at: float


@dataclass(frozen=True)
class ConversationContext:
    owner_id: str
    thread_id: str
    messages: list[tuple[str, str]]
    memories: list[str]
    memory_items: list[MemoryRecord]
    session_summary: str = ""
    open_loops: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    recent_attachments: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConversationSummary:
    thread_id: str
    owner_id: str
    channel: str
    title: str
    created_at: float
    updated_at: float
    message_count: int


@dataclass(frozen=True)
class StoredConversationMessage:
    id: int
    role: str
    content: str
    created_at: float
    run_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class StoredAgentRun:
    run_id: str
    owner_id: str
    thread_id: str
    channel: str
    message: str
    status: str
    approval: dict[str, Any] | None
    response: dict[str, Any] | None
    created_at: float
    updated_at: float


class MemoryIsolationError(RuntimeError):
    """Raised when a thread is accessed by a different owner."""


class MemoryPolicyError(ValueError):
    """Raised when content is unsafe to persist as long-term memory."""


class SQLiteMemoryStore:
    """Owner-scoped conversation history and explicit long-term memories."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fts_available = False
        self._initialize()

    def context(
        self,
        *,
        owner_id: str,
        thread_id: str,
        channel: str | None = None,
        query: str = "",
        message_limit: int = 12,
        memory_limit: int = 8,
    ) -> ConversationContext:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
        )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)
        summary = self.get_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
        )
        summarized_through = summary.last_message_id if summary else 0
        with self._connect() as connection:
            message_rows = connection.execute(
                """
                SELECT role, content, metadata_json
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND id > ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    owner_id,
                    thread_id,
                    summarized_through,
                    max(1, min(message_limit, 50)),
                ),
            ).fetchall()
        recent_attachments: list[dict[str, Any]] = []
        for row in message_rows:
            if str(row[0]) != "user":
                continue
            try:
                metadata = json.loads(str(row[2] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                continue
            candidates = metadata.get("attachments", [])
            if not isinstance(candidates, list):
                candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                source_image_id = candidate.get("source_image_id")
                if not isinstance(source_image_id, str) or not source_image_id:
                    continue
                recent_attachments.append(
                    {
                        "id": source_image_id[:100],
                        "original_name": str(candidate.get("name", ""))[:160],
                        "width": candidate.get("width"),
                        "height": candidate.get("height"),
                    }
                )
            if recent_attachments:
                break
        memory_items = self.search_memories(
            owner_id=owner_id,
            query=query,
            thread_id=thread_id,
            channel=channel,
            limit=max(1, min(memory_limit, 20)),
            track_access=False,
        )
        return ConversationContext(
            owner_id=owner_id,
            thread_id=thread_id,
            messages=[
                (str(row[0]), str(row[1])) for row in reversed(message_rows)
            ],
            memories=[item.content for item in memory_items],
            memory_items=memory_items,
            session_summary=summary.summary if summary else "",
            open_loops=tuple(summary.open_loops if summary else []),
            decisions=tuple(summary.decisions if summary else []),
            recent_attachments=tuple(recent_attachments[:4]),
        )

    def ensure_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
        channel: str | None = None,
        title: str | None = None,
    ) -> None:
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT owner_id FROM agent_threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is not None and str(row[0]) != owner_id:
                raise MemoryIsolationError("该会话属于其他用户，无法访问。")
            safe_channel = (channel or "unknown").strip()[:40] or "unknown"
            safe_title = (title or "新对话").strip()[:80] or "新对话"
            connection.execute(
                """
                INSERT INTO agent_threads (
                    thread_id, owner_id, channel, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id)
                DO NOTHING
                """,
                (
                    thread_id,
                    owner_id,
                    safe_channel,
                    safe_title,
                    timestamp,
                    timestamp,
                ),
            )
            if row is not None and (channel is not None or title is not None):
                assignments: list[str] = []
                values: list[object] = []
                if channel is not None:
                    assignments.append("channel = ?")
                    values.append(safe_channel)
                if title is not None:
                    assignments.append("title = ?")
                    values.append(safe_title)
                values.extend((timestamp, thread_id, owner_id))
                connection.execute(
                    f"""
                    UPDATE agent_threads
                    SET {", ".join(assignments)}, updated_at = ?
                    WHERE thread_id = ? AND owner_id = ?
                    """,
                    values,
                )

    def list_threads(
        self,
        *,
        owner_id: str,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[ConversationSummary]:
        safe_limit = max(1, min(limit, 100))
        parameters: list[object] = [owner_id]
        channel_filter = ""
        if channel is not None:
            channel_filter = "AND threads.channel = ?"
            parameters.append(channel)
        parameters.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    threads.thread_id,
                    threads.owner_id,
                    threads.channel,
                    threads.title,
                    threads.created_at,
                    threads.updated_at,
                    COUNT(messages.id) AS message_count
                FROM agent_threads AS threads
                LEFT JOIN agent_messages AS messages
                    ON messages.thread_id = threads.thread_id
                    AND messages.owner_id = threads.owner_id
                WHERE threads.owner_id = ?
                {channel_filter}
                GROUP BY threads.thread_id
                ORDER BY threads.updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            ConversationSummary(
                thread_id=str(row[0]),
                owner_id=str(row[1]),
                channel=str(row[2]),
                title=str(row[3]),
                created_at=float(row[4]),
                updated_at=float(row[5]),
                message_count=int(row[6]),
            )
            for row in rows
        ]

    def get_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    threads.thread_id,
                    threads.owner_id,
                    threads.channel,
                    threads.title,
                    threads.created_at,
                    threads.updated_at,
                    COUNT(messages.id) AS message_count
                FROM agent_threads AS threads
                LEFT JOIN agent_messages AS messages
                    ON messages.thread_id = threads.thread_id
                    AND messages.owner_id = threads.owner_id
                WHERE threads.owner_id = ? AND threads.thread_id = ?
                GROUP BY threads.thread_id
                """,
                (owner_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        return ConversationSummary(
            thread_id=str(row[0]),
            owner_id=str(row[1]),
            channel=str(row[2]),
            title=str(row[3]),
            created_at=float(row[4]),
            updated_at=float(row[5]),
            message_count=int(row[6]),
        )

    def list_messages(
        self,
        *,
        owner_id: str,
        thread_id: str,
        limit: int = 200,
    ) -> list[StoredConversationMessage]:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        safe_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at, run_id, metadata_json
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (owner_id, thread_id, safe_limit),
            ).fetchall()
        messages: list[StoredConversationMessage] = []
        for row in reversed(rows):
            try:
                metadata = json.loads(str(row[5] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            messages.append(
                StoredConversationMessage(
                    id=int(row[0]),
                    role=str(row[1]),
                    content=str(row[2]),
                    created_at=float(row[3]),
                    run_id=str(row[4]) if row[4] is not None else None,
                    metadata=metadata,
                )
            )
        return messages

    def rename_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
        title: str,
    ) -> None:
        safe_title = title.strip()[:80]
        if not safe_title:
            raise ValueError("会话标题不能为空。")
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_threads
                SET title = ?, updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (safe_title, time.time(), owner_id, thread_id),
            )

    def delete_thread(self, *, owner_id: str, thread_id: str) -> int:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM agent_runs
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            deleted_messages = connection.execute(
                """
                DELETE FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).rowcount
            connection.execute(
                """
                DELETE FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            connection.execute(
                """
                DELETE FROM agent_threads
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
        return deleted_messages

    def append_message(
        self,
        *,
        owner_id: str,
        thread_id: str,
        role: str,
        content: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        safe_content = content.strip()[:12000]
        if not safe_content:
            return
        timestamp = time.time()
        safe_metadata = json.dumps(
            metadata or {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_messages (
                    owner_id, thread_id, role, content, created_at,
                    run_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_id,
                    thread_id,
                    role,
                    safe_content,
                    timestamp,
                    run_id,
                    safe_metadata,
                ),
            )
            connection.execute(
                """
                UPDATE agent_threads
                SET
                    title = CASE
                        WHEN ? = 'user' AND title = '新对话'
                        THEN substr(replace(replace(?, char(10), ' '), char(13), ' '), 1, 24)
                        ELSE title
                    END,
                    updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (role, safe_content, timestamp, owner_id, thread_id),
            )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)

    def upsert_assistant_run_message(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        safe_content = content.strip()[:12000]
        if not safe_content:
            return
        timestamp = time.time()
        safe_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND role = 'assistant' AND run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (owner_id, thread_id, run_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO agent_messages (
                        owner_id, thread_id, role, content, created_at,
                        run_id, metadata_json
                    ) VALUES (?, ?, 'assistant', ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        thread_id,
                        safe_content,
                        timestamp,
                        run_id,
                        safe_metadata,
                    ),
                )
            else:
                message_id = int(row[0])
                connection.execute(
                    """
                    UPDATE agent_messages
                    SET content = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (safe_content, safe_metadata, message_id),
                )
                connection.execute(
                    """
                    DELETE FROM agent_session_summaries
                    WHERE owner_id = ? AND thread_id = ?
                      AND last_message_id >= ?
                    """,
                    (owner_id, thread_id, message_id),
                )
            connection.execute(
                """
                UPDATE agent_threads
                SET updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (timestamp, owner_id, thread_id),
            )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)

    def create_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        thread_id: str,
        channel: str,
        message: str,
    ) -> None:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
        )
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            pending = connection.execute(
                """
                SELECT run_id FROM agent_runs
                WHERE thread_id = ?
                  AND status IN ('waiting_approval', 'resuming')
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if pending is not None:
                raise ValueError(
                    f"当前会话仍有待审批任务：{pending[0]}"
                )
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, owner_id, thread_id, channel, message,
                    status, approval_json, response_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    owner_id,
                    thread_id,
                    channel,
                    message[:4000],
                    timestamp,
                    timestamp,
                ),
            )

    def update_run(
        self,
        *,
        run_id: str,
        status: str,
        approval: dict[str, Any] | None,
        response: dict[str, Any],
    ) -> None:
        approval_json = (
            json.dumps(approval, ensure_ascii=False, separators=(",", ":"))
            if approval is not None
            else None
        )
        response_json = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, approval_json = ?, response_json = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    approval_json,
                    response_json,
                    time.time(),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("找不到这个 Agent Run。")

    def get_run(self, run_id: str) -> StoredAgentRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, owner_id, thread_id, channel, message,
                       status, approval_json, response_json,
                       created_at, updated_at
                FROM agent_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[StoredAgentRun]:
        safe_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    """
                    SELECT run_id, owner_id, thread_id, channel, message,
                           status, approval_json, response_json,
                           created_at, updated_at
                    FROM agent_runs
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT run_id, owner_id, thread_id, channel, message,
                           status, approval_json, response_json,
                           created_at, updated_at
                    FROM agent_runs
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (status, safe_limit),
                ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def begin_resume(self, *, run_id: str, owner_id: str | None) -> bool:
        with self._lock, self._connect() as connection:
            if owner_id is None:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND status = 'waiting_approval'
                    """,
                    (time.time(), run_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND owner_id = ?
                      AND status = 'waiting_approval'
                    """,
                    (time.time(), run_id, owner_id),
                )
            return cursor.rowcount == 1

    def remember(
        self,
        *,
        owner_id: str,
        content: str,
        source: str,
        memory_type: MemoryType | None = None,
        scope: MemoryScope = "user",
        scope_id: str | None = None,
        source_message_id: int | None = None,
        source_run_id: str | None = None,
        confidence: float = 1.0,
        importance: float = 0.65,
        sensitivity: MemorySensitivity | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        status: MemoryStatus = "active",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        safe_content = content.strip()[:2000]
        if not safe_content:
            raise ValueError("记忆内容不能为空。")
        if _contains_forbidden_secret(safe_content):
            raise MemoryPolicyError(
                "检测到密钥、密码或验证码特征，已拒绝写入长期记忆。"
            )
        safe_type = memory_type or _infer_memory_type(safe_content)
        if safe_type not in _MEMORY_TYPES:
            raise ValueError("记忆类型无效。")
        if scope not in _MEMORY_SCOPES:
            raise ValueError("记忆作用域无效。")
        if status not in _MEMORY_STATUSES:
            raise ValueError("记忆状态无效。")
        safe_scope = scope
        safe_scope_id = (
            (scope_id or "").strip()[:160] or None
            if safe_scope != "user"
            else None
        )
        if safe_scope != "user" and safe_scope_id is None:
            raise ValueError("非用户级记忆必须提供 scope_id。")
        safe_sensitivity = sensitivity or _infer_sensitivity(safe_content)
        if safe_sensitivity not in _MEMORY_SENSITIVITIES:
            raise ValueError("记忆敏感度无效。")
        normalized = re.sub(r"\s+", " ", safe_content).casefold()
        if safe_type == "episode" and source_run_id:
            normalized = f"{normalized}\u241f{source_run_id}"
        topic_key = _memory_topic_key(safe_content, safe_type)
        memory_id = str(uuid4())
        timestamp = time.time()
        safe_valid_from = timestamp if valid_from is None else float(valid_from)
        safe_valid_to = float(valid_to) if valid_to is not None else None
        if safe_valid_to is not None and safe_valid_to <= safe_valid_from:
            raise ValueError("记忆失效时间必须晚于生效时间。")
        safe_confidence = _clamp_score(confidence)
        safe_importance = _clamp_score(importance)
        safe_metadata = json.dumps(
            metadata or {}, ensure_ascii=False, separators=(",", ":")
        )
        with self._lock, self._connect() as connection:
            if source_run_id:
                source_row = connection.execute(
                    """
                    SELECT id FROM agent_memories
                    WHERE owner_id = ? AND source_run_id = ?
                    LIMIT 1
                    """,
                    (owner_id, source_run_id),
                ).fetchone()
                if source_row is not None:
                    return str(source_row[0])
            existing = connection.execute(
                """
                SELECT id FROM agent_memories
                WHERE owner_id = ? AND normalized_content = ?
                """,
                (owner_id, normalized),
            ).fetchone()
            if existing is not None:
                memory_id = str(existing[0])

            effective_status: MemoryStatus = status
            effective_valid_to = safe_valid_to
            supersedes_id: str | None = None
            if topic_key and status == "active":
                previous = connection.execute(
                    """
                    SELECT id, valid_from, content FROM agent_memories
                    WHERE owner_id = ? AND memory_type = ?
                      AND scope = ? AND COALESCE(scope_id, '') = COALESCE(?, '')
                      AND topic_key = ? AND status = 'active' AND id <> ?
                    ORDER BY valid_from DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (
                        owner_id,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        topic_key,
                        memory_id,
                    ),
                ).fetchone()
                if previous is not None:
                    previous_id = str(previous[0])
                    previous_valid_from = float(previous[1])
                    if safe_valid_from >= previous_valid_from:
                        supersedes_id = previous_id
                        connection.execute(
                            """
                            UPDATE agent_memories
                            SET status = 'superseded',
                                valid_to = CASE
                                    WHEN valid_to IS NULL OR valid_to > ? THEN ?
                                    ELSE valid_to
                                END,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                safe_valid_from,
                                safe_valid_from,
                                timestamp,
                                previous_id,
                            ),
                        )
                        self._delete_fts(connection, previous_id)
                        self._record_memory_event(
                            connection,
                            memory_id=previous_id,
                            owner_id=owner_id,
                            event_type="superseded",
                            content=str(previous[2]),
                            metadata={"superseded_by": memory_id},
                        )
                    else:
                        effective_status = "superseded"
                        if (
                            effective_valid_to is None
                            or effective_valid_to > previous_valid_from
                        ):
                            effective_valid_to = previous_valid_from
            if existing is not None:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET content = ?, memory_type = ?, scope = ?, scope_id = ?,
                        topic_key = ?, source = ?, source_message_id = ?,
                        source_run_id = COALESCE(?, source_run_id),
                        confidence = ?, importance = ?, sensitivity = ?,
                        valid_from = ?, valid_to = ?, status = ?,
                        supersedes_id = ?, metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        safe_content,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        topic_key,
                        source[:160],
                        source_message_id,
                        source_run_id,
                        safe_confidence,
                        safe_importance,
                        safe_sensitivity,
                        safe_valid_from,
                        effective_valid_to,
                        effective_status,
                        supersedes_id,
                        safe_metadata,
                        timestamp,
                        memory_id,
                    ),
                )
                event_type = "reinforced"
            else:
                connection.execute(
                    """
                    INSERT INTO agent_memories (
                        id, owner_id, content, normalized_content,
                        source, created_at, updated_at, memory_type,
                        scope, scope_id, topic_key, source_message_id,
                        source_run_id, confidence, importance, sensitivity,
                        valid_from, valid_to, status, supersedes_id,
                        metadata_json, last_accessed_at, access_count,
                        utility_score
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, NULL, 0, 0.5
                    )
                    """,
                    (
                        memory_id,
                        owner_id,
                        safe_content,
                        normalized,
                        source[:120],
                        timestamp,
                        timestamp,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        topic_key,
                        source_message_id,
                        source_run_id,
                        safe_confidence,
                        safe_importance,
                        safe_sensitivity,
                        safe_valid_from,
                        effective_valid_to,
                        effective_status,
                        supersedes_id,
                        safe_metadata,
                    ),
                )
                event_type = "created"
            if effective_status == "active":
                self._upsert_fts(
                    connection,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    content=safe_content,
                    topic_key=topic_key,
                )
            else:
                self._delete_fts(connection, memory_id)
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                owner_id=owner_id,
                event_type=event_type,
                content=safe_content,
                metadata={"source": source[:160], "type": safe_type},
            )
        return memory_id

    def list_memories(self, owner_id: str, limit: int = 50) -> list[str]:
        return [
            item.content
            for item in self.list_memory_records(
                owner_id=owner_id,
                limit=limit,
            )
        ]

    def list_memory_records(
        self,
        *,
        owner_id: str,
        memory_type: MemoryType | None = None,
        scope: MemoryScope | None = None,
        status: MemoryStatus | None = "active",
        query: str | None = None,
        limit: int | None = 100,
    ) -> list[MemoryRecord]:
        clauses = ["owner_id = ?"]
        parameters: list[object] = [owner_id]
        if memory_type is not None:
            clauses.append("memory_type = ?")
            parameters.append(memory_type)
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if query and query.strip():
            clauses.append("content LIKE ? ESCAPE '\\'")
            escaped = (
                query.strip()[:160]
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
        limit_clause = ""
        if limit is not None:
            parameters.append(max(1, min(limit, 500)))
            limit_clause = "LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY importance DESC, updated_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        return [self._memory_from_row(row) for row in rows]

    def get_memory(self, *, owner_id: str, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories
                WHERE owner_id = ? AND id = ?
                """,
                (owner_id, memory_id),
            ).fetchone()
        return self._memory_from_row(row) if row is not None else None

    def update_memory(
        self,
        *,
        owner_id: str,
        memory_id: str,
        updates: dict[str, Any],
    ) -> MemoryRecord:
        current = self.get_memory(owner_id=owner_id, memory_id=memory_id)
        if current is None:
            raise ValueError("找不到这条记忆。")
        allowed = {
            "content",
            "memory_type",
            "scope",
            "scope_id",
            "confidence",
            "importance",
            "sensitivity",
            "valid_from",
            "valid_to",
            "status",
            "metadata",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不支持更新字段：{sorted(unknown)[0]}")
        for field in {
            "content",
            "memory_type",
            "scope",
            "confidence",
            "importance",
            "sensitivity",
            "valid_from",
            "status",
            "metadata",
        }:
            if field in updates and updates[field] is None:
                raise ValueError(f"{field} 不能设为空。")
        content = str(updates.get("content", current.content)).strip()[:2000]
        if not content:
            raise ValueError("记忆内容不能为空。")
        if _contains_forbidden_secret(content):
            raise MemoryPolicyError(
                "检测到密钥、密码或验证码特征，已拒绝写入长期记忆。"
            )
        memory_type = updates.get("memory_type", current.memory_type)
        scope = updates.get("scope", current.scope)
        scope_id = updates.get("scope_id", current.scope_id)
        if memory_type not in _MEMORY_TYPES or scope not in _MEMORY_SCOPES:
            raise ValueError("记忆类型或作用域无效。")
        if scope == "user":
            scope_id = None
        elif not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("非用户级记忆必须提供 scope_id。")
        sensitivity = updates.get("sensitivity", current.sensitivity)
        status = updates.get("status", current.status)
        if sensitivity not in _MEMORY_SENSITIVITIES or status not in _MEMORY_STATUSES:
            raise ValueError("记忆敏感度或状态无效。")
        metadata = updates.get("metadata", current.metadata)
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须是对象。")
        next_valid_from = float(updates.get("valid_from", current.valid_from))
        next_valid_to = (
            (
                float(updates["valid_to"])
                if updates.get("valid_to") is not None
                else None
            )
            if "valid_to" in updates
            else current.valid_to
        )
        if next_valid_to is not None and next_valid_to <= next_valid_from:
            raise ValueError("记忆失效时间必须晚于生效时间。")
        values = (
            content,
            re.sub(r"\s+", " ", content).casefold(),
            memory_type,
            scope,
            str(scope_id).strip()[:160] if scope_id else None,
            _memory_topic_key(content, memory_type),
            _clamp_score(updates.get("confidence", current.confidence)),
            _clamp_score(updates.get("importance", current.importance)),
            sensitivity,
            next_valid_from,
            next_valid_to,
            status,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            time.time(),
            owner_id,
            memory_id,
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET content = ?, normalized_content = ?, memory_type = ?,
                        scope = ?, scope_id = ?, topic_key = ?, confidence = ?,
                        importance = ?, sensitivity = ?, valid_from = ?,
                        valid_to = ?, status = ?, metadata_json = ?, updated_at = ?
                    WHERE owner_id = ? AND id = ?
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("已经存在内容相同的记忆。") from exc
            if status == "active":
                self._upsert_fts(
                    connection,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    content=content,
                    topic_key=_memory_topic_key(content, memory_type),
                )
            else:
                self._delete_fts(connection, memory_id)
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                owner_id=owner_id,
                event_type="updated",
                content=content,
                metadata={"fields": sorted(updates)},
            )
        updated = self.get_memory(owner_id=owner_id, memory_id=memory_id)
        assert updated is not None
        return updated

    def delete_memory(self, *, owner_id: str, memory_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM agent_memories WHERE owner_id = ? AND id = ?",
                (owner_id, memory_id),
            ).fetchone()
            if row is None:
                return False
            self._delete_fts(connection, memory_id)
            connection.execute(
                "DELETE FROM agent_memory_usage WHERE owner_id = ? AND memory_id = ?",
                (owner_id, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memory_events WHERE owner_id = ? AND memory_id = ?",
                (owner_id, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memories WHERE owner_id = ? AND id = ?",
                (owner_id, memory_id),
            )
            return True

    def search_memories(
        self,
        *,
        owner_id: str,
        query: str,
        thread_id: str | None = None,
        channel: str | None = None,
        limit: int = 8,
        track_access: bool = False,
    ) -> list[MemoryRecord]:
        now = time.time()
        candidates = self.list_memory_records(
            owner_id=owner_id,
            status="active",
            limit=300,
        )
        candidates = [
            item
            for item in candidates
            if item.valid_from <= now
            and (item.valid_to is None or item.valid_to > now)
            and (
                item.scope == "user"
                or (item.scope == "thread" and item.scope_id == thread_id)
                or (item.scope == "channel" and item.scope_id == channel)
            )
        ]
        fts_ids = self._fts_candidate_ids(owner_id=owner_id, query=query)
        query_terms = _memory_terms(query)
        if query.strip() and not query_terms:
            return []
        scored: list[MemoryRecord] = []
        for item in candidates:
            item_terms = _memory_terms(item.content + " " + (item.topic_key or ""))
            overlap = len(query_terms & item_terms) / max(1, len(query_terms))
            substring = 1.0 if query.strip() and query.strip() in item.content else 0.0
            fts_score = 1.0 if item.id in fts_ids else 0.0
            if query_terms and overlap == 0 and substring == 0 and fts_score == 0:
                continue
            age_days = max(0.0, (now - item.updated_at) / 86_400)
            recency = math.exp(-age_days / 90.0)
            scope_bonus = 1.0 if item.scope == "thread" else 0.75
            if item.memory_type in {"profile", "preference", "procedure"}:
                scope_bonus = max(scope_bonus, 0.9)
            relevance = (
                0.31 * overlap
                + 0.10 * substring
                + 0.09 * fts_score
                + 0.12 * recency
                + 0.13 * item.importance
                + 0.10 * item.confidence
                + 0.09 * item.utility_score
                + 0.06 * scope_bonus
            )
            if not query_terms:
                relevance = (
                    0.30 * recency
                    + 0.28 * item.importance
                    + 0.20 * item.confidence
                    + 0.15 * item.utility_score
                    + 0.07 * scope_bonus
                )
            scored.append(
                MemoryRecord(
                    **{
                        **item.__dict__,
                        "relevance_score": round(relevance, 6),
                    }
                )
            )
        selected: list[MemoryRecord] = []
        seen: set[str] = set()
        for item in sorted(
            scored,
            key=lambda value: (value.relevance_score, value.updated_at),
            reverse=True,
        ):
            normalized = re.sub(r"\W+", "", item.content).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            selected.append(item)
            if len(selected) >= max(1, min(limit, 20)):
                break
        if track_access and selected:
            with self._lock, self._connect() as connection:
                for item in selected:
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET last_accessed_at = ?, access_count = access_count + 1
                        WHERE owner_id = ? AND id = ?
                        """,
                        (now, owner_id, item.id),
                    )
        return selected

    def record_memory_usage(
        self,
        *,
        owner_id: str,
        run_id: str,
        memories: list[MemoryRecord],
    ) -> None:
        if not memories:
            return
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            for rank, item in enumerate(memories, start=1):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_memory_usage (
                        memory_id, owner_id, run_id, rank, relevance_score,
                        outcome, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        item.id,
                        owner_id,
                        run_id,
                        rank,
                        item.relevance_score,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount:
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET last_accessed_at = ?, access_count = access_count + 1
                        WHERE owner_id = ? AND id = ?
                        """,
                        (timestamp, owner_id, item.id),
                    )

    def complete_memory_usage(self, *, run_id: str, outcome: str) -> None:
        delta = 0.04 if outcome == "completed" else -0.08
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, owner_id FROM agent_memory_usage
                WHERE run_id = ? AND outcome IS NULL
                """,
                (run_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE agent_memory_usage
                SET outcome = ?, updated_at = ?
                WHERE run_id = ? AND outcome IS NULL
                """,
                (outcome, timestamp, run_id),
            )
            for memory_id, owner_id in rows:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET utility_score = MIN(1.0, MAX(0.0, utility_score + ?))
                    WHERE id = ? AND owner_id = ?
                    """,
                    (delta, str(memory_id), str(owner_id)),
                )

    def record_episode(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        task: str,
        answer: str,
        status: str,
        tool_names: list[str],
    ) -> str | None:
        if not tool_names and status == "completed":
            return None
        tools = "、".join(dict.fromkeys(tool_names)) or "未调用工具"
        outcome = "成功" if status == "completed" else "失败"
        content = (
            f"任务：{task.strip()[:500]}\n"
            f"执行：{tools}\n"
            f"结果（{outcome}）：{answer.strip()[:700]}"
        )
        try:
            return self.remember(
                owner_id=owner_id,
                content=content,
                source=f"run:{run_id}",
                memory_type="episode",
                scope="user",
                source_run_id=run_id,
                confidence=1.0,
                importance=0.62 if status == "failed" else 0.55,
                metadata={
                    "thread_id": thread_id,
                    "status": status,
                    "tool_names": list(dict.fromkeys(tool_names)),
                },
            )
        except MemoryPolicyError:
            return None

    def get_session_summary(
        self,
        *,
        owner_id: str,
        thread_id: str,
    ) -> SessionMemorySummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, open_loops_json, decisions_json,
                       last_message_id, updated_at
                FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        return SessionMemorySummary(
            summary=str(row[0]),
            open_loops=_decode_string_list(row[1]),
            decisions=_decode_string_list(row[2]),
            last_message_id=int(row[3]),
            updated_at=float(row[4]),
        )

    def forget_all(self, owner_id: str) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM agent_memories WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
            for row in rows:
                self._delete_fts(connection, str(row[0]))
            connection.execute(
                "DELETE FROM agent_memory_usage WHERE owner_id = ?",
                (owner_id,),
            )
            connection.execute(
                "DELETE FROM agent_memory_events WHERE owner_id = ?",
                (owner_id,),
            )
            cursor = connection.execute(
                "DELETE FROM agent_memories WHERE owner_id = ?",
                (owner_id,),
            )
            return cursor.rowcount

    def clear_thread(self, *, owner_id: str, thread_id: str) -> int:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            cursor = connection.execute(
                """
                DELETE FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            return cursor.rowcount

    def _refresh_session_summary(self, *, owner_id: str, thread_id: str) -> None:
        """Maintain an extractive summary while preserving recent raw turns.

        The summary is intentionally deterministic and local. It treats all
        message text as untrusted data and never promotes it to a system rule.
        """

        with self._lock, self._connect() as connection:
            cutoff = connection.execute(
                """
                SELECT id FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                ORDER BY id DESC
                LIMIT 1 OFFSET 5
                """,
                (owner_id, thread_id),
            ).fetchone()
            if cutoff is None:
                return
            existing = connection.execute(
                """
                SELECT summary, open_loops_json, decisions_json, last_message_id
                FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).fetchone()
            previous_last_id = int(existing[3]) if existing is not None else 0
            cutoff_id = int(cutoff[0])
            if cutoff_id <= previous_last_id:
                return
            rows = connection.execute(
                """
                SELECT id, role, content
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (owner_id, thread_id, previous_last_id, cutoff_id),
            ).fetchall()
            if not rows:
                return

            lines = (
                str(existing[0]).splitlines() if existing is not None else []
            )
            open_loops = (
                _decode_string_list(existing[1]) if existing is not None else []
            )
            decisions = (
                _decode_string_list(existing[2]) if existing is not None else []
            )
            for _, role, raw_content in rows:
                content = re.sub(r"\s+", " ", str(raw_content)).strip()
                if not content:
                    continue
                clipped = content[:260] + ("…" if len(content) > 260 else "")
                label = "用户" if str(role) == "user" else "助手"
                lines.append(f"[{label}] {clipped}")
                if re.search(r"决定|确认|选择|采用|改为|同意|已完成", content):
                    decisions.append(clipped)
                if re.search(r"待办|下一步|还需|尚未|未完成|稍后|需要继续", content):
                    open_loops.append(clipped)

            lines = lines[-18:]
            while len("\n".join(lines)) > 3600 and len(lines) > 1:
                lines.pop(0)
            open_loops = _dedupe_strings(open_loops)[-8:]
            decisions = _dedupe_strings(decisions)[-8:]
            timestamp = time.time()
            connection.execute(
                """
                INSERT INTO agent_session_summaries (
                    owner_id, thread_id, summary, open_loops_json,
                    decisions_json, last_message_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, thread_id) DO UPDATE SET
                    summary = excluded.summary,
                    open_loops_json = excluded.open_loops_json,
                    decisions_json = excluded.decisions_json,
                    last_message_id = excluded.last_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_id,
                    thread_id,
                    "\n".join(lines),
                    json.dumps(open_loops, ensure_ascii=False),
                    json.dumps(decisions, ensure_ascii=False),
                    cutoff_id,
                    timestamp,
                ),
            )

    def _record_memory_event(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        owner_id: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO agent_memory_events (
                event_id, memory_id, owner_id, event_type,
                content_hash, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                memory_id,
                owner_id,
                event_type[:40],
                sha256(content.encode("utf-8")).hexdigest(),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            ),
        )

    def _upsert_fts(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        owner_id: str,
        content: str,
        topic_key: str | None,
    ) -> None:
        if not self._fts_available:
            return
        self._delete_fts(connection, memory_id)
        connection.execute(
            """
            INSERT INTO agent_memory_fts (
                memory_id, owner_id, content, topic_key
            ) VALUES (?, ?, ?, ?)
            """,
            (memory_id, owner_id, content, topic_key or ""),
        )

    def _delete_fts(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> None:
        if self._fts_available:
            connection.execute(
                "DELETE FROM agent_memory_fts WHERE memory_id = ?",
                (memory_id,),
            )

    def _fts_candidate_ids(self, *, owner_id: str, query: str) -> set[str]:
        if not self._fts_available:
            return set()
        terms = sorted(_memory_terms(query), key=len, reverse=True)[:12]
        if not terms:
            return set()
        match_query = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT memory_id FROM agent_memory_fts
                    WHERE owner_id = ? AND agent_memory_fts MATCH ?
                    LIMIT 100
                    """,
                    (owner_id, match_query),
                ).fetchall()
            return {str(row[0]) for row in rows}
        except sqlite3.OperationalError:
            return set()

    @staticmethod
    def _memory_from_row(row: sqlite3.Row | tuple[Any, ...]) -> MemoryRecord:
        try:
            metadata = json.loads(str(row[22] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryRecord(
            id=str(row[0]),
            owner_id=str(row[1]),
            memory_type=str(row[2]),  # type: ignore[arg-type]
            scope=str(row[3]),  # type: ignore[arg-type]
            scope_id=str(row[4]) if row[4] is not None else None,
            content=str(row[5]),
            topic_key=str(row[6]) if row[6] is not None else None,
            source=str(row[7]),
            source_message_id=int(row[8]) if row[8] is not None else None,
            source_run_id=str(row[9]) if row[9] is not None else None,
            confidence=float(row[10]),
            importance=float(row[11]),
            sensitivity=str(row[12]),  # type: ignore[arg-type]
            valid_from=float(row[13]),
            valid_to=float(row[14]) if row[14] is not None else None,
            status=str(row[15]),  # type: ignore[arg-type]
            supersedes_id=str(row[16]) if row[16] is not None else None,
            created_at=float(row[17]),
            updated_at=float(row[18]),
            last_accessed_at=float(row[19]) if row[19] is not None else None,
            access_count=int(row[20]),
            utility_score=float(row[21]),
            metadata=metadata,
        )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'unknown',
                    title TEXT NOT NULL DEFAULT '新对话',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(thread_id) REFERENCES agent_threads(thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'user',
                    scope_id TEXT,
                    topic_key TEXT,
                    source_message_id INTEGER,
                    source_run_id TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.65,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    valid_from REAL NOT NULL DEFAULT 0,
                    valid_to REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    supersedes_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    last_accessed_at REAL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    utility_score REAL NOT NULL DEFAULT 0.5,
                    UNIQUE(owner_id, normalized_content)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_json TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES agent_threads(thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_session_summaries (
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    open_loops_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_events (
                    event_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_memory_usage (
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    relevance_score REAL NOT NULL,
                    outcome TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(memory_id, run_id)
                );

                CREATE INDEX IF NOT EXISTS agent_messages_thread_idx
                ON agent_messages(owner_id, thread_id, id DESC);

                CREATE INDEX IF NOT EXISTS agent_memories_owner_idx
                ON agent_memories(owner_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_runs_status_idx
                ON agent_runs(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_runs_thread_idx
                ON agent_runs(thread_id, updated_at DESC);
                """
            )
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'waiting_approval'
                WHERE status = 'resuming'
                """
            )
            thread_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_threads)"
                ).fetchall()
            }
            if "channel" not in thread_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_threads
                    ADD COLUMN channel TEXT NOT NULL DEFAULT 'unknown'
                    """
                )
            if "title" not in thread_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_threads
                    ADD COLUMN title TEXT NOT NULL DEFAULT '新对话'
                    """
                )
            message_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_messages)"
                ).fetchall()
            }
            if "run_id" not in message_columns:
                connection.execute(
                    "ALTER TABLE agent_messages ADD COLUMN run_id TEXT"
                )
            if "metadata_json" not in message_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_messages
                    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'
                    """
                )
            memory_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_memories)"
                ).fetchall()
            }
            memory_migrations = {
                "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "scope_id": "TEXT",
                "topic_key": "TEXT",
                "source_message_id": "INTEGER",
                "source_run_id": "TEXT",
                "confidence": "REAL NOT NULL DEFAULT 1.0",
                "importance": "REAL NOT NULL DEFAULT 0.65",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "valid_from": "REAL NOT NULL DEFAULT 0",
                "valid_to": "REAL",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "supersedes_id": "TEXT",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_accessed_at": "REAL",
                "access_count": "INTEGER NOT NULL DEFAULT 0",
                "utility_score": "REAL NOT NULL DEFAULT 0.5",
            }
            for name, declaration in memory_migrations.items():
                if name not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE agent_memories ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                UPDATE agent_memories
                SET valid_from = created_at
                WHERE valid_from = 0
                """
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS agent_memories_lookup_idx
                ON agent_memories(
                    owner_id, status, memory_type, scope, scope_id, updated_at DESC
                );

                CREATE INDEX IF NOT EXISTS agent_memories_topic_idx
                ON agent_memories(owner_id, topic_key, status);

                CREATE UNIQUE INDEX IF NOT EXISTS agent_memories_source_run_idx
                ON agent_memories(owner_id, source_run_id)
                WHERE source_run_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS agent_memory_usage_run_idx
                ON agent_memory_usage(run_id, outcome);

                CREATE INDEX IF NOT EXISTS agent_memory_events_item_idx
                ON agent_memory_events(owner_id, memory_id, created_at DESC);
                """
            )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts
                    USING fts5(
                        memory_id UNINDEXED,
                        owner_id UNINDEXED,
                        content,
                        topic_key,
                        tokenize='unicode61 remove_diacritics 2'
                    )
                    """
                )
                self._fts_available = True
                connection.execute("DELETE FROM agent_memory_fts")
                connection.execute(
                    """
                    INSERT INTO agent_memory_fts (
                        memory_id, owner_id, content, topic_key
                    )
                    SELECT id, owner_id, content, COALESCE(topic_key, '')
                    FROM agent_memories
                    WHERE status = 'active'
                    """
                )
            except sqlite3.OperationalError:
                self._fts_available = False
            connection.execute(
                """
                UPDATE agent_threads
                SET channel = CASE
                    WHEN thread_id LIKE 'web:%' THEN 'web'
                    WHEN thread_id LIKE 'feishu:%' THEN 'feishu'
                    ELSE channel
                END
                WHERE channel = 'unknown'
                """
            )
            connection.execute(
                """
                UPDATE agent_threads
                SET title = COALESCE(
                    (
                        SELECT substr(
                            replace(replace(content, char(10), ' '), char(13), ' '),
                            1,
                            24
                        )
                        FROM agent_messages
                        WHERE agent_messages.thread_id = agent_threads.thread_id
                          AND agent_messages.owner_id = agent_threads.owner_id
                          AND role = 'user'
                        ORDER BY id ASC
                        LIMIT 1
                    ),
                    '新对话'
                )
                WHERE title = '新对话'
                """
            )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _run_from_row(row: sqlite3.Row | tuple[Any, ...]) -> StoredAgentRun:
        def decode(raw: Any) -> dict[str, Any] | None:
            if raw is None:
                return None
            try:
                value = json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None

        return StoredAgentRun(
            run_id=str(row[0]),
            owner_id=str(row[1]),
            thread_id=str(row[2]),
            channel=str(row[3]),
            message=str(row[4]),
            status=str(row[5]),
            approval=decode(row[6]),
            response=decode(row[7]),
            created_at=float(row[8]),
            updated_at=float(row[9]),
        )


_MEMORY_TYPES = {
    "profile",
    "preference",
    "fact",
    "task_state",
    "episode",
    "procedure",
    "asset_relation",
}
_MEMORY_SCOPES = {"user", "channel", "thread", "project"}
_MEMORY_STATUSES = {"candidate", "active", "superseded", "archived"}
_MEMORY_SENSITIVITIES = {"normal", "private", "sensitive"}
_MEMORY_SELECT_COLUMNS = """
    id, owner_id, memory_type, scope, scope_id, content, topic_key,
    source, source_message_id, source_run_id, confidence, importance,
    sensitivity, valid_from, valid_to, status, supersedes_id,
    created_at, updated_at, last_accessed_at, access_count,
    utility_score, metadata_json
"""


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.5
    return min(max(score, 0.0), 1.0)


def _infer_memory_type(content: str) -> MemoryType:
    if re.search(r"偏好|喜欢|希望|优先|默认|习惯", content):
        return "preference"
    if re.search(r"待办|下一步|截止|任务状态|进行中|已完成", content):
        return "task_state"
    if re.search(r"以后每次|操作流程|先.+再|遇到.+就", content):
        return "procedure"
    if re.search(r"我的|我是|我住|我在|联系方式", content):
        return "profile"
    return "fact"


def _infer_sensitivity(content: str) -> MemorySensitivity:
    if re.search(r"身份证|银行卡|病历|家庭住址|手机号|邮箱", content):
        return "sensitive"
    if re.search(r"私人|隐私|住址|生日|家庭|健康", content):
        return "private"
    return "normal"


def _contains_forbidden_secret(content: str) -> bool:
    if re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", content):
        return True
    if re.search(
        r"(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
        r"password|密码|密钥|验证码)\s*[:：=]\s*[^\s，。,;；]{4,}",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _memory_topic_key(content: str, memory_type: str) -> str | None:
    if memory_type != "profile":
        return None
    normalized = re.sub(r"\s+", "", content).casefold()
    location = re.search(
        r"(?:我)?(?:现在)?(?:住在|居住在|所在地是|位置是)([^，。；;]{1,40})",
        normalized,
    )
    if location:
        return "profile:location"
    attribute = re.search(
        r"我的([^，。；;]{1,20}?)(?:是|为|改为|改成)([^，。；;]{1,60})",
        normalized,
    )
    if attribute:
        return f"profile:{attribute.group(1)[:20]}"
    return None


def _memory_terms(value: str) -> set[str]:
    lowered = value.casefold().replace("_", " ")
    terms = set(re.findall(r"[a-z0-9][a-z0-9-]{1,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) <= 8:
            terms.add(chunk)
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    synonym_groups = {
        "preference": {"偏好", "喜欢", "习惯", "希望", "优先", "默认"},
        "location": {"住在", "居住", "地址", "位置", "所在地"},
        "display": {"界面", "主题", "颜色", "外观"},
        "image": {"图片", "照片", "图像"},
        "workflow": {"流程", "步骤", "操作", "方法"},
        "failure": {"失败", "错误", "异常", "问题"},
    }
    for canonical, synonyms in synonym_groups.items():
        if any(term in lowered for term in synonyms):
            terms.add(canonical)
    return {term for term in terms if term}


def _decode_string_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result
