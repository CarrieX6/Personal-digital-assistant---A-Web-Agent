from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any, Callable, Protocol

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
from .models import ChannelMessagePublic, FeishuRuntimePublic
from .style_transfer import PhotoStyleService, StyleParameters


FEISHU_IMAGE_FILE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


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


def _install_isolated_lark_ws_loop() -> asyncio.AbstractEventLoop:
    """Keep the SDK's module-level WS loop away from the ASGI event loop."""

    from lark_channel.ws import client as ws_client_module

    _install_lark_clean_close_handler()
    # Creating a ProactorEventLoop on the Windows main thread replaces the
    # process-wide signal wakeup fd. Closing that secondary loop later leaves
    # Uvicorn's SIGINT handler writing to a closed socket (WinError 10038).
    # The selector loop supports this WebSocket client without taking over the
    # signal wakeup fd, so the ASGI loop remains the sole signal owner.
    isolated_loop = (
        asyncio.SelectorEventLoop()
        if os.name == "nt"
        else asyncio.new_event_loop()
    )
    ws_client_module.loop = isolated_loop
    return isolated_loop


def _install_lark_clean_close_handler() -> None:
    """Treat a server WebSocket 1000 close as reconnectable, not an error.

    lark-channel currently logs every ``ConnectionClosedOK`` at ERROR and
    re-raises it while a deliberate stop is in progress. Its other receive
    failures and reconnect policy remain unchanged by this compatibility shim.
    """

    from lark_channel.ws import client as ws_client_module
    from websockets.exceptions import ConnectionClosedOK

    client_type = ws_client_module.Client
    current = client_type._receive_message_loop
    if getattr(current, "_personal_assistant_clean_close", False):
        return

    async def receive_message_loop(client: Any, connection: Any) -> None:
        try:
            while True:
                message = await connection.recv()
                await client._schedule_handle_message(message)
        except ConnectionClosedOK:
            if client._auto_reconnect:
                await client._disconnect_and_reconnect(
                    expected_conn=connection,
                )
            else:
                await client._disconnect(expected_conn=connection)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ws_client_module.logger.error(
                client._fmt_log("receive message loop exit, err: {}", exc)
            )
            if client._auto_reconnect:
                await client._disconnect_and_reconnect(
                    expected_conn=connection,
                )
            else:
                await client._disconnect(expected_conn=connection)
                raise

    setattr(receive_message_loop, "_personal_assistant_clean_close", True)
    setattr(client_type, "_receive_message_loop", receive_message_loop)


def _install_bounded_lark_disconnect(ws_client: Any) -> None:
    """Replace the SDK's lock-bound disconnect only for local shutdown."""

    async def bounded_disconnect(
        client: Any,
        *,
        expected_conn: Any = None,
    ) -> bool:
        connection = getattr(client, "_conn", None)
        if connection is None:
            return False
        if expected_conn is not None and connection is not expected_conn:
            return False

        # Clear first so a simultaneous receive-loop close becomes a no-op.
        client._conn = None
        client._conn_url = ""
        client._conn_id = ""
        client._service_id = ""
        try:
            await asyncio.wait_for(connection.close(), timeout=0.75)
        except Exception:
            pass
        return True

    ws_client._disconnect = MethodType(bounded_disconnect, ws_client)


def _bind_lark_channel_to_loop(
    channel: FeishuChannel,
    isolated_loop: asyncio.AbstractEventLoop,
) -> None:
    """Make SDK helpers created in its executor thread use the isolated loop."""

    start = channel.start

    def start_on_isolated_loop() -> None:
        asyncio.set_event_loop(isolated_loop)
        try:
            start()
        finally:
            asyncio.set_event_loop(None)

    channel.start = start_on_isolated_loop  # type: ignore[method-assign]


def _drain_and_close_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)

    async def cancel_pending_tasks() -> None:
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks(loop)
            if task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        loop.run_until_complete(cancel_pending_tasks())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class FeishuResourceError(AssetError):
    """User-actionable media failure without exposing credentials."""


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

    def claim_feature_menu(
        self,
        chat_id: str,
        *,
        max_age_seconds: float | None = None,
        force: bool = False,
    ) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT sent_at FROM channel_feature_menu WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is not None and not force and (
                max_age_seconds is None
                or float(row[0]) >= time.time() - max_age_seconds
            ):
                return False
            connection.execute(
                """
                INSERT INTO channel_feature_menu (chat_id, sent_at)
                VALUES (?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET sent_at = excluded.sent_at
                """,
                (chat_id, time.time()),
            )
            return True

    def release_feature_menu(self, chat_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM channel_feature_menu WHERE chat_id = ?",
                (chat_id,),
            )

    def start_style_collection(
        self,
        chat_id: str,
        sender_id: str,
        *,
        description: str = "",
    ) -> list[tuple[str, str]]:
        """Start a durable per-user collection and return replaced images."""

        normalized_description = description.strip()
        if len(normalized_description) > 1000:
            raise ValueError("图片风格化补充描述不能超过 1000 个字符。")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT images_json FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            ).fetchone()
            previous = self._decode_style_images(row[0]) if row else []
            connection.execute(
                """
                INSERT INTO channel_style_collections (
                    chat_id, sender_id, images_json, description, updated_at
                ) VALUES (?, ?, '[]', ?, ?)
                ON CONFLICT(chat_id, sender_id)
                DO UPDATE SET
                    images_json = '[]',
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (chat_id, sender_id, normalized_description, time.time()),
            )
        return previous

    def style_collection_draft(
        self,
        chat_id: str,
        sender_id: str,
        *,
        max_age_seconds: float,
    ) -> tuple[list[tuple[str, str]], str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT images_json, description, updated_at
                FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            ).fetchone()
            if row is None:
                return None
            if float(row[2]) < time.time() - max_age_seconds:
                connection.execute(
                    """
                    DELETE FROM channel_style_collections
                    WHERE chat_id = ? AND sender_id = ?
                    """,
                    (chat_id, sender_id),
                )
                return None
            return self._decode_style_images(row[0]), str(row[1] or "")

    def style_collection(
        self,
        chat_id: str,
        sender_id: str,
        *,
        max_age_seconds: float,
    ) -> list[tuple[str, str]] | None:
        draft = self.style_collection_draft(
            chat_id,
            sender_id,
            max_age_seconds=max_age_seconds,
        )
        return draft[0] if draft is not None else None

    def update_style_description(
        self,
        chat_id: str,
        sender_id: str,
        *,
        description: str,
        append: bool,
        max_age_seconds: float,
    ) -> tuple[list[tuple[str, str]], str] | None:
        normalized = description.strip()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT images_json, description, updated_at
                FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            ).fetchone()
            if row is None or float(row[2]) < time.time() - max_age_seconds:
                if row is not None:
                    connection.execute(
                        """
                        DELETE FROM channel_style_collections
                        WHERE chat_id = ? AND sender_id = ?
                        """,
                        (chat_id, sender_id),
                    )
                return None
            current = str(row[1] or "").strip()
            updated = (
                "\n".join(item for item in (current, normalized) if item)
                if append
                else normalized
            )
            if len(updated) > 1000:
                raise ValueError("图片风格化补充描述不能超过 1000 个字符。")
            connection.execute(
                """
                UPDATE channel_style_collections
                SET description = ?, updated_at = ?
                WHERE chat_id = ? AND sender_id = ?
                """,
                (updated, time.time(), chat_id, sender_id),
            )
            return self._decode_style_images(row[0]), updated

    def append_style_image(
        self,
        chat_id: str,
        sender_id: str,
        *,
        source_image_id: str,
        message_id: str,
        max_age_seconds: float,
    ) -> list[tuple[str, str]] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT images_json, updated_at
                FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            ).fetchone()
            if row is None or float(row[1]) < time.time() - max_age_seconds:
                if row is not None:
                    connection.execute(
                        """
                        DELETE FROM channel_style_collections
                        WHERE chat_id = ? AND sender_id = ?
                        """,
                        (chat_id, sender_id),
                    )
                return None
            images = self._decode_style_images(row[0])
            if len(images) >= 4:
                raise ValueError("图片风格化最多收集四张图片。")
            images.append((source_image_id, message_id))
            payload = [
                {"source_image_id": source_id, "message_id": linked_message_id}
                for source_id, linked_message_id in images
            ]
            connection.execute(
                """
                UPDATE channel_style_collections
                SET images_json = ?, updated_at = ?
                WHERE chat_id = ? AND sender_id = ?
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                    chat_id,
                    sender_id,
                ),
            )
            return images

    def clear_style_collection(
        self,
        chat_id: str,
        sender_id: str,
    ) -> list[tuple[str, str]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT images_json FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            ).fetchone()
            connection.execute(
                """
                DELETE FROM channel_style_collections
                WHERE chat_id = ? AND sender_id = ?
                """,
                (chat_id, sender_id),
            )
        return self._decode_style_images(row[0]) if row else []

    @staticmethod
    def _decode_style_images(value: Any) -> list[tuple[str, str]]:
        try:
            payload = json.loads(str(value))
        except (TypeError, ValueError):
            return []
        images: list[tuple[str, str]] = []
        if not isinstance(payload, list):
            return images
        for item in payload[:4]:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_image_id", ""))
            message_id = str(item.get("message_id", ""))
            if (
                source_id
                and Path(source_id).name == source_id
                and len(source_id) <= 100
                and message_id
                and len(message_id) <= 200
            ):
                images.append((source_id, message_id))
        return images

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

                CREATE TABLE IF NOT EXISTS channel_style_collections (
                    chat_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    images_json TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, sender_id)
                );
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
            style_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(channel_style_collections)"
                ).fetchall()
            }
            if "description" not in style_columns:
                connection.execute(
                    "ALTER TABLE channel_style_collections "
                    "ADD COLUMN description TEXT NOT NULL DEFAULT ''"
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
    style_collection_ttl_seconds = 30 * 60
    feature_menu_repeat_seconds = 30 * 60

    def __init__(
        self,
        settings_service: FeishuSettingsService,
        runner: AgentRunner,
        store: SQLiteChannelStore,
        channel_factory: ChannelFactory = FeishuChannel,
        spatial_service: SpatialSceneService | None = None,
        style_service: PhotoStyleService | None = None,
        viewer_link_factory: Callable[[str], str] | None = None,
        job_poll_interval: float = 2,
        job_timeout_seconds: float = 10 * 60,
    ) -> None:
        self.settings_service = settings_service
        self.runner = runner
        self.store = store
        self.channel_factory = channel_factory
        self.spatial_service = spatial_service
        self.style_service = style_service
        self.viewer_link_factory = viewer_link_factory
        self.job_poll_interval = job_poll_interval
        self.job_timeout_seconds = job_timeout_seconds
        self._channel: FeishuChannelLike | None = None
        self._settings = settings_service.load()
        self._status = "disabled"
        self._last_error: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._style_collection_lock = asyncio.Lock()
        self._sdk_ws_loop: asyncio.AbstractEventLoop | None = None

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
                if isinstance(channel, FeishuChannel):
                    self._sdk_ws_loop = _install_isolated_lark_ws_loop()
                    _bind_lark_channel_to_loop(channel, self._sdk_ws_loop)
                    try:
                        await asyncio.wait_for(
                            channel.connect_until_ready(timeout=None),
                            timeout=5,
                        )
                    except asyncio.TimeoutError:
                        # A clean server close can enter the SDK's jittered
                        # reconnect delay. Keep that worker alive instead of
                        # stopping its loop during application startup.
                        self._status = "reconnecting"
                        self._last_error = (
                            "飞书长连接暂未稳定，正在后台自动重连。"
                        )
                        self._track_background_start(channel)
                        return
                else:
                    await channel.connect_until_ready(timeout=12)
                self._status = "connected"
            except Exception as exc:
                self._status = "error"
                self._last_error = self._safe_error(exc)
                if self._channel is not None:
                    try:
                        await self._disconnect_channel(self._channel)
                    except Exception:
                        pass
                self._channel = None
                await self._release_sdk_ws_loop()

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
                await self._disconnect_channel(channel)
            except Exception:
                pass
        await self._release_sdk_ws_loop()

    async def _disconnect_channel(self, channel: FeishuChannelLike) -> None:
        if not isinstance(channel, FeishuChannel):
            await channel.disconnect()
            return

        ws_client = getattr(channel, "_ws_client", None)
        if ws_client is not None:
            # A deliberate local stop must not enter the SDK reconnect loop.
            setattr(ws_client, "_auto_reconnect", False)
            _install_bounded_lark_disconnect(ws_client)

        safety = getattr(channel, "_safety", None)
        dispose = getattr(safety, "dispose", None)
        if callable(dispose):
            try:
                await asyncio.wait_for(dispose(), timeout=2)
            except Exception:
                pass

        cancel_background_tasks = getattr(channel, "_cancel_bg_tasks", None)
        if callable(cancel_background_tasks):
            await asyncio.to_thread(cancel_background_tasks)
        await self._close_sdk_device_flow(channel)
        stop_background_loop = getattr(channel, "_stop_bg_loop", None)
        if callable(stop_background_loop):
            await asyncio.to_thread(stop_background_loop, join_timeout=3.0)
        stop = getattr(channel, "stop")
        await asyncio.to_thread(stop, join_timeout=3.0)

    def _track_background_start(self, channel: FeishuChannel) -> None:
        """Consume a late SDK startup failure after readiness timed out."""

        future = getattr(channel, "_start_future", None)
        if future is None:
            return

        def completed(done: asyncio.Future[Any]) -> None:
            if done.cancelled():
                return
            error = done.exception()
            if error is not None and self._channel is channel:
                self._status = "error"
                self._last_error = self._safe_error(error)

        future.add_done_callback(completed)

    async def _close_sdk_device_flow(self, channel: FeishuChannelLike) -> None:
        device_flow = getattr(channel, "_device_flow", None)
        close = getattr(device_flow, "close", None)
        if not callable(close):
            return
        background_loop = getattr(channel, "_bg_loop", None)
        future = None
        try:
            if background_loop is not None and background_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(close(), background_loop)
                await asyncio.wait_for(asyncio.wrap_future(future), timeout=2)
            else:
                await asyncio.wait_for(close(), timeout=2)
        except Exception:
            if future is not None:
                future.cancel()
                try:
                    drain = asyncio.run_coroutine_threadsafe(
                        asyncio.sleep(0),
                        background_loop,
                    )
                    await asyncio.wait_for(asyncio.wrap_future(drain), timeout=0.5)
                except Exception:
                    pass
            # The SDK owns this best-effort client; prevent its synchronous stop
            # path from emitting a second timeout after a failed handshake.
            if device_flow is not None:
                setattr(device_flow, "_http", None)

    async def _release_sdk_ws_loop(self) -> None:
        loop = self._sdk_ws_loop
        self._sdk_ws_loop = None
        if loop is None:
            return
        running_loop = asyncio.get_running_loop()
        deadline = running_loop.time() + 3
        while loop.is_running() and running_loop.time() < deadline:
            await asyncio.sleep(0.02)
        if not loop.is_running() and not loop.is_closed():
            await asyncio.to_thread(_drain_and_close_event_loop, loop)

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
            kind, content = self._event_summary(message)
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
        if self._is_feature_menu_request(text):
            self.store.claim_feature_menu(chat_id, force=True)
            await self._send_feature_menu(chat_id, message_id)
            self.store.mark_completed(message_id, "feature-menu")
            return
        # The menu is a channel affordance, not an Agent answer. Show it on the
        # first authorized message regardless of whether that message is text,
        # an image, or a deterministic workflow command.
        await self._send_feature_menu_once(
            chat_id,
            message_id,
            force=self._requests_photo_style(text),
        )
        image_resources = self._image_resource_entries(message)
        if image_resources:
            style_draft = self.store.style_collection_draft(
                chat_id,
                sender_id,
                max_age_seconds=self.style_collection_ttl_seconds,
            )
            description_update = self._style_description_update(
                text,
                allow_plain=style_draft is not None,
            )
            if style_draft is None and (
                len(image_resources) > 1 or self._looks_like_style_request(text)
            ):
                description_update = self._style_description_update(
                    text,
                    allow_plain=True,
                )
                initial_description = (
                    self._photo_style_request_description(text)
                    if self._looks_like_style_request(text)
                    else (
                        description_update[0]
                        if description_update is not None
                        else ""
                    )
                )
                started = await self._begin_photo_style_collection(
                    chat_id=chat_id,
                    message_id=message_id,
                    sender_id=sender_id,
                    initial_description=initial_description,
                    announce=False,
                )
                if not started:
                    return
                style_draft = ([], initial_description)
                description_update = None
            if style_draft is not None:
                remaining = 4 - len(style_draft[0])
                if len(image_resources) > remaining:
                    self.store.mark_rejected(message_id, "too_many_style_images")
                    await self._reply_safely(
                        chat_id,
                        message_id,
                        (
                            f"图片风格化当前还可接收 {remaining} 张图片，"
                            f"本次选择了 {len(image_resources)} 张。"
                            "请减少选择数量后重新发送。"
                        ),
                        "too-many-style-images",
                    )
                    return
                await self._collect_photo_style_images(
                    message_id=message_id,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    resources=image_resources,
                    description_update=description_update,
                )
                return
            (
                file_key,
                file_name,
                resource_message_id,
                resource_type,
            ) = image_resources[0]
            await self._process_image_message(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                file_key=file_key,
                file_name=file_name,
                resource_message_id=resource_message_id,
                resource_type=resource_type,
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
                "当前版本支持文本、图片及 JPG / PNG / WebP 图片文件；"
                "其他文件与视频将在后续接入。",
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

        compact_command = re.sub(r"\s+", "", text).casefold()
        active_style_draft = self.store.style_collection_draft(
            chat_id,
            sender_id,
            max_age_seconds=self.style_collection_ttl_seconds,
        )
        if self._requests_spatial_photo(text):
            discarded = await self._discard_photo_style_collection(
                chat_id=chat_id,
                sender_id=sender_id,
            )
            self.store.mark_completed(message_id, "spatial-photo-selected")
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "已切换到空间照片。原图片风格化草稿已清理。\n"
                    if discarded
                    else "已进入空间照片模式。\n"
                )
                + "请发送一张 JPG、PNG 或 WebP 图片，我会创建空间照片任务。",
                "spatial-photo-selected",
            )
            return
        if active_style_draft is None and self._requests_photo_style(text):
            await self._begin_photo_style_collection(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                initial_description=self._photo_style_request_description(text),
            )
            return
        if active_style_draft is not None and self._requests_photo_style(text):
            self.store.mark_completed(message_id, "photo-style-draft-status")
            await self._send_photo_style_draft_card(
                chat_id=chat_id,
                message_id=message_id,
                images=active_style_draft[0],
                description=active_style_draft[1],
                phase="photo-style-draft-status",
                notice="图片风格化草稿已在进行，请继续上传图片或补充描述。",
            )
            return
        if compact_command in {"取消风格化", "退出风格化", "取消图片风格化"} or (
            active_style_draft is not None and compact_command in {"取消", "退出"}
        ):
            await self._cancel_photo_style_collection(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
            )
            return
        if active_style_draft is not None:
            if compact_command in {
                "查看风格化任务",
                "风格化任务",
                "当前风格化任务",
                "查看任务",
                "当前任务",
            }:
                self.store.mark_completed(message_id, "photo-style-draft-status")
                await self._send_photo_style_draft_card(
                    chat_id=chat_id,
                    message_id=message_id,
                    images=active_style_draft[0],
                    description=active_style_draft[1],
                    phase="photo-style-draft-status",
                )
                return
            if compact_command in {"清空描述", "删除描述", "清除描述"}:
                updated = await asyncio.to_thread(
                    self.store.update_style_description,
                    chat_id,
                    sender_id,
                    description="",
                    append=False,
                    max_age_seconds=self.style_collection_ttl_seconds,
                )
                if updated is not None:
                    self.store.mark_completed(
                        message_id,
                        "photo-style-description-cleared",
                    )
                    await self._send_photo_style_draft_card(
                        chat_id=chat_id,
                        message_id=message_id,
                        images=updated[0],
                        description=updated[1],
                        phase="photo-style-description-cleared",
                        notice="补充描述已清空。",
                    )
                return
            if self._starts_photo_style_generation(text):
                description_update = self._style_description_update(
                    text,
                    allow_plain=False,
                )
                if description_update is not None:
                    try:
                        await asyncio.to_thread(
                            self.store.update_style_description,
                            chat_id,
                            sender_id,
                            description=description_update[0],
                            append=description_update[1],
                            max_age_seconds=self.style_collection_ttl_seconds,
                        )
                    except ValueError as exc:
                        self.store.mark_rejected(
                            message_id,
                            "photo_style_description_too_long",
                        )
                        await self._reply_safely(
                            chat_id,
                            message_id,
                            str(exc),
                            "photo-style-description-too-long",
                        )
                        return
                await self._start_photo_style_transfer(
                    chat_id=chat_id,
                    message_id=message_id,
                    sender_id=sender_id,
                )
                return
            description_update = self._style_description_update(
                text,
                allow_plain=True,
            )
            if description_update is not None:
                try:
                    updated = await asyncio.to_thread(
                        self.store.update_style_description,
                        chat_id,
                        sender_id,
                        description=description_update[0],
                        append=description_update[1],
                        max_age_seconds=self.style_collection_ttl_seconds,
                    )
                except ValueError as exc:
                    self.store.mark_rejected(
                        message_id,
                        "photo_style_description_too_long",
                    )
                    await self._reply_safely(
                        chat_id,
                        message_id,
                        str(exc),
                        "photo-style-description-too-long",
                    )
                    return
                if updated is not None:
                    self.store.mark_completed(
                        message_id,
                        "photo-style-description-updated",
                    )
                    await self._send_photo_style_draft_card(
                        chat_id=chat_id,
                        message_id=message_id,
                        images=updated[0],
                        description=updated[1],
                        phase="photo-style-description-updated",
                        notice="补充描述已保存，可继续上传图片或开始生成。",
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
        if (
            not chat_id
            or not message_id
            or sender_id not in self._settings.allowed_open_ids
            or not isinstance(command, str)
        ):
            return
        self.store.record_event(
            chat_id=chat_id,
            sender_id=sender_id,
            direction="inbound",
            kind="card",
            content=(
                f"[重试卡片] {self._card_command_label(command)}"
                if command == "retry_spatial_job"
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
    ) -> None:
        if command == "retry_spatial_job":
            await self._retry_spatial_job(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                job_id=job_id,
            )
            return
        if command == "spatial_photo":
            discarded = await self._discard_photo_style_collection(
                chat_id=chat_id,
                sender_id=sender_id,
            )
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "已切换到空间照片，之前的图片风格化草稿已清理。\n"
                    if discarded
                    else "已进入空间照片模式。\n"
                )
                + "请发送一张 JPG、PNG 或 WebP 图片，我会在本机生成空间照片并返回封面。",
                "card-spatial-help",
            )
            return
        if command == "photo_style_transfer":
            await self._begin_photo_style_collection(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
            )
            return
        if command == "photo_style_start":
            await self._start_photo_style_transfer(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
            )
            return
        if command == "photo_style_cancel":
            await self._cancel_photo_style_collection(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
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

    async def _send_feature_menu_once(
        self,
        chat_id: str,
        message_id: str,
        *,
        force: bool = False,
    ) -> None:
        if not self.store.claim_feature_menu(
            chat_id,
            max_age_seconds=self.feature_menu_repeat_seconds,
            force=force,
        ):
            return
        try:
            await self._send_feature_menu(chat_id, message_id)
        except Exception as exc:
            self.store.release_feature_menu(chat_id)
            self._status = "error"
            self._last_error = self._safe_error(exc)

    async def _begin_photo_style_collection(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        initial_description: str = "",
        announce: bool = True,
    ) -> bool:
        spatial = self.spatial_service
        style = self.style_service
        async with self._style_collection_lock:
            # Persist the routing mode before the first await. Mobile Feishu can
            # dispatch a selected photo immediately after the card click; if the
            # mode were created only after a provider health check, that photo
            # could incorrectly fall through to the spatial-photo route.
            try:
                previous = self.store.start_style_collection(
                    chat_id,
                    sender_id,
                    description=initial_description,
                )
            except ValueError as exc:
                self.store.mark_rejected(
                    message_id,
                    "photo_style_description_too_long",
                )
                await self._reply_safely(
                    chat_id,
                    message_id,
                    str(exc),
                    "photo-style-description-too-long",
                )
                return False
            await self._delete_staged_style_images(
                [source_id for source_id, _ in previous],
                owner_id=self._owner_id(sender_id),
            )

        self.store.mark_completed(message_id, "photo-style-collection")
        if not announce:
            return True
        provider_ready: bool | None = None
        if style is not None:
            provider_status = await asyncio.to_thread(style.provider_status)
            provider_ready = provider_status.get("ready")
        availability_note = ""
        if spatial is None or style is None:
            availability_note = (
                "电脑端图片风格化服务尚未加载；图片不会转入空间照片，"
                "请启动服务后再上传。"
            )
        elif provider_ready is False:
            availability_note = (
                "SDXL + IP-Adapter 服务当前未通过健康检查。"
                "你仍可先上传图片，服务恢复后再发送“开始风格化”。"
            )
        await self._send_photo_style_draft_card(
            chat_id=chat_id,
            message_id=message_id,
            images=[],
            description=initial_description.strip(),
            phase="photo-style-collection-started",
            notice=availability_note or "风格化草稿已创建，30 分钟内持续有效。",
        )
        return True

    async def _cancel_photo_style_collection(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
    ) -> None:
        images = await self._discard_photo_style_collection(
            chat_id=chat_id,
            sender_id=sender_id,
        )
        self.store.mark_completed(message_id, "photo-style-cancelled")
        await self._reply_safely(
            chat_id,
            message_id,
            (
                "图片风格化模式已取消，临时图片已经清理。"
                if images
                else "当前没有正在收集的图片风格化任务。"
            ),
            "photo-style-cancelled",
        )

    async def _discard_photo_style_collection(
        self,
        *,
        chat_id: str,
        sender_id: str,
    ) -> bool:
        async with self._style_collection_lock:
            images = await asyncio.to_thread(
                self.store.clear_style_collection,
                chat_id,
                sender_id,
            )
            await self._delete_staged_style_images(
                [source_id for source_id, _ in images],
                owner_id=self._owner_id(sender_id),
            )
        return bool(images)

    async def _collect_photo_style_images(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        resources: list[tuple[str, str | None, str, str]],
        description_update: tuple[str, bool] | None = None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None or self.style_service is None:
            self.store.mark_failed(message_id, "photo_style_unavailable")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化能力当前不可用，请确认电脑端服务已经启动。",
                "photo-style-unavailable",
            )
            return

        await self._reply_safely(
            chat_id,
            message_id,
            f"已收到 {len(resources)} 张图片，正在下载并加入风格化草稿。",
            "photo-style-images-accepted",
        )
        pending_source_id: str | None = None
        try:
            channel = self._channel
            if channel is None:
                raise RuntimeError("飞书长连接当前不可用")
            async with self._style_collection_lock:
                draft = await asyncio.to_thread(
                    self.store.style_collection_draft,
                    chat_id,
                    sender_id,
                    max_age_seconds=self.style_collection_ttl_seconds,
                )
                if draft is None:
                    raise LookupError("photo_style_collection_expired")
                if len(draft[0]) + len(resources) > 4:
                    raise ValueError("图片风格化最多收集四张图片。")
                images = draft[0]
                description = draft[1]
                for (
                    file_key,
                    file_name,
                    resource_message_id,
                    resource_type,
                ) in resources:
                    image_bytes = await self._download_image_resource(
                        channel,
                        file_key=file_key,
                        message_id=resource_message_id or message_id,
                        resource_type=resource_type,
                    )
                    source = await asyncio.to_thread(
                        spatial.stage_source_image,
                        image_bytes,
                        original_name=file_name or "feishu-style-image.jpg",
                        owner_id=self._owner_id(sender_id),
                    )
                    pending_source_id = source.id
                    appended = await asyncio.to_thread(
                        self.store.append_style_image,
                        chat_id,
                        sender_id,
                        source_image_id=source.id,
                        message_id=message_id,
                        max_age_seconds=self.style_collection_ttl_seconds,
                    )
                    if appended is None:
                        raise LookupError("photo_style_collection_expired")
                    images = appended
                    pending_source_id = None
                if description_update is not None:
                    updated = await asyncio.to_thread(
                        self.store.update_style_description,
                        chat_id,
                        sender_id,
                        description=description_update[0],
                        append=description_update[1],
                        max_age_seconds=self.style_collection_ttl_seconds,
                    )
                    if updated is None:
                        raise LookupError("photo_style_collection_expired")
                    images, description = updated
            count = len(images)
            self.store.mark_completed(message_id, f"photo-style-images-{count}")
            await self._send_photo_style_draft_card(
                chat_id=chat_id,
                message_id=message_id,
                images=images,
                description=description,
                phase=f"photo-style-images-{count}-stored",
                notice=(
                    f"本次新增 {len(resources)} 张图片。"
                    "系统按发送顺序将首张识别为内容图，其余识别为风格参考图。"
                    if len(resources) > 1 and count == len(resources)
                    else f"本次新增 {len(resources)} 张图片，已保持原发送顺序。"
                )
            )
        except LookupError:
            if pending_source_id:
                await self._delete_staged_style_images(
                    [pending_source_id],
                    owner_id=self._owner_id(sender_id),
                )
            self.store.mark_rejected(message_id, "photo_style_collection_expired")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片收集会话已过期，请重新发送“图片风格化”后上传。",
                "photo-style-collection-expired",
            )
        except ValueError as exc:
            if pending_source_id:
                await self._delete_staged_style_images(
                    [pending_source_id],
                    owner_id=self._owner_id(sender_id),
                )
            self.store.mark_rejected(message_id, "photo_style_image_limit")
            await self._reply_safely(
                chat_id,
                message_id,
                f"{exc} 请发送“开始风格化”或“取消风格化”。",
                "photo-style-image-limit",
            )
        except AssetError as exc:
            if pending_source_id:
                await self._delete_staged_style_images(
                    [pending_source_id],
                    owner_id=self._owner_id(sender_id),
                )
            self.store.mark_failed(message_id, "invalid_style_image")
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片无法处理：{exc}",
                "invalid-style-image",
            )
        except Exception:
            if pending_source_id:
                await self._delete_staged_style_images(
                    [pending_source_id],
                    owner_id=self._owner_id(sender_id),
                )
            self.store.mark_failed(message_id, "style_image_download_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片下载失败，请检查消息资源权限和电脑端日志后重试。",
                "style-image-download-failed",
            )

    async def _start_photo_style_transfer(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
    ) -> None:
        style = self.style_service
        if style is None:
            self.store.mark_failed(message_id, "photo_style_unavailable")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化能力当前不可用，请确认电脑端服务已经启动。",
                "photo-style-unavailable",
            )
            return
        provider_status = await asyncio.to_thread(style.provider_status)
        if provider_status.get("ready") is False:
            self.store.mark_rejected(
                message_id,
                "photo_style_provider_unavailable",
            )
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片已保留，但 SDXL + IP-Adapter 服务当前未就绪。"
                    "请在电脑端恢复服务后再次发送“开始风格化”；"
                    "这些图片不会被送往空间照片功能。"
                ),
                "photo-style-provider-unavailable",
            )
            return
        try:
            async with self._style_collection_lock:
                draft = await asyncio.to_thread(
                    self.store.style_collection_draft,
                    chat_id,
                    sender_id,
                    max_age_seconds=self.style_collection_ttl_seconds,
                )
                if draft is None:
                    self.store.mark_rejected(
                        message_id,
                        "photo_style_collection_missing",
                    )
                    await self._reply_safely(
                        chat_id,
                        message_id,
                        "当前没有图片风格化收集会话，请先发送“图片风格化”。",
                        "photo-style-collection-missing",
                    )
                    return
                images, description = draft
                if len(images) < 2:
                    self.store.mark_rejected(
                        message_id,
                        "photo_style_reference_missing",
                    )
                    await self._reply_safely(
                        chat_id,
                        message_id,
                        "还缺少风格参考图，请至少再发送一张图片。",
                        "photo-style-reference-missing",
                    )
                    return
                source_ids = [source_id for source_id, _ in images]
                created = await asyncio.to_thread(
                    style.create_transfer_from_sources,
                    source_ids[0],
                    source_ids[1:],
                    title="飞书图片风格化",
                    parameters=StyleParameters(prompt=description),
                    owner_id=self._owner_id(sender_id),
                )
                await asyncio.to_thread(
                    self.store.clear_style_collection,
                    chat_id,
                    sender_id,
                )
            linked_urls = [
                created.asset.source_url,
                *created.asset.style_reference_urls,
            ]
            for (_, linked_message_id), media_url in zip(images, linked_urls):
                self.store.attach_event_media(linked_message_id, media_url)
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片风格化任务已创建。\n"
                    f"任务 ID：{created.job.id}\n"
                    f"补充描述：{description or '未填写，使用模型默认描述'}\n"
                    "本机正在处理，完成后会自动返回预览图和可下载文件。"
                ),
                "photo-style-job-created",
            )
            await self._deliver_photo_style_result(
                chat_id=chat_id,
                message_id=message_id,
                job_id=created.job.id,
                tracked_message_id=message_id,
            )
        except AssetError as exc:
            self.store.mark_failed(message_id, "photo_style_create_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片风格化任务无法创建：{exc}",
                "photo-style-create-failed",
            )
        except TimeoutError:
            self.store.mark_failed(message_id, "photo_style_job_timeout")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化超过 10 分钟，请在电脑端个人资产库查看进度。",
                "photo-style-job-timeout",
            )
        except Exception:
            self.store.mark_failed(message_id, "photo_style_execution_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                "图片风格化执行失败，请在电脑端查看日志后重试。",
                "photo-style-execution-failed",
            )

    async def _deliver_photo_style_result(
        self,
        *,
        chat_id: str,
        message_id: str,
        job_id: str,
        tracked_message_id: str,
    ) -> None:
        style = self.style_service
        if style is None:
            raise AssetError("图片风格化能力当前不可用。")
        job = await self._wait_for_photo_style_job(job_id)
        if job.status == "failed":
            self.store.mark_failed(tracked_message_id, "photo_style_job_failed")
            await self._reply_safely(
                chat_id,
                message_id,
                f"图片风格化失败：{job.error or job.message}",
                f"photo-style-job-{job.id}-failed",
            )
            return

        asset = await asyncio.to_thread(
            style.asset_library.get_asset,
            job.asset_id,
        )
        if not asset.result_url:
            raise AssetError("图片风格化任务完成，但结果文件不存在。")
        result_name = asset.result_url.rsplit("/", maxsplit=1)[-1]
        result_path = await asyncio.to_thread(
            style.asset_library.resolve_asset_file,
            asset.id,
            result_name,
        )
        await self._reply_safely(
            chat_id,
            message_id,
            (
                f"图片风格化“{asset.name}”已完成。\n"
                f"尺寸：{asset.width} × {asset.height}\n"
                "下方先发送预览图，再发送可下载的原始结果文件。"
            ),
            f"photo-style-job-{job.id}-completed",
        )
        preview_delivered = False
        file_delivered = False
        try:
            await self._send_image(
                chat_id,
                message_id,
                result_path,
                self._uuid(message_id, f"photo-style-{job.id}-preview"),
                media_url=asset.result_url,
            )
            preview_delivered = True
        except Exception as exc:
            self._last_error = self._safe_error(exc)
        try:
            await self._send_file(
                chat_id,
                message_id,
                result_path,
                self._uuid(message_id, f"photo-style-{job.id}-download"),
                file_name=f"style-result-{asset.id[:8]}.webp",
                media_url=asset.result_url,
            )
            file_delivered = True
        except Exception as exc:
            self._last_error = self._safe_error(exc)
        if preview_delivered or file_delivered:
            self.store.mark_completed(tracked_message_id, job.id)
            if not file_delivered:
                await self._reply_safely(
                    chat_id,
                    message_id,
                    (
                        "原始结果文件上传失败，可先长按预览图保存；"
                        "完整结果仍保留在电脑端个人资产库。"
                    ),
                    f"photo-style-{job.id}-file-delivery-failed",
                )
        else:
            self.store.mark_failed(
                tracked_message_id,
                "photo_style_result_delivery_failed",
            )
            await self._reply_safely(
                chat_id,
                message_id,
                "结果已保存在电脑端，但上传到飞书失败，请检查图片/文件上传权限。",
                f"photo-style-{job.id}-delivery-failed",
            )

    async def _delete_staged_style_images(
        self,
        source_ids: list[str],
        *,
        owner_id: str,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None:
            return
        for source_id in source_ids:
            try:
                await asyncio.to_thread(
                    spatial.delete_source_image,
                    source_id,
                    owner_id=owner_id,
                )
            except AssetError:
                pass

    async def _process_image_message(
        self,
        *,
        message_id: str,
        chat_id: str,
        sender_id: str,
        file_key: str,
        file_name: str | None,
        resource_message_id: str | None = None,
        resource_type: str = "image",
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
                message_id=resource_message_id or message_id,
                resource_type=resource_type,
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
            self.store.mark_failed(message_id, "spatial_job_timeout")
            await self._reply_safely(
                chat_id,
                message_id,
                "空间照片处理时间超过 10 分钟，请在电脑端任务列表查看进度。",
                "image-job-timeout",
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
        )

    async def _deliver_spatial_job_result(
        self,
        *,
        chat_id: str,
        message_id: str,
        job_id: str,
        tracked_message_id: str | None,
    ) -> None:
        spatial = self.spatial_service
        if spatial is None:
            raise AssetError("空间照片能力当前不可用。")
        job = await self._wait_for_job(job_id)
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

        asset = await asyncio.to_thread(spatial.get_asset, job.asset_id)
        if tracked_message_id:
            self.store.mark_completed(tracked_message_id, job.id)
        viewer_line = "可动视角请在电脑端个人资产库中打开。"
        if self.viewer_link_factory is not None:
            try:
                viewer_url = await asyncio.to_thread(
                    self.viewer_link_factory,
                    asset.id,
                )
                viewer_line = (
                    "手机全屏可动预览（需与电脑同一局域网）：\n"
                    f"{viewer_url}"
                )
            except Exception as exc:
                self._last_error = self._safe_error(exc)
        await self._reply_safely(
            chat_id,
            message_id,
            (
                f"空间照片“{asset.name}”已生成完成。\n"
                f"尺寸：{asset.width} × {asset.height}\n"
                f"下方先返回封面图。\n{viewer_line}"
            ),
            f"spatial-job-{job.id}-completed",
        )
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
            except Exception as exc:
                self._status = "error"
                self._last_error = self._safe_error(exc)

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
        resource_type: str = "image",
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

            requests = [
                (
                    client.im.v1.message_resource,
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type(resource_type)
                    .build(),
                )
            ]
            if resource_type == "image":
                requests.append((
                    client.im.v1.image,
                    GetImageRequest.builder().image_key(file_key).build(),
                ))
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
                    resource_type=resource_type,
                    message_id=linked_message_id,
                )
                if payload:
                    return payload

        permission_denied = any(code == 99991672 for code, _ in errors)
        suffix = "（平台错误码 99991672）" if permission_denied else ""
        raise FeishuResourceError(
            "无法下载飞书图片或图片文件。请在开发者后台开通 im:resource，"
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

    async def _wait_for_job(self, job_id: str) -> Any:
        spatial = self.spatial_service
        if spatial is None:
            raise RuntimeError("空间照片能力当前不可用")
        deadline = time.monotonic() + self.job_timeout_seconds
        while time.monotonic() < deadline:
            job = await asyncio.to_thread(spatial.get_job, job_id)
            if job.status in {"completed", "failed"}:
                return job
            await asyncio.sleep(self.job_poll_interval)
        raise TimeoutError("spatial job timed out")

    async def _wait_for_photo_style_job(self, job_id: str) -> Any:
        style = self.style_service
        if style is None:
            raise RuntimeError("图片风格化能力当前不可用")
        deadline = time.monotonic() + self.job_timeout_seconds
        while time.monotonic() < deadline:
            job = await asyncio.to_thread(
                style.asset_library.get_job,
                job_id,
            )
            if job.status in {"completed", "failed"}:
                return job
            await asyncio.sleep(self.job_poll_interval)
        raise TimeoutError("photo style job timed out")

    @staticmethod
    def _image_resources(
        message: Any,
    ) -> list[tuple[str, str | None, str]]:
        images: list[tuple[str, str | None, str]] = []
        for resource in list(getattr(message, "resources", []) or []):
            resource_type = str(getattr(resource, "type", "")).casefold()
            file_name = str(getattr(resource, "file_name", "") or "") or None
            if resource_type == "file" and (
                file_name is None
                or Path(file_name).suffix.casefold() not in FEISHU_IMAGE_FILE_SUFFIXES
            ):
                continue
            if resource_type not in {"image", "file"}:
                continue
            file_key = str(getattr(resource, "file_key", "") or "")
            if file_key:
                images.append((file_key, file_name, resource_type))
        if images:
            return images
        content = getattr(message, "content", None)
        image_key = str(getattr(content, "image_key", "") or "")
        return [(image_key, None, "image")] if image_key else []

    @classmethod
    def _image_resource_entries(
        cls,
        message: Any,
    ) -> list[tuple[str, str | None, str, str]]:
        """Return image resources with the message that owns each file key.

        Feishu mobile can send a multi-select as either one post containing
        several image resources or as a rapid media batch. Message-resource
        downloads require the original source message id, not necessarily the
        id of the merged dispatch produced by the SDK.
        """

        sources = list(getattr(message, "batched_sources", None) or [])
        if not sources:
            sources = [message]
        entries: list[tuple[str, str | None, str, str]] = []
        for source in sources:
            source_message_id = str(
                getattr(source, "message_id", None)
                or getattr(source, "id", "")
            )
            for file_key, file_name, resource_type in cls._image_resources(source):
                entries.append(
                    (file_key, file_name, source_message_id, resource_type)
                )
        return entries

    @staticmethod
    def _looks_like_style_request(text: str) -> bool:
        return FeishuChannelRuntime._requests_photo_style(text)

    @staticmethod
    def _is_feature_menu_request(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).casefold()
        return compact in {
            "菜单",
            "功能",
            "功能菜单",
            "功能卡片",
            "功能列表",
            "打开菜单",
            "打开功能卡片",
            "查看功能",
            "查看功能卡片",
            "你能做什么",
            "你会什么",
            "你好",
            "您好",
            "哈喽",
            "嗨",
            "在吗",
            "/menu",
            "menu",
            "/help",
            "help",
            "hi",
            "hello",
        }

    @staticmethod
    def _requests_photo_style(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).casefold()
        has_style_intent = any(
            keyword in compact
            for keyword in (
                "图片风格化",
                "照片风格化",
                "图片风格转换",
                "照片风格转换",
                "图片风格迁移",
                "照片风格迁移",
                "风格迁移",
                "风格化图片",
                "风格化照片",
            )
        ) or compact.startswith("风格化")
        if not has_style_intent:
            return False
        return re.search(
            r"(?:不要|不想|无需|不用|取消|停止).{0,8}"
            r"(?:图片|照片)?(?:风格化|风格转换|风格迁移)",
            compact,
        ) is None

    @staticmethod
    def _photo_style_request_description(text: str) -> str:
        match = re.search(
            r"(?:图片|照片)\s*(?:风格化|风格转换|风格迁移)|"
            r"风格迁移|风格化(?:图片|照片)",
            text,
            flags=re.IGNORECASE,
        )
        if match is None:
            return ""
        tail = text[match.end() :].strip()
        tail = re.sub(r"^(?:转换|转化)?\s*任务", "", tail).strip()
        tail = tail.lstrip("：:，,。；;-— ").strip()
        return "" if tail in {"转换", "转化", "处理"} else tail

    @staticmethod
    def _requests_spatial_photo(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).casefold()
        if re.search(
            r"(?:不要|不想|无需|不用|取消|停止).{0,8}空间(?:照片|图片)",
            compact,
        ):
            return False
        return compact in {"空间照片", "空间图片", "生成空间照片", "生成空间图片"} or (
            re.search(
                r"(?:生成|创建|制作|进入|切换到|我要进行).{0,8}"
                r"空间(?:照片|图片)",
                compact,
            )
            is not None
        )

    @staticmethod
    def _starts_photo_style_collection(text: str) -> bool:
        return FeishuChannelRuntime._requests_photo_style(text)

    @staticmethod
    def _starts_photo_style_generation(text: str) -> bool:
        compact = re.sub(r"\s+", "", text).casefold()
        return compact in {
            "开始",
            "生成",
            "开始生成",
            "开始风格化",
            "生成风格化",
            "执行风格化",
        } or re.match(
            r"^(?:开始风格化|开始生成|执行风格化|生成风格化|生成)"
            r"[：:，,。；;\-—]",
            compact,
        ) is not None

    @staticmethod
    def _style_description_update(
        text: str,
        *,
        allow_plain: bool,
    ) -> tuple[str, bool] | None:
        value = text.strip()
        if not value or value in {"[图片]", "[文件]"}:
            return None
        if re.sub(r"\s+", "", value).casefold() in {
            "内容图",
            "风格图",
            "参考图",
            "风格参考图",
            "图片风格化",
            "开始图片风格化",
            "进入图片风格化",
            "风格迁移",
            "风格化",
            "开始",
            "生成",
            "开始生成",
            "开始风格化",
            "生成风格化",
            "执行风格化",
        }:
            return None
        description_match = re.match(
            r"^\s*(补充|追加|修改|更新|设置)?\s*"
            r"(?:补充)?描述\s*[：:]?\s*(.*)$",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if description_match:
            description = description_match.group(2).strip()
            if not description:
                return None
            append = description_match.group(1) in {"补充", "追加"}
            return description, append
        command_match = re.match(
            r"^\s*(?:开始图片风格化|进入图片风格化|图片风格化|"
            r"照片风格化|风格迁移|开始风格化|开始生成|执行风格化|"
            r"生成风格化)\s*(?:任务)?\s*[：:，,。；;\-—]?\s*(.*)$",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if command_match:
            description = command_match.group(1).strip()
            return (description, True) if description else None
        return (value, True) if allow_plain else None

    async def _send_photo_style_draft_card(
        self,
        *,
        chat_id: str,
        message_id: str,
        images: list[tuple[str, str]],
        description: str,
        phase: str,
        notice: str = "",
    ) -> None:
        image_count = len(images)
        reference_count = max(0, image_count - 1)
        description_preview = description.strip()
        if len(description_preview) > 300:
            description_preview = description_preview[:297] + "..."
        lines = []
        if notice:
            lines.append(notice)
        lines.extend(
            [
                "**内容图**：" + ("已上传 1 张" if image_count else "待上传"),
                f"**风格参考图**：已上传 {reference_count} / 3 张",
                "**补充描述**：" + (description_preview or "未填写，可直接发送文字补充"),
                "**目标风格**：由参考图与补充描述共同决定，不预设固定风格。",
                "支持逐张发送、多选相册、富文本图文，以及 JPG / PNG / WebP 文件。",
                "发送图片时附带的文字，或随后单独发送的文字，都会加入本次任务描述。",
            ]
        )
        buttons: list[dict[str, Any]] = []
        if image_count >= 2:
            buttons.append(
                {
                    "label": "开始生成",
                    "action": {"command": "photo_style_start"},
                    "style": "primary",
                }
            )
        buttons.append(
            {
                "label": "取消草稿",
                "action": {"command": "photo_style_cancel"},
            }
        )
        card = (
            new_card()
            .header(
                title="图片风格化草稿",
                subtitle=f"已收集 {image_count} / 4 张图片 · 30 分钟内有效",
                template="blue" if image_count < 2 else "green",
            )
            .markdown("\n\n".join(lines))
            .buttons(buttons)
            .footer("继续发送图片或描述即可更新；也可发送“开始风格化”")
            .build()
        )
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"card": card.data},
            {"reply_to": message_id, "uuid": self._uuid(message_id, phase)},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="card",
            content=(
                f"[图片风格化草稿] 内容图 {1 if image_count else 0}/1，"
                f"参考图 {reference_count}/3，"
                f"补充描述：{description_preview or '未填写'}"
            ),
            message_id=message_id,
        )

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

    async def _send_file(
        self,
        chat_id: str,
        message_id: str,
        path: Path,
        uuid: str,
        *,
        file_name: str,
        media_url: str | None = None,
    ) -> None:
        channel = self._channel
        if channel is None:
            raise RuntimeError("飞书长连接当前不可用")
        result = await channel.send(
            chat_id,
            {"file": {"source": str(path), "file_name": file_name}},
            {"reply_to": message_id, "uuid": uuid},
        )
        self._ensure_send_success(result)
        self.store.record_event(
            chat_id=chat_id,
            sender_id=None,
            direction="outbound",
            kind="file",
            content=f"[可下载文件] {file_name}",
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
            "photo_style_start": "开始图片风格化",
            "photo_style_cancel": "取消图片风格化草稿",
            "retry_spatial_job": "重新生成空间照片",
            "list_assets": "查看个人资产",
            "capabilities": "能力列表",
            "current_time": "当前时间",
        }.get(command, "未知功能")

    def _owner_id(self, sender_id: str) -> str:
        owner_alias = self._owner_alias(sender_id)
        memory_store = getattr(self.runner, "memory_store", None)
        resolver = getattr(memory_store, "resolve_verified_subject_id", None)
        if callable(resolver):
            return str(resolver(owner_alias))
        # Lightweight runner implementations may not provide linked-identity
        # storage. The channel-qualified alias remains an isolated owner key.
        return owner_alias

    def _owner_alias(self, sender_id: str) -> str:
        return f"feishu:{self._settings.app_id}:{sender_id}"

    def _thread_id(self, chat_id: str, sender_id: str) -> str:
        return f"{self._owner_alias(sender_id)}:{chat_id}"

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
