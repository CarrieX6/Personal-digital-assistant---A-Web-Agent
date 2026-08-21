from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ConversationContext:
    owner_id: str
    thread_id: str
    messages: list[tuple[str, str]]
    memories: list[str]


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


class SQLiteMemoryStore:
    """Owner-scoped conversation history and explicit long-term memories."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._initialize()

    def context(
        self,
        *,
        owner_id: str,
        thread_id: str,
        channel: str | None = None,
        message_limit: int = 12,
        memory_limit: int = 20,
    ) -> ConversationContext:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
        )
        with self._connect() as connection:
            message_rows = connection.execute(
                """
                SELECT role, content
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (owner_id, thread_id, max(1, min(message_limit, 50))),
            ).fetchall()
            memory_rows = connection.execute(
                """
                SELECT content
                FROM agent_memories
                WHERE owner_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (owner_id, max(1, min(memory_limit, 100))),
            ).fetchall()
        return ConversationContext(
            owner_id=owner_id,
            thread_id=thread_id,
            messages=[
                (str(row[0]), str(row[1])) for row in reversed(message_rows)
            ],
            memories=[str(row[0]) for row in memory_rows],
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
                connection.execute(
                    """
                    UPDATE agent_messages
                    SET content = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (safe_content, safe_metadata, int(row[0])),
                )
            connection.execute(
                """
                UPDATE agent_threads
                SET updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (timestamp, owner_id, thread_id),
            )

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
    ) -> str:
        safe_content = content.strip()[:2000]
        if not safe_content:
            raise ValueError("记忆内容不能为空。")
        normalized = re.sub(r"\s+", " ", safe_content).casefold()
        memory_id = str(uuid4())
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM agent_memories
                WHERE owner_id = ? AND normalized_content = ?
                """,
                (owner_id, normalized),
            ).fetchone()
            if existing is not None:
                memory_id = str(existing[0])
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET content = ?, source = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (safe_content, source[:120], timestamp, memory_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO agent_memories (
                        id, owner_id, content, normalized_content,
                        source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        owner_id,
                        safe_content,
                        normalized,
                        source[:120],
                        timestamp,
                        timestamp,
                    ),
                )
        return memory_id

    def list_memories(self, owner_id: str, limit: int = 50) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT content FROM agent_memories
                WHERE owner_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (owner_id, max(1, min(limit, 100))),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def forget_all(self, owner_id: str) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_memories WHERE owner_id = ?",
                (owner_id,),
            )
            return cursor.rowcount

    def clear_thread(self, *, owner_id: str, thread_id: str) -> int:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            return cursor.rowcount

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
