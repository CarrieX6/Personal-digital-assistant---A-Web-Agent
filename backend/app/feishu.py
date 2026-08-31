from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from lark_channel import (
    FeishuChannel,
    KeepaliveConfig,
    LogLevel,
    OutboundConfig,
    PolicyConfig,
    RetryConfig,
    SecurityConfig,
    TransportConfig,
    new_card,
)

from .agent import AgentRunError, AgentRunner
from .assets import AssetError, SpatialSceneService
from .channel_settings import FeishuSettingsService, StoredFeishuSettings
from .identity import IdentityBindingError, IdentityBindingRegistry
from .lan_viewer import ViewerLinkError
from .models import ChannelMessagePublic, FeishuRuntimePublic
from .style_transfer import PhotoStyleService


class FeishuChannelLike(Protocol):
    def on(self, name: str, handler: Callable[..., Any]) -> Any: ...

    async def connect_until_ready(
        self,
        *,
        timeout: float | None = 30.0,
    ) -> None: ...

    async def disconnect(self) -> None: ...

    async def send(
        self,
        to: str,
        message: dict[str, Any],
        opts: dict[str, Any] | None = None,
    ) -> Any: ...

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None: ...


class FeishuResourceError(AssetError):
    """User-actionable media failure without exposing credentials."""


@dataclass(frozen=True)
class MediaCollectionSession:
    id: str
    capability_id: str
    owner_id: str
    chat_id: str
    sender_id: str
    state: str
    content_image_id: str | None
    style_image_ids: tuple[str, ...]
    job_id: str | None
    expires_at: float


class SQLiteChannelStore:
    """Persistent SDK dedup and message execution ledger without message bodies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._initialize()

    def seen(self, key: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM sdk_dedup WHERE dedup_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return False
            if float(row[0]) <= now:
                connection.execute(
                    "DELETE FROM sdk_dedup WHERE dedup_key = ?",
                    (key,),
                )
                return False
            return True

    def mark(self, key: str, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sdk_dedup (dedup_key, expires_at)
                VALUES (?, ?)
                ON CONFLICT(dedup_key)
                DO UPDATE SET expires_at = excluded.expires_at
                """,
                (key, expires_at),
            )
            connection.execute(
                "DELETE FROM sdk_dedup WHERE expires_at <= ?",
                (time.time(),),
            )

    def claim_message(
        self,
        message_id: str,
        sender_id: str,
        chat_id: str,
    ) -> bool:
        now = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO channel_messages (
                    message_id, sender_id, chat_id, status,
                    run_id, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, 'accepted', NULL, NULL, ?, ?)
                """,
                (message_id, sender_id, chat_id, now, now),
            )
            return cursor.rowcount == 1

    def mark_completed(self, message_id: str, run_id: str) -> None:
        self._mark(message_id, status="completed", run_id=run_id)

    def mark_rejected(self, message_id: str, error_code: str) -> None:
        self._mark(message_id, status="rejected", error_code=error_code)

    def mark_failed(self, message_id: str, error_code: str) -> None:
        self._mark(message_id, status="failed", error_code=error_code)

    def record_event(
        self,
        *,
        chat_id: str,
        sender_id: str | None,
        direction: str,
        kind: str,
        content: str,
        message_id: str | None = None,
        media_url: str | None = None,
    ) -> None:
        safe_content = content.strip()[:4000] or "[空消息]"
        safe_media_url = (
            media_url[:1000]
            if media_url and media_url.startswith("/api/assets/")
            else None
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO channel_events (
                    platform, chat_id, sender_id, direction,
                    kind, content, message_id, media_url, created_at
                ) VALUES ('feishu', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    sender_id,
                    direction,
                    kind,
                    safe_content,
                    message_id,
                    safe_media_url,
                    time.time(),
                ),
            )
            connection.execute(
                """
                DELETE FROM channel_events
                WHERE id NOT IN (
                    SELECT id FROM channel_events
                    ORDER BY id DESC LIMIT 500
                )
                """
            )

    def list_events(self, limit: int = 100) -> list[ChannelMessagePublic]:
        safe_limit = max(1, min(limit, 200))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, platform, chat_id, sender_id, direction,
                       kind, content, media_url, created_at
                FROM channel_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            ChannelMessagePublic(
                id=int(row[0]),
                platform=row[1],
                chat_id=row[2],
                sender_id=row[3],
                direction=row[4],
                kind=row[5],
                content=row[6],
                media_url=row[7],
                created_at=datetime.fromtimestamp(
                    float(row[8]),
                    tz=timezone.utc,
                ),
            )
            for row in reversed(rows)
        ]

    def clear_events(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM channel_events")

    def attach_event_media(self, message_id: str, media_url: str) -> bool:
        if not media_url.startswith("/api/assets/"):
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_events
                SET media_url = ?
                WHERE id = (
                    SELECT id FROM channel_events
                    WHERE message_id = ? AND kind = 'image'
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (media_url[:1000], message_id),
            )
            return cursor.rowcount == 1

    def delete_chat_events(self, chat_id: str) -> bool:
        """Delete only the local read-only mirror, never Feishu messages."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM channel_events WHERE chat_id = ?",
                (chat_id,),
            )
            return cursor.rowcount > 0

    def claim_feature_menu(self, chat_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO channel_feature_menu (chat_id, sent_at)
                VALUES (?, ?)
                """,
                (chat_id, time.time()),
            )
            return cursor.rowcount == 1

    def release_feature_menu(self, chat_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM channel_feature_menu WHERE chat_id = ?",
                (chat_id,),
            )

    def start_media_session(
        self,
        *,
        capability_id: str,
        owner_id: str,
        chat_id: str,
        sender_id: str,
        ttl_seconds: int = 15 * 60,
    ) -> tuple[MediaCollectionSession, MediaCollectionSession | None]:
        """Replace one user's active media flow and return the superseded flow."""
        now = time.time()
        state = (
            "awaiting_content"
            if capability_id == "photo_style_transfer"
            else "awaiting_source"
        )
        session = MediaCollectionSession(
            id=str(uuid4()),
            capability_id=capability_id,
            owner_id=owner_id,
            chat_id=chat_id,
            sender_id=sender_id,
            state=state,
            content_image_id=None,
            style_image_ids=(),
            job_id=None,
            expires_at=now + ttl_seconds,
        )
        with self._lock, self._connect() as connection:
            previous_row = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions
                WHERE owner_id = ? AND chat_id = ?
                """,
                (owner_id, chat_id),
            ).fetchone()
            previous = self._media_session_from_row(previous_row)
            connection.execute(
                "DELETE FROM channel_media_sessions WHERE owner_id = ? AND chat_id = ?",
                (owner_id, chat_id),
            )
            connection.execute(
                """
                INSERT INTO channel_media_sessions (
                    session_id, capability_id, owner_id, chat_id, sender_id,
                    state, content_image_id, style_image_ids_json, job_id,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, '[]', NULL, ?, ?, ?)
                """,
                (
                    session.id,
                    capability_id,
                    owner_id,
                    chat_id,
                    sender_id,
                    state,
                    session.expires_at,
                    now,
                    now,
                ),
            )
        return session, previous

    def get_media_session(
        self,
        *,
        owner_id: str,
        chat_id: str,
    ) -> MediaCollectionSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions
                WHERE owner_id = ? AND chat_id = ?
                """,
                (owner_id, chat_id),
            ).fetchone()
            session = self._media_session_from_row(row)
            if session is not None and session.expires_at <= time.time():
                connection.execute(
                    "DELETE FROM channel_media_sessions WHERE session_id = ?",
                    (session.id,),
                )
                return None
            if session is not None and session.state in {
                "completed",
                "failed",
                "cancelled",
            }:
                connection.execute(
                    "DELETE FROM channel_media_sessions WHERE session_id = ?",
                    (session.id,),
                )
                return None
            return session

    def add_media_session_image(
        self,
        *,
        session_id: str,
        owner_id: str,
        source_image_id: str,
    ) -> MediaCollectionSession:
        """Atomically assign a sanitized image to its declared session role."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions
                WHERE session_id = ? AND owner_id = ?
                """,
                (session_id, owner_id),
            ).fetchone()
            session = self._media_session_from_row(row)
            if session is None or session.expires_at <= time.time():
                raise ValueError("图片收集会话已过期，请重新选择功能。")
            now = time.time()
            if session.state == "awaiting_content":
                connection.execute(
                    """
                    UPDATE channel_media_sessions
                    SET content_image_id = ?, state = 'awaiting_styles',
                        updated_at = ?
                    WHERE session_id = ? AND owner_id = ?
                      AND state = 'awaiting_content'
                    """,
                    (source_image_id, now, session_id, owner_id),
                )
            elif session.state in {"awaiting_styles", "ready"}:
                style_ids = list(session.style_image_ids)
                if len(style_ids) >= 3:
                    raise ValueError("最多只能添加三张风格参考图。")
                style_ids.append(source_image_id)
                connection.execute(
                    """
                    UPDATE channel_media_sessions
                    SET style_image_ids_json = ?, state = 'ready',
                        updated_at = ?
                    WHERE session_id = ? AND owner_id = ?
                      AND state IN ('awaiting_styles', 'ready')
                    """,
                    (json.dumps(style_ids), now, session_id, owner_id),
                )
            else:
                raise ValueError("当前会话不再接收图片。")
            refreshed = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        result = self._media_session_from_row(refreshed)
        if result is None:
            raise ValueError("图片收集会话不存在。")
        return result

    def claim_media_session(
        self,
        *,
        session_id: str,
        owner_id: str,
        chat_id: str,
    ) -> MediaCollectionSession | None:
        now = time.time()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE channel_media_sessions
                SET state = 'submitted', updated_at = ?
                WHERE session_id = ? AND owner_id = ? AND chat_id = ?
                  AND state = 'ready' AND expires_at > ?
                """,
                (now, session_id, owner_id, chat_id, now),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._media_session_from_row(row)

    def set_media_session_job(
        self,
        *,
        session_id: str,
        owner_id: str,
        job_id: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE channel_media_sessions
                SET job_id = ?, updated_at = ?
                WHERE session_id = ? AND owner_id = ?
                """,
                (job_id, time.time(), session_id, owner_id),
            )

    def finish_media_session(
        self,
        *,
        session_id: str,
        owner_id: str,
        state: str,
    ) -> None:
        if state not in {"ready", "completed", "failed", "cancelled"}:
            raise ValueError("无效的媒体会话终态。")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE channel_media_sessions
                SET state = ?, updated_at = ?
                WHERE session_id = ? AND owner_id = ?
                """,
                (state, time.time(), session_id, owner_id),
            )

    def cancel_media_session(
        self,
        *,
        session_id: str,
        owner_id: str,
        chat_id: str,
    ) -> MediaCollectionSession | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, capability_id, owner_id, chat_id,
                       sender_id, state, content_image_id,
                       style_image_ids_json, job_id, expires_at
                FROM channel_media_sessions
                WHERE session_id = ? AND owner_id = ? AND chat_id = ?
                """,
                (session_id, owner_id, chat_id),
            ).fetchone()
            session = self._media_session_from_row(row)
            if session is not None and session.state != "submitted":
                connection.execute(
                    "DELETE FROM channel_media_sessions WHERE session_id = ?",
                    (session_id,),
                )
            return session

    @staticmethod
    def _media_session_from_row(
        row: sqlite3.Row | tuple[Any, ...] | None,
    ) -> MediaCollectionSession | None:
        if row is None:
            return None
        try:
            style_ids = json.loads(str(row[7]) or "[]")
        except (TypeError, ValueError):
            style_ids = []
        return MediaCollectionSession(
            id=str(row[0]),
            capability_id=str(row[1]),
            owner_id=str(row[2]),
            chat_id=str(row[3]),
            sender_id=str(row[4]),
            state=str(row[5]),
            content_image_id=(str(row[6]) if row[6] else None),
            style_image_ids=tuple(
                str(item) for item in style_ids if isinstance(item, str)
            ),
            job_id=(str(row[8]) if row[8] else None),
            expires_at=float(row[9]),
        )

    def _mark(
        self,
        message_id: str,
        *,
        status: str,
        run_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE channel_messages
                SET status = ?, run_id = ?, error_code = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (status, run_id, error_code, time.time(), message_id),
            )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sdk_dedup (
                    dedup_key TEXT PRIMARY KEY,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_messages (
                    message_id TEXT PRIMARY KEY,
                    sender_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    error_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT,
                    direction TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    message_id TEXT,
                    media_url TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_feature_menu (
                    chat_id TEXT PRIMARY KEY,
                    sent_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channel_media_sessions (
                    session_id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    content_image_id TEXT,
                    style_image_ids_json TEXT NOT NULL DEFAULT '[]',
                    job_id TEXT,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(owner_id, chat_id)
                );

                CREATE INDEX IF NOT EXISTS channel_media_sessions_expiry_idx
                ON channel_media_sessions(expires_at);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(channel_events)"
                ).fetchall()
            }
            if "message_id" not in columns:
                connection.execute(
                    "ALTER TABLE channel_events ADD COLUMN message_id TEXT"
                )
            if "media_url" not in columns:
                connection.execute(
                    "ALTER TABLE channel_events ADD COLUMN media_url TEXT"
                )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


ChannelFactory = Callable[..., FeishuChannelLike]


class FeishuChannelRuntime:
    max_message_chars = 4000
    max_reply_chars = 8000

    def __init__(
        self,
        settings_service: FeishuSettingsService,
        runner: AgentRunner,
        store: SQLiteChannelStore,
        channel_factory: ChannelFactory = FeishuChannel,
        spatial_service: SpatialSceneService | None = None,
        style_service: PhotoStyleService | None = None,
        viewer_link_factory: Callable[[str], str] | None = None,
        identity_registry: IdentityBindingRegistry | None = None,
        job_poll_interval: float = 2,
        job_timeout_seconds: float = 10 * 60,
        background_job_timeout_seconds: float = 60 * 60,
    ) -> None:
        self.settings_service = settings_service
        self.runner = runner
        self.store = store
        self.channel_factory = channel_factory
        self.spatial_service = spatial_service
        self.style_service = style_service
        self.viewer_link_factory = viewer_link_factory
        self.identity_registry = identity_registry
        self.job_poll_interval = job_poll_interval
        self.job_timeout_seconds = job_timeout_seconds
        self.background_job_timeout_seconds = background_job_timeout_seconds
        self._channel: FeishuChannelLike | None = None
        self._settings = settings_service.load()
        self._status = "disabled"
        self._last_error: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()

    def public_status(self) -> FeishuRuntimePublic:
        snapshot_method = getattr(self._channel, "connection_snapshot", None)
        snapshot = snapshot_method() if callable(snapshot_method) else None
        connected_at = getattr(snapshot, "last_connected_at", None)
        return FeishuRuntimePublic(
            status=self._status,
            last_error=self._last_error,
            reconnect_attempts=int(
                getattr(snapshot, "reconnect_attempts", 0) or 0
            ),
            last_connected_at=(
                datetime.fromtimestamp(float(connected_at), tz=timezone.utc)
                if connected_at
                else None
            ),
        )

    async def start_if_enabled(self) -> None:
        await self.apply_settings(self.settings_service.load())

    async def apply_settings(self, settings: StoredFeishuSettings) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()
            self._settings = settings
            self._last_error = None
            if self.identity_registry is not None and settings.app_id:
                try:
                    for open_id in settings.allowed_open_ids:
                        self.identity_registry.ensure_feishu_binding(
                            app_id=settings.app_id,
                            open_id=open_id,
                        )
                except IdentityBindingError as exc:
                    self._status = "error"
                    self._last_error = str(exc)
                    return
            if not settings.enabled:
                self._status = "disabled"
                return

            app_secret = self.settings_service.app_secret()
            if not settings.app_id or not app_secret:
                self._status = "error"
                self._last_error = "缺少 App ID 或 App Secret。"
                return

            self._status = "starting"
            try:
                channel = self.channel_factory(
                    app_id=settings.app_id,
                    app_secret=app_secret,
                    domain=FeishuSettingsService.domain_urls[settings.domain],
                    log_level=LogLevel.WARNING,
                    transport=TransportConfig(
                        kind="ws",
                        auto_reconnect=True,
                        keepalive=KeepaliveConfig(
                            enabled=True,
                            check_interval_seconds=30,
                            wake_threshold_seconds=90,
                            probe_timeout_seconds=5,
                            failure_threshold=2,
                        ),
                    ),
                    outbound=OutboundConfig(
                        retry=RetryConfig(max_attempts=4, base_delay_ms=500),
                    ),
                    policy=PolicyConfig(
                        dm_policy="open",
                        group_policy=(
                            "open" if settings.allow_group_mentions else "disabled"
                        ),
                        require_mention=True,
                    ),
                    security=SecurityConfig(
                        mode="strict",
                        strict_content_text=True,
                        max_ws_fragment_parts=128,
                        max_ws_fragment_bytes=8 * 1024 * 1024,
                        max_concurrent_ws_handlers=16,
                        resource_overflow_policy="drop",
                    ),
                    dedup_store=self.store,
                )
                channel.on("message", self._on_message)
                channel.on("cardAction", self._on_card_action)
                channel.on("error", self._on_channel_error)
                channel.on("reconnecting", self._on_reconnecting)
                channel.on("reconnected", self._on_reconnected)
                self._channel = channel
                await channel.connect_until_ready(timeout=12)
                self._status = "connected"
            except Exception as exc:
                self._status = "error"
                self._last_error = self._safe_error(exc)
                if self._channel is not None:
                    try:
                        await self._channel.disconnect()
                    except Exception:
                        pass
                self._channel = None

    async def send_agent_run_update(
        self,
        *,
        thread_id: str,
        run_id: str,
        answer: str,
    ) -> None:
        chat_id = thread_id.rsplit(":", maxsplit=1)[-1]
        if not chat_id or chat_id == thread_id:
            raise ValueError("无法从 Agent 会话中识别飞书 chat_id。")
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"markdown": answer},
            {"uuid": self._uuid(run_id, "root-approval-result")},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="markdown",
            content=answer,
        )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()
            self._status = "disabled"

    async def _stop_unlocked(self) -> None:
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        channel = self._channel
        self._channel = None
        if channel is not None:
            try:
                await channel.disconnect()
            except Exception:
                pass

    async def _on_channel_error(self, error: Exception) -> None:
        self._last_error = self._safe_error(error)

    def _on_reconnecting(self) -> None:
        self._status = "reconnecting"

    def _on_reconnected(self) -> None:
        self._status = "connected"
        self._last_error = None

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self._last_error = self._safe_error(error)

        task.add_done_callback(completed)

    async def _on_message(self, message: Any) -> None:
        message_id = str(
            getattr(message, "message_id", None)
            or getattr(message, "id", "")
        )
        chat_id = str(getattr(message, "chat_id", "") or "")
        sender_id = str(getattr(message, "sender_id", "") or "")
        if (
            not message_id
            or not chat_id
            or not sender_id
            or bool(getattr(message, "sender_is_bot", False))
        ):
            return
        if not self.store.claim_message(message_id, sender_id, chat_id):
            return
        if sender_id in self._settings.allowed_open_ids:
            try:
                self._owner_id(sender_id)
                kind, content = self._event_summary(message)
            except IdentityBindingError:
                kind, content = "status", "[个人工作区已停用，内容未保存]"
        else:
            kind, content = "status", "[未授权消息，内容未保存]"
        self.store.record_event(
            chat_id=chat_id,
            sender_id=sender_id,
            direction="inbound",
            kind=kind,
            content=content,
            message_id=message_id,
        )

        self._spawn(self._process_message(message))

    async def _process_message(self, message: Any) -> None:
        message_id = str(getattr(message, "message_id", None) or message.id)
        chat_id = str(message.chat_id)
        sender_id = str(message.sender_id)
        chat_type = str(getattr(message, "chat_type", ""))

        if sender_id not in self._settings.allowed_open_ids:
            self.store.mark_rejected(message_id, "sender_not_allowed")
            if chat_type in {"p2p", "private", "direct"}:
                await self._reply_safely(
                    chat_id,
                    message_id,
                    (
                        "这台电脑尚未授权你的飞书账号。\n"
                        f"你的 Open ID：{sender_id}\n"
                        "请在电脑端“外部接入”设置中加入该 ID，再重新发送指令。"
                    ),
                    "unauthorized",
                )
            return

        try:
            self._owner_id(sender_id)
        except IdentityBindingError as exc:
            self.store.mark_rejected(message_id, "workspace_not_active")
            await self._reply_safely(
                chat_id,
                message_id,
                str(exc),
                "workspace-not-active",
            )
            return

        if chat_type not in {"p2p", "private", "direct"}:
            if (
                not self._settings.allow_group_mentions
                or not bool(getattr(message, "mentioned_bot", False))
            ):
                self.store.mark_rejected(message_id, "group_policy")
                return

        raw_content_type = str(getattr(message, "raw_content_type", ""))
        text = str(
            getattr(message, "body_text", "")
            or getattr(message, "safe_content_text", "")
        ).strip()
        approval_match = re.fullmatch(
            r"(批准|拒绝)\s+([0-9a-fA-F-]{36})",
            text,
        )
        if approval_match:
            try:
                result = await asyncio.to_thread(
                    self.runner.resume_run,
                    approval_match.group(2),
                    approved=approval_match.group(1) == "批准",
                    requester_owner_id=self._owner_id(sender_id),
                )
                await self._send_markdown_reply(
                    chat_id,
                    message_id,
                    result.answer,
                    self._uuid(message_id, "approval-decision"),
                )
                self.store.mark_completed(message_id, result.run_id)
            except AgentRunError as exc:
                self.store.mark_rejected(message_id, "approval_rejected")
                await self._reply_safely(
                    chat_id,
                    message_id,
                    str(exc),
                    "approval-rejected",
                )
            return
        if text.lower() in {"菜单", "功能", "功能菜单", "/menu", "menu", "help"}:
            await self._send_feature_menu(chat_id, message_id)
            self.store.mark_completed(message_id, "feature-menu")
            return
        if self.store.claim_feature_menu(chat_id):
            try:
                await self._send_feature_menu(chat_id, message_id)
            except Exception as exc:
                self.store.release_feature_menu(chat_id)
                self._status = "error"
                self._last_error = self._safe_error(exc)

        image_resources = self._image_resources(message)
        if image_resources:
            if len(image_resources) > 1:
                self.store.mark_rejected(message_id, "too_many_images")
                await self._reply_safely(
                    chat_id,
                    message_id,
                    "当前一次只能处理一张图片，请重新发送单张图片。",
                    "too-many-images",
                )
                return
            await self._process_media_image_message(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                file_key=image_resources[0][0],
                file_name=image_resources[0][1],
            )
            return

        if raw_content_type == "image":
            self.store.mark_failed(message_id, "missing_image_resource")
            await self._reply_safely(
                chat_id,
                message_id,
                "收到了图片消息，但没有取得可下载的图片资源。请确认应用已开通消息资源权限后重试。",
                "missing-image-resource",
            )
            return

        if raw_content_type not in {"text", "post"}:
            self.store.mark_rejected(message_id, "unsupported_content")
            await self._reply_safely(
                chat_id,
                message_id,
                "当前版本支持文本和单张图片；文件与视频将在后续接入。",
                "unsupported",
            )
            return

        if not text:
            self.store.mark_rejected(message_id, "empty_message")
            await self._reply_safely(
                chat_id,
                message_id,
                "没有识别到文本指令，请输入你希望 Agent 完成的任务。",
                "empty",
            )
            return
        if len(text) > self.max_message_chars:
            self.store.mark_rejected(message_id, "message_too_long")
            await self._reply_safely(
                chat_id,
                message_id,
                f"指令过长，请控制在 {self.max_message_chars} 个字符以内。",
                "too-long",
            )
            return

        await self._reply_safely(
            chat_id,
            message_id,
            "已收到，正在调用本地 Agent 处理。",
            "accepted",
        )
        try:
            result = await asyncio.to_thread(
                self.runner.run,
                text,
                owner_id=self._owner_id(sender_id),
                thread_id=self._thread_id(chat_id, sender_id),
                channel="feishu",
            )
            answer = result.answer.strip() or "任务已完成，但没有生成可展示的文本。"
            if len(answer) > self.max_reply_chars:
                answer = (
                    answer[: self.max_reply_chars - 28]
                    + "\n\n[回答过长，已在飞书端截断]"
                )
            await self._send_markdown_reply(
                chat_id,
                message_id,
                answer,
                self._uuid(message_id, "completed"),
            )
            self.store.mark_completed(message_id, result.run_id)
        except AgentRunError as exc:
            self.store.mark_rejected(message_id, "agent_run_conflict")
            await self._reply_safely(
                chat_id,
                message_id,
                str(exc),
                "run-conflict",
            )
        except Exception:
            self.store.mark_failed(message_id, "agent_execution_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "本地 Agent 执行失败。请在电脑端控制台查看日志后重试。",
                "failed",
            )

    async def _on_card_action(self, event: Any) -> None:
        chat_id = str(getattr(event, "chat_id", "") or "")
        message_id = str(getattr(event, "message_id", "") or "")
        operator = getattr(event, "operator", None)
        sender_id = str(getattr(operator, "open_id", "") or "")
        value = getattr(getattr(event, "action", None), "value", None)
        command = value.get("command") if isinstance(value, dict) else None
        job_id = value.get("job_id") if isinstance(value, dict) else None
        asset_id = value.get("asset_id") if isinstance(value, dict) else None
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if (
            not chat_id
            or not message_id
            or sender_id not in self._settings.allowed_open_ids
            or not isinstance(command, str)
        ):
            return
        try:
            self._owner_id(sender_id)
        except IdentityBindingError as exc:
            await self._reply_safely(
                chat_id,
                message_id,
                str(exc),
                "workspace-not-active-card",
            )
            return
        self.store.record_event(
            chat_id=chat_id,
            sender_id=sender_id,
            direction="inbound",
            kind="card",
            content=(
                f"[重试卡片] {self._card_command_label(command)}"
                if command in {"retry_spatial_job", "retry_photo_style_job"}
                else f"[功能卡片] {self._card_command_label(command)}"
            ),
        )
        self._spawn(
            self._process_card_action(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                command=command,
                job_id=job_id if isinstance(job_id, str) else None,
                asset_id=asset_id if isinstance(asset_id, str) else None,
                session_id=(session_id if isinstance(session_id, str) else None),
            )
        )

    async def _process_card_action(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        command: str,
        job_id: str | None = None,
        asset_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if command == "refresh_spatial_viewer":
            await self._refresh_spatial_viewer_link(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                asset_id=asset_id,
            )
            return
        if command == "retry_spatial_job":
            await self._retry_spatial_job(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                job_id=job_id,
            )
            return
        if command == "retry_photo_style_job":
            await self._retry_photo_style_job(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                job_id=job_id,
            )
            return
        if command == "submit_photo_style_session":
            await self._submit_photo_style_session(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                session_id=session_id,
            )
            return
        if command == "cancel_media_session":
            await self._cancel_media_session(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                session_id=session_id,
            )
            return
        if command == "spatial_photo":
            owner_id = self._owner_id(sender_id)
            _, previous = self.store.start_media_session(
                capability_id="spatial_photo",
                owner_id=owner_id,
                chat_id=chat_id,
                sender_id=sender_id,
            )
            await self._discard_session_sources(previous)
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "空间照片模式已开启，请在 15 分钟内发送一张 JPG、PNG 或 WebP 图片。"
                    "完成后会返回封面和可移动视角 Viewer 卡片。"
                ),
                "card-spatial-help",
            )
            return
        if command == "photo_style_transfer":
            style = self.style_service
            if style is None:
                await self._reply_safely(
                    chat_id,
                    message_id,
                    "图片风格化能力尚未安装，请先在电脑端配置独立 SDXL + IP-Adapter 服务。",
                    "card-photo-style-unavailable",
                )
                return
            provider = await asyncio.to_thread(style.provider_status)
            if provider.get("ready") is False:
                await self._reply_safely(
                    chat_id,
                    message_id,
                    "图片风格化服务尚未就绪，请先在电脑端启动并通过健康检查。",
                    "card-photo-style-not-ready",
                )
                return
            owner_id = self._owner_id(sender_id)
            _, previous = self.store.start_media_session(
                capability_id="photo_style_transfer",
                owner_id=owner_id,
                chat_id=chat_id,
                sender_id=sender_id,
            )
            await self._discard_session_sources(previous)
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片风格化模式已开启。请在 15 分钟内先发送 1 张内容图，"
                    "随后再发送 1–3 张风格参考图。\n"
                    f"当前 Provider：{provider.get('name', 'unknown')}"
                ),
                "card-photo-style-help",
            )
            return
        prompts = {
            "list_assets": "查看我的个人资产",
            "capabilities": "你能做什么？请简洁列出当前可用能力。",
            "current_time": "现在几点？",
        }
        prompt = prompts.get(command)
        if prompt is None:
            await self._reply_safely(
                chat_id,
                message_id,
                "这个卡片功能当前不可用，请发送“菜单”获取最新功能。",
                "card-unknown",
            )
            return
        await self._reply_safely(
            chat_id,
            message_id,
            f"正在执行：{self._card_command_label(command)}。",
            f"card-{command}-accepted",
        )
        try:
            result = await asyncio.to_thread(
                self.runner.run,
                prompt,
                owner_id=self._owner_id(sender_id),
                thread_id=self._thread_id(chat_id, sender_id),
                channel="feishu",
            )
            await self._send_markdown_reply(
                chat_id,
                message_id,
                result.answer,
                self._uuid(message_id, f"card-{command}-completed"),
            )
        except Exception:
            await self._reply_safely(
                chat_id,
                message_id,
                "卡片任务执行失败，请在电脑端控制台查看日志。",
                f"card-{command}-failed",
            )

    async def _send_feature_menu(
        self,
        chat_id: str,
        message_id: str,
    ) -> None:
        card = (
            new_card()
            .header(
                title="个人数字助手",
                subtitle="选择常用功能，或直接发送自然语言指令",
                template="green",
            )
            .markdown(
                "图片和生成资产默认保存在你的电脑。"
                "高耗时能力会先创建本地任务，再回传结果。"
            )
            .buttons(
                [
                    {
                        "label": "生成空间照片",
                        "action": {"command": "spatial_photo"},
                        "style": "primary",
                    },
                    {
                        "label": "图片风格化",
                        "action": {"command": "photo_style_transfer"},
                    },
                ]
            )
            .buttons(
                [
                    {
                        "label": "查看个人资产",
                        "action": {"command": "list_assets"},
                    },
                    {
                        "label": "能力列表",
                        "action": {"command": "capabilities"},
                    },
                ]
            )
            .buttons(
                [
                    {
                        "label": "当前时间",
                        "action": {"command": "current_time"},
                    },
                ]
            )
            .footer("发送“菜单”可随时重新打开")
            .build()
        )
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"card": card.data},
            {
                "reply_to": message_id,
                "uuid": self._uuid(message_id, "feature-menu"),
            },
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="card",
            content=(
                "[功能卡片] 空间照片、图片风格化、个人资产、"
                "能力列表、当前时间"
            ),
        )

    async def _discard_session_sources(
        self,
        session: MediaCollectionSession | None,
    ) -> None:
        if session is None or session.state == "submitted":
            return
        spatial = self.spatial_service
        if spatial is None:
            return
        source_ids = [
            source_id
            for source_id in (
                session.content_image_id,
                *session.style_image_ids,
            )
            if source_id
        ]
        for source_id in source_ids:
            try:
                await asyncio.to_thread(
                    spatial.delete_source_image,
                    source_id,
                    owner_id=session.owner_id,
                )
            except AssetError:
                continue

    async def _collect_photo_style_image(
        self,
        *,
        session: MediaCollectionSession,
        message_id: str,
        chat_id: str,
        sender_id: str,
        file_key: str,
        file_name: str | None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None:
            self.store.mark_failed(message_id, "style_asset_service_unavailable")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片资产服务当前不可用，请确认电脑端服务已启动。",
                "style-assets-unavailable",
            )
            return
        role = "内容图" if session.state == "awaiting_content" else "风格参考图"
        await self._reply_safely(
            chat_id,
            message_id,
            f"已收到{role}，正在安全下载并移除图片元数据。",
            f"style-{role}-accepted",
        )
        staged = None
        try:
            channel = self._channel
            if channel is None:
                raise RuntimeError("飞书长连接当前不可用")
            image_bytes = await self._download_image_resource(
                channel,
                file_key=file_key,
                message_id=message_id,
            )
            staged = await asyncio.to_thread(
                spatial.stage_source_image,
                image_bytes,
                original_name=file_name or "feishu-style-image.jpg",
                owner_id=session.owner_id,
            )
            updated = self.store.add_media_session_image(
                session_id=session.id,
                owner_id=session.owner_id,
                source_image_id=staged.id,
            )
            self.store.mark_completed(message_id, f"media-session:{session.id}")
            if updated.state == "awaiting_styles":
                await self._reply_safely(
                    chat_id,
                    message_id,
                    "内容图已保存。现在请发送 1–3 张风格参考图。",
                    "style-content-saved",
                )
                return
            await self._send_photo_style_collection_card(
                chat_id=chat_id,
                message_id=message_id,
                session=updated,
            )
        except (AssetError, ValueError) as exc:
            if staged is not None:
                try:
                    await asyncio.to_thread(
                        spatial.delete_source_image,
                        staged.id,
                        owner_id=session.owner_id,
                    )
                except AssetError:
                    pass
            self.store.mark_failed(message_id, "style_image_rejected")
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片无法加入风格化会话：{exc}",
                "style-image-rejected",
            )
        except Exception:
            self.store.mark_failed(message_id, "style_image_download_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片下载失败，请检查飞书消息资源权限和电脑端日志后重试。",
                "style-image-download-failed",
            )

    async def _send_photo_style_collection_card(
        self,
        *,
        chat_id: str,
        message_id: str,
        session: MediaCollectionSession,
    ) -> None:
        style_count = len(session.style_image_ids)
        card = (
            new_card()
            .header(
                title="图片风格化素材已就绪",
                subtitle="确认后创建本地异步任务",
                template="green",
            )
            .markdown(
                f"**内容图**：1 张\n**风格参考图**：{style_count} 张\n"
                "如果需要更多参考图，可继续发送，最多 3 张。"
            )
            .buttons(
                [
                    {
                        "label": "开始生成",
                        "action": {
                            "command": "submit_photo_style_session",
                            "session_id": session.id,
                        },
                        "style": "primary",
                    },
                    {
                        "label": "取消",
                        "action": {
                            "command": "cancel_media_session",
                            "session_id": session.id,
                        },
                    },
                ]
            )
            .footer("重复点击开始生成不会创建多个任务")
            .build()
        )
        await self._send_card(
            chat_id,
            message_id,
            card.data,
            self._uuid(message_id, f"style-session-{session.id}-{style_count}"),
            f"[素材卡片] 内容图 1 张、风格参考图 {style_count} 张",
        )

    async def _submit_photo_style_session(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        session_id: str | None,
    ) -> None:
        style = self.style_service
        owner_id = self._owner_id(sender_id)
        if style is None or not session_id or len(session_id) > 100:
            await self._reply_safely(
                chat_id,
                message_id,
                "无法识别图片风格化会话，请从功能菜单重新开始。",
                "style-session-invalid",
            )
            return
        session = self.store.claim_media_session(
            session_id=session_id,
            owner_id=owner_id,
            chat_id=chat_id,
        )
        if session is None:
            await self._reply_safely(
                chat_id,
                message_id,
                "该会话已提交、已过期或不属于当前用户，请勿重复点击。",
                "style-session-not-claimable",
            )
            return
        if session.content_image_id is None or not session.style_image_ids:
            self.store.finish_media_session(
                session_id=session.id,
                owner_id=owner_id,
                state="ready",
            )
            await self._reply_safely(
                chat_id,
                message_id,
                "素材尚未收集完整，请先发送内容图和至少一张风格参考图。",
                "style-session-incomplete",
            )
            return
        try:
            created = await asyncio.to_thread(
                style.create_transfer_from_sources,
                session.content_image_id,
                list(session.style_image_ids),
                title="飞书图片风格化",
                owner_id=owner_id,
            )
            self.store.set_media_session_job(
                session_id=session.id,
                owner_id=owner_id,
                job_id=created.job.id,
            )
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片风格化任务已创建，正在由独立模型服务处理。\n"
                    f"任务 ID：{created.job.id}"
                ),
                f"style-job-{created.job.id}-created",
            )
            completed = await self._deliver_photo_style_job_result(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                job_id=created.job.id,
            )
            self.store.finish_media_session(
                session_id=session.id,
                owner_id=owner_id,
                state="completed" if completed else "failed",
            )
        except AssetError as exc:
            self.store.finish_media_session(
                session_id=session.id,
                owner_id=owner_id,
                state="ready",
            )
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片风格化任务无法创建：{exc}",
                "style-job-create-rejected",
            )
        except TimeoutError:
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化处理时间超过 10 分钟，任务仍可在电脑端继续查看。",
                "style-job-timeout",
            )
        except Exception:
            self.store.finish_media_session(
                session_id=session.id,
                owner_id=owner_id,
                state="failed",
            )
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化提交失败，请检查独立模型服务和电脑端日志。",
                "style-job-create-failed",
            )

    async def _deliver_photo_style_job_result(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        job_id: str,
    ) -> bool:
        style = self.style_service
        if style is None:
            raise AssetError("图片风格化能力当前不可用。")
        owner_id = self._owner_id(sender_id)
        job = await self._wait_for_job(job_id, owner_id=owner_id)
        if job.status == "failed":
            await self._send_photo_style_retry_card(
                chat_id,
                message_id,
                job.id,
                job.error or job.message,
            )
            return False

        assets = style.asset_library
        asset = await asyncio.to_thread(
            assets.get_asset,
            job.asset_id,
            owner_id=owner_id,
        )
        if not asset.result_url:
            raise AssetError("风格化任务已完成，但结果图片不存在。")
        result_name = asset.result_url.rsplit("/", maxsplit=1)[-1]
        result_path = await asyncio.to_thread(
            assets.resolve_asset_file,
            asset.id,
            result_name,
        )
        result_image_sent = False
        try:
            await self._send_image(
                chat_id,
                message_id,
                result_path,
                self._uuid(message_id, f"style-job-{job.id}-result"),
                media_url=asset.result_url,
            )
            result_image_sent = True
        except Exception as exc:
            self._last_error = self._safe_error(exc)
            await self._send_media_upload_permission_card(
                chat_id,
                message_id,
                result_kind="图片风格化结果",
                fallback="结果仍已保存在电脑端个人资产库。",
            )
        seed = asset.parameters.get("seed")
        provider_name = self._safe_card_value(asset.provider_name or "unknown")
        model_name = self._safe_card_value(asset.model_name or "unknown")
        card = (
            new_card()
            .header(
                title="图片风格化已完成",
                subtitle=(
                    "结果图已发送，可在飞书中直接查看或下载"
                    if result_image_sent
                    else "结果已保存到电脑端个人资产库"
                ),
                template="green",
            )
            .markdown(
                f"**尺寸**：{asset.width} × {asset.height}\n"
                f"**Provider**：{provider_name}\n"
                f"**模型**：{model_name}\n"
                f"**Seed**：{seed if seed is not None else '未记录'}"
            )
            .buttons(
                [
                    {
                        "label": "再次创作",
                        "action": {"command": "photo_style_transfer"},
                        "style": "primary",
                    }
                ]
            )
            .footer("结果保存在当前用户的本机个人资产库")
            .build()
        )
        await self._send_card(
            chat_id,
            message_id,
            card.data,
            self._uuid(message_id, f"style-job-{job.id}-completed-card"),
            "[结果卡片] 图片风格化已完成",
        )
        return True

    async def _send_photo_style_retry_card(
        self,
        chat_id: str,
        message_id: str,
        job_id: str,
        error: str,
    ) -> None:
        card = (
            new_card()
            .header(
                title="图片风格化失败",
                subtitle="已保留规范化内容图和风格参考图",
                template="red",
            )
            .markdown(f"失败原因：{self._safe_card_value(error, limit=500)}")
            .buttons(
                [
                    {
                        "label": "重新生成",
                        "action": {
                            "command": "retry_photo_style_job",
                            "job_id": job_id,
                        },
                        "style": "primary",
                    }
                ]
            )
            .footer("重复点击不会创建多个任务")
            .build()
        )
        await self._send_card(
            chat_id,
            message_id,
            card.data,
            self._uuid(message_id, f"retry-style-{job_id}"),
            f"[重试卡片] 图片风格化失败：{error[:500]}",
        )

    async def _retry_photo_style_job(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        job_id: str | None,
    ) -> None:
        style = self.style_service
        if style is None or not job_id or len(job_id) > 100:
            await self._reply_safely(
                chat_id,
                message_id,
                "无法识别要重试的图片风格化任务，请重新选择功能。",
                "retry-style-invalid",
            )
            return
        owner_id = self._owner_id(sender_id)
        try:
            job, started = await asyncio.to_thread(
                style.retry_job,
                job_id,
                owner_id=owner_id,
            )
        except AssetError as exc:
            await self._reply_safely(
                chat_id,
                message_id,
                f"无法重新生成：{exc}",
                "retry-style-rejected",
            )
            return
        if job.status == "completed":
            await self._reply_safely(
                chat_id,
                message_id,
                "这个图片风格化任务已经完成，无需再次重试。",
                "retry-style-completed",
            )
            return
        if not started:
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化已经在重新生成中，请稍候。",
                "retry-style-running",
            )
            return
        await self._reply_safely(
            chat_id,
            message_id,
            "已复用原始内容图和风格参考图，任务重新进入模型队列。",
            "retry-style-started",
        )
        await self._deliver_photo_style_job_result(
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            job_id=job.id,
        )

    async def _cancel_media_session(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        session_id: str | None,
    ) -> None:
        if not session_id or len(session_id) > 100:
            await self._reply_safely(
                chat_id,
                message_id,
                "无法识别要取消的图片会话。",
                "cancel-media-invalid",
            )
            return
        owner_id = self._owner_id(sender_id)
        session = self.store.cancel_media_session(
            session_id=session_id,
            owner_id=owner_id,
            chat_id=chat_id,
        )
        if session is None:
            await self._reply_safely(
                chat_id,
                message_id,
                "该图片会话不存在、已过期或不属于当前用户。",
                "cancel-media-missing",
            )
            return
        if session.state == "submitted":
            await self._reply_safely(
                chat_id,
                message_id,
                "任务已经提交，不能通过素材卡片取消。",
                "cancel-media-submitted",
            )
            return
        await self._discard_session_sources(session)
        await self._reply_safely(
            chat_id,
            message_id,
            "图片会话已取消，尚未提交的本地临时图片已清理。",
            "cancel-media-completed",
        )

    async def _process_media_image_message(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        file_key: str,
        file_name: str | None,
    ) -> None:
        owner_id = self._owner_id(sender_id)
        session = self.store.get_media_session(
            owner_id=owner_id,
            chat_id=chat_id,
        )
        if session is not None and session.capability_id == "photo_style_transfer":
            await self._collect_photo_style_image(
                session=session,
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                file_key=file_key,
                file_name=file_name,
            )
            return
        await self._process_spatial_image_message(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            file_key=file_key,
            file_name=file_name,
        )

    async def _process_spatial_image_message(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        file_key: str,
        file_name: str | None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None:
            self.store.mark_failed(message_id, "spatial_service_unavailable")
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片能力当前不可用，请确认电脑端服务已经启动。",
                "spatial-unavailable",
            )
            return

        await self._reply_safely(
            chat_id,
            message_id,
            "已收到图片，正在安全下载到本机并创建空间照片任务。",
            "image-accepted",
        )
        try:
            channel = self._channel
            if channel is None:
                raise RuntimeError("飞书长连接当前不可用")
            image_bytes = await self._download_image_resource(
                channel,
                file_key=file_key,
                message_id=message_id,
            )
            created = await asyncio.to_thread(
                spatial.create_scene,
                image_bytes,
                original_name=file_name or "feishu-image.jpg",
                title="飞书空间照片",
                owner_id=self._owner_id(sender_id),
            )
            self.store.attach_event_media(
                message_id,
                created.asset.source_url,
            )
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片校验通过，空间照片任务已创建。\n"
                    f"任务 ID：{created.job.id}\n"
                    "本机正在进行深度估计和分层处理，完成后会自动回复。"
                ),
                "image-job-created",
            )
            await self._deliver_spatial_job_result(
                chat_id=chat_id,
                message_id=message_id,
                job_id=created.job.id,
                tracked_message_id=message_id,
                owner_id=self._owner_id(sender_id),
            )
        except AssetError as exc:
            self.store.mark_failed(message_id, "invalid_image")
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片无法处理：{exc}",
                "invalid-image",
            )
        except TimeoutError:
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "空间照片处理时间超过 10 分钟，但任务仍在本机后台运行。"
                    "完成后机器人会继续补发结果，无需重新上传图片。"
                ),
                "image-job-timeout",
            )
            self._spawn(
                self._continue_spatial_delivery_after_timeout(
                    chat_id=chat_id,
                    message_id=message_id,
                    job_id=created.job.id,
                    tracked_message_id=message_id,
                    owner_id=self._owner_id(sender_id),
                )
            )
        except Exception:
            self.store.mark_failed(message_id, "image_processing_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片下载或空间照片创建失败，请检查消息资源权限和电脑端日志。",
                "image-processing-failed",
            )

    async def _retry_spatial_job(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        job_id: str | None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None or not job_id or len(job_id) > 100:
            await self._reply_safely(
                chat_id,
                message_id,
                "无法识别要重试的空间照片任务，请重新发送图片。",
                "retry-spatial-invalid",
            )
            return
        try:
            job, started = await asyncio.to_thread(
                spatial.retry_job,
                job_id,
                owner_id=self._owner_id(sender_id),
            )
        except AssetError as exc:
            await self._reply_safely(
                chat_id,
                message_id,
                f"无法重新生成：{exc}",
                "retry-spatial-rejected",
            )
            return

        if job.status == "completed":
            await self._reply_safely(
                chat_id,
                message_id,
                "这个空间照片任务已经生成完成，无需再次重试。",
                "retry-spatial-completed",
            )
            return
        if not started:
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片已经在重新生成中，请稍候。",
                "retry-spatial-running",
            )
            return

        await self._reply_safely(
            chat_id,
            message_id,
            "已复用原始图片，空间照片重新进入本地处理队列。",
            "retry-spatial-started",
        )
        await self._deliver_spatial_job_result(
            chat_id=chat_id,
            message_id=message_id,
            job_id=job.id,
            tracked_message_id=None,
            owner_id=self._owner_id(sender_id),
        )

    async def _deliver_spatial_job_result(
        self,
        *,
        chat_id: str,
        message_id: str,
        job_id: str,
        tracked_message_id: str | None,
        owner_id: str,
        wait_timeout_seconds: float | None = None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None:
            raise AssetError("空间照片能力当前不可用。")
        job = await self._wait_for_job(
            job_id,
            owner_id=owner_id,
            timeout_seconds=wait_timeout_seconds,
        )
        if job.status == "failed":
            if tracked_message_id:
                self.store.mark_failed(
                    tracked_message_id,
                    "spatial_job_failed",
                )
            await self._send_spatial_retry_card(
                chat_id,
                message_id,
                job.id,
                job.error or job.message,
            )
            return

        asset = await asyncio.to_thread(
            spatial.get_asset,
            job.asset_id,
            owner_id=owner_id,
        )
        if tracked_message_id:
            self.store.mark_completed(tracked_message_id, job.id)
        viewer_url: str | None = None
        if self.viewer_link_factory is not None:
            try:
                viewer_url = await asyncio.to_thread(
                    self.viewer_link_factory,
                    asset.id,
                )
            except Exception as exc:
                self._last_error = self._safe_error(exc)
        preview_sent = False
        if asset.preview_url:
            preview_name = asset.preview_url.rsplit("/", maxsplit=1)[-1]
            preview_path = await asyncio.to_thread(
                spatial.resolve_asset_file,
                asset.id,
                preview_name,
            )
            try:
                await self._send_image(
                    chat_id,
                    message_id,
                    preview_path,
                    self._uuid(message_id, f"spatial-job-{job.id}-preview"),
                    media_url=asset.preview_url,
                )
                preview_sent = True
            except Exception as exc:
                self._last_error = self._safe_error(exc)
                await self._send_media_upload_permission_card(
                    chat_id,
                    message_id,
                    result_kind="空间照片封面",
                    fallback="可先点击结果卡片查看可动预览。",
                )
        builder = (
            new_card()
            .header(
                title="2.5D 空间照片已生成",
                subtitle=(
                    "封面已发送，点击按钮查看可移动视角"
                    if preview_sent
                    else "点击按钮查看可移动视角"
                ),
                template="green",
            )
            .markdown(
                f"**名称**：{self._safe_card_value(asset.name)}\n"
                f"**尺寸**：{asset.width} × {asset.height}\n"
                f"**模型**：{self._safe_card_value(asset.model_name or 'unknown')}\n"
                + (
                    "**预览**：短时签名链接，需手机能够访问当前 Viewer 网络"
                    if viewer_url
                    else "**预览**：Viewer 暂不可达，可在电脑端个人资产库打开"
                )
            )
        )
        buttons: list[dict[str, Any]] = []
        if viewer_url:
            buttons.append(
                {
                    "label": "打开可动预览",
                    "url": viewer_url,
                    "style": "primary",
                }
            )
            buttons.append(
                {
                    "label": "刷新预览链接",
                    "action": {
                        "command": "refresh_spatial_viewer",
                        "asset_id": asset.id,
                    },
                }
            )
        buttons.append(
            {
                "label": "重新制作",
                "action": {"command": "spatial_photo"},
            }
        )
        card = builder.buttons(buttons).footer(
            "2.5D 交互依赖 Viewer，飞书图片气泡只展示静态封面"
        ).build()
        await self._send_card(
            chat_id,
            message_id,
            card.data,
            self._uuid(message_id, f"spatial-job-{job.id}-completed-card"),
            "[结果卡片] 2.5D 空间照片已生成",
        )

    async def _refresh_spatial_viewer_link(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        asset_id: str | None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None or self.viewer_link_factory is None:
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片 Viewer 当前不可用，请确认电脑端服务与公网隧道已经启动。",
                "refresh-viewer-unavailable",
            )
            return
        if not asset_id:
            await self._reply_safely(
                chat_id,
                message_id,
                "无法识别要刷新的空间照片，请重新生成一次空间照片。",
                "refresh-viewer-invalid",
            )
            return
        try:
            asset = await asyncio.to_thread(
                spatial.get_asset,
                asset_id,
                owner_id=self._owner_id(sender_id),
            )
            viewer_url = await asyncio.to_thread(
                self.viewer_link_factory,
                asset.id,
            )
        except (AssetError, ViewerLinkError) as exc:
            await self._reply_safely(
                chat_id,
                message_id,
                f"预览链接刷新失败：{self._safe_error(exc)}",
                "refresh-viewer-rejected",
            )
            return
        card = (
            new_card()
            .header(
                title="空间照片预览链接已刷新",
                subtitle="旧的临时域名可能已失效，请使用这个新链接",
                template="green",
            )
            .markdown(
                f"**名称**：{self._safe_card_value(asset.name)}\n"
                "**有效期**：最长 7 天；公网隧道重启后可再次点击原卡片刷新"
            )
            .buttons(
                [
                    {
                        "label": "打开最新预览",
                        "url": viewer_url,
                        "style": "primary",
                    },
                    {
                        "label": "再次刷新",
                        "action": {
                            "command": "refresh_spatial_viewer",
                            "asset_id": asset.id,
                        },
                    },
                ]
            )
            .footer("链接仅授权访问当前空间照片，请勿转发给无关人员")
            .build()
        )
        refresh_bucket = int(time.time() // 10)
        await self._send_card(
            chat_id,
            message_id,
            card.data,
            self._uuid(
                message_id,
                f"refresh-viewer-{asset.id}-{refresh_bucket}",
            ),
            "[结果卡片] 空间照片预览链接已刷新",
        )

    async def _continue_spatial_delivery_after_timeout(
        self,
        *,
        chat_id: str,
        message_id: str,
        job_id: str,
        tracked_message_id: str | None,
        owner_id: str,
    ) -> None:
        """Keep the user-facing delivery alive after the first wait window."""
        try:
            await self._deliver_spatial_job_result(
                chat_id=chat_id,
                message_id=message_id,
                job_id=job_id,
                tracked_message_id=tracked_message_id,
                owner_id=owner_id,
                wait_timeout_seconds=self.background_job_timeout_seconds,
            )
        except TimeoutError:
            self.store.mark_failed(message_id, "spatial_job_delivery_timeout")
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片后台处理超过 1 小时，请在电脑端检查任务并重试。",
                "image-job-background-timeout",
            )
        except Exception:
            self.store.mark_failed(message_id, "spatial_job_delivery_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片已经结束处理，但结果回传失败，请在电脑端任务列表重试。",
                "image-job-background-delivery-failed",
            )

    async def _send_spatial_retry_card(
        self,
        chat_id: str,
        message_id: str,
        job_id: str,
        error: str,
    ) -> None:
        card = (
            new_card()
            .header(
                title="空间照片生成失败",
                subtitle="原始图片仍保存在本机，可以直接重新生成",
                template="red",
            )
            .markdown(f"失败原因：{error[:500]}")
            .buttons(
                [
                    {
                        "label": "重新生成",
                        "action": {
                            "command": "retry_spatial_job",
                            "job_id": job_id,
                        },
                        "style": "primary",
                    }
                ]
            )
            .footer("重复点击不会创建多个任务")
            .build()
        )
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"card": card.data},
            {
                "reply_to": message_id,
                "uuid": self._uuid(message_id, f"retry-spatial-{job_id}"),
            },
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="card",
            content=f"[重试卡片] 空间照片生成失败、重新生成：{error[:500]}",
            message_id=message_id,
        )

    async def _download_image_resource(
        self,
        channel: FeishuChannelLike,
        *,
        file_key: str,
        message_id: str,
    ) -> bytes:
        """Try both Feishu image routes and retain permission diagnostics."""
        client = getattr(channel, "client", None)
        errors: list[tuple[int | None, str]] = []
        if client is not None:
            from lark_channel.api.im.v1.model.get_image_request import (
                GetImageRequest,
            )
            from lark_channel.api.im.v1.model.get_message_resource_request import (
                GetMessageResourceRequest,
            )

            requests = (
                (
                    client.im.v1.message_resource,
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type("image")
                    .build(),
                ),
                (
                    client.im.v1.image,
                    GetImageRequest.builder().image_key(file_key).build(),
                ),
            )
            for resource, request in requests:
                try:
                    response = await resource.aget(request)
                    code = getattr(response, "code", None)
                    if code in {None, 0}:
                        payload = await self._response_file_bytes(response)
                        if payload:
                            return payload
                    errors.append(
                        (code, str(getattr(response, "msg", "") or ""))
                    )
                except Exception as exc:
                    errors.append((None, type(exc).__name__))
        else:
            for linked_message_id in (message_id, None):
                payload = await channel.download_resource(
                    file_key,
                    resource_type="image",
                    message_id=linked_message_id,
                )
                if payload:
                    return payload

        permission_denied = any(code == 99991672 for code, _ in errors)
        suffix = "（平台错误码 99991672）" if permission_denied else ""
        raise FeishuResourceError(
            "无法下载飞书图片。请在开发者后台开通 im:resource，"
            "并开通 im:message:readonly（或 im:message），发布新版本后"
            f"重新授权再试{suffix}。"
        )

    @staticmethod
    async def _response_file_bytes(response: Any) -> bytes | None:
        file = getattr(response, "file", None)
        if isinstance(file, (bytes, bytearray)):
            return bytes(file)
        if callable(getattr(file, "read", None)):
            return await asyncio.to_thread(file.read)
        return None

    async def _wait_for_job(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        spatial = self.spatial_service
        if spatial is None:
            raise RuntimeError("本地媒体任务服务当前不可用")
        effective_timeout = (
            self.job_timeout_seconds
            if timeout_seconds is None
            else max(float(timeout_seconds), self.job_poll_interval)
        )
        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            job = await asyncio.to_thread(
                spatial.get_job,
                job_id,
                owner_id=owner_id,
            )
            if job.status in {"completed", "failed"}:
                return job
            await asyncio.sleep(self.job_poll_interval)
        raise TimeoutError("spatial job timed out")

    @staticmethod
    def _image_resources(message: Any) -> list[tuple[str, str | None]]:
        images: list[tuple[str, str | None]] = []
        for resource in list(getattr(message, "resources", []) or []):
            if str(getattr(resource, "type", "")) != "image":
                continue
            file_key = str(getattr(resource, "file_key", "") or "")
            if file_key:
                images.append((file_key, getattr(resource, "file_name", None)))
        if images:
            return images
        content = getattr(message, "content", None)
        image_key = str(getattr(content, "image_key", "") or "")
        return [(image_key, None)] if image_key else []

    async def _reply_safely(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        phase: str,
    ) -> None:
        try:
            await self._send_reply(
                chat_id,
                message_id,
                text,
                self._uuid(message_id, phase),
            )
        except Exception as exc:
            self._status = "error"
            self._last_error = self._safe_error(exc)

    async def _send_reply(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        uuid: str,
    ) -> None:
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"text": text},
            {"reply_to": message_id, "uuid": uuid},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="status",
            content=text,
        )

    async def _send_markdown_reply(
        self,
        chat_id: str,
        message_id: str,
        markdown: str,
        uuid: str,
    ) -> None:
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"markdown": markdown},
            {"reply_to": message_id, "uuid": uuid},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="markdown",
            content=markdown,
        )

    async def _send_card(
        self,
        chat_id: str,
        message_id: str,
        card: dict[str, Any],
        uuid: str,
        content: str,
    ) -> None:
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"card": card},
            {"reply_to": message_id, "uuid": uuid},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="card",
            content=content,
            message_id=message_id,
        )

    async def _send_media_upload_permission_card(
        self,
        chat_id: str,
        message_id: str,
        *,
        result_kind: str,
        fallback: str,
    ) -> None:
        app_id = self._settings.app_id.strip()
        permission_url = (
            f"https://open.feishu.cn/app/{app_id}/auth"
            "?q=im:resource:upload,im:resource"
            "&op_from=openapi&token_type=tenant"
        )
        card = (
            new_card()
            .header(
                title="飞书图片回传权限待开通",
                subtitle=f"{result_kind}已经生成，本机结果不会丢失",
                template="orange",
            )
            .markdown(
                "请开通**应用身份权限** `im:resource:upload`；"
                "如果租户只展示合并权限，则开通 `im:resource`。\n\n"
                "开通后还需要：**创建版本 → 发布版本 → 重新授权应用**。\n"
                f"{fallback}"
            )
            .buttons(
                [
                    {
                        "label": "开通图片上传权限",
                        "url": permission_url,
                        "style": "primary",
                    }
                ]
            )
            .footer("用户图片下载已通过，本问题只影响机器人上传结果图")
            .build()
        )
        try:
            await self._send_card(
                chat_id,
                message_id,
                card.data,
                self._uuid(message_id, f"media-upload-scope-{result_kind}"),
                "[权限卡片] 飞书图片回传权限待开通",
            )
        except Exception:
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    f"{result_kind}已经生成，但飞书不能上传结果图片。请开通应用身份权限 "
                    "im:resource:upload（或 im:resource），发布新版本并重新授权。"
                    f"{fallback}"
                ),
                "media-upload-permission-card-fallback",
            )

    async def _send_image(
        self,
        chat_id: str,
        message_id: str,
        path: Path,
        uuid: str,
        *,
        media_url: str | None = None,
    ) -> None:
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"image": {"source": str(path)}},
            {"reply_to": message_id, "uuid": uuid},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="image",
            content=f"[图片] {path.name}",
            message_id=message_id,
            media_url=media_url,
        )

    def _ensure_send_success(self, result: Any) -> None:
        if getattr(result, "success", True) is False:
            error = getattr(result, "error", None)
            raw_code = getattr(error, "raw_code", None)
            hint = str(getattr(error, "hint", "") or "").strip()
            if raw_code == 99991672:
                raise RuntimeError(
                    "缺少飞书机器人发送消息权限。请开通 "
                    "im:message:send_as_bot（或平台提示的等价权限），"
                    "并发布新版本后重试"
                )
            detail = f"（平台错误码 {raw_code}）" if raw_code else ""
            if hint:
                detail += f"：{hint[:120]}"
            raise RuntimeError(f"飞书消息发送失败{detail}")
        self._status = "connected"
        self._last_error = None

    @staticmethod
    def _uuid(message_id: str, phase: str) -> str:
        return hashlib.sha256(f"{message_id}:{phase}".encode()).hexdigest()[:48]

    @classmethod
    def _event_summary(cls, message: Any) -> tuple[str, str]:
        if cls._image_resources(message):
            return "image", "[图片]"
        text = str(
            getattr(message, "body_text", "")
            or getattr(message, "safe_content_text", "")
        ).strip()
        if text:
            return "text", text
        raw_content_type = str(
            getattr(message, "raw_content_type", "消息") or "消息"
        )
        return "status", f"[{raw_content_type}]"

    @staticmethod
    def _card_command_label(command: str) -> str:
        return {
            "spatial_photo": "生成空间照片",
            "photo_style_transfer": "图片风格化",
            "retry_spatial_job": "重新生成空间照片",
            "refresh_spatial_viewer": "刷新空间照片预览链接",
            "submit_photo_style_session": "开始图片风格化",
            "retry_photo_style_job": "重新生成风格化图片",
            "cancel_media_session": "取消图片会话",
            "list_assets": "查看个人资产",
            "capabilities": "能力列表",
            "current_time": "当前时间",
        }.get(command, "未知功能")

    @staticmethod
    def _safe_card_value(value: Any, *, limit: int = 120) -> str:
        text = " ".join(str(value).strip().split())[:limit]
        return re.sub(r"[\\`*_\[\]()]", "", text) or "unknown"

    def _owner_id(self, sender_id: str) -> str:
        if self.identity_registry is None:
            return f"feishu:{self._settings.app_id}:{sender_id}"
        return self.identity_registry.resolve_feishu_owner_key(
            app_id=self._settings.app_id,
            open_id=sender_id,
        )

    def _thread_id(self, chat_id: str, sender_id: str) -> str:
        return f"{self._owner_id(sender_id)}:{chat_id}"

    def _safe_error(self, error: Exception) -> str:
        name = type(error).__name__
        text = str(error).strip()
        if not text:
            return f"飞书连接异常（{name}）。"
        try:
            app_secret = self.settings_service.app_secret()
        except Exception:
            app_secret = None
        if app_secret:
            text = text.replace(app_secret, "[已隐藏]")
        return f"飞书连接异常（{name}）：{text[:180]}"
