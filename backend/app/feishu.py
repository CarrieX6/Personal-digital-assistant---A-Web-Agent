from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
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
        viewer_link_factory: Callable[[str], str] | None = None,
        job_poll_interval: float = 2,
        job_timeout_seconds: float = 10 * 60,
    ) -> None:
        self.settings_service = settings_service
        self.runner = runner
        self.store = store
        self.channel_factory = channel_factory
        self.spatial_service = spatial_service
        self.viewer_link_factory = viewer_link_factory
        self.job_poll_interval = job_poll_interval
        self.job_timeout_seconds = job_timeout_seconds
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
            await self._process_image_message(
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
            content=f"[功能卡片] {self._card_command_label(command)}",
        )
        self._spawn(
            self._process_card_action(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                command=command,
            )
        )

    async def _process_card_action(
        self,
        *,
        chat_id: str,
        message_id: str,
        sender_id: str,
        command: str,
    ) -> None:
        if command == "spatial_photo":
            await self._reply_safely(
                chat_id,
                message_id,
                "请直接发送一张 JPG、PNG 或 WebP 图片。我会在本机生成空间照片并返回封面。",
                "card-spatial-help",
            )
            return
        if command == "photo_style_transfer":
            await self._reply_safely(
                chat_id,
                message_id,
                (
                    "图片风格化已上线，需要 1 张内容图和 1–3 张风格参考图。\n"
                    "当前请在电脑端 Web 控制台的“工具库 → 图片风格化”中上传；"
                    "飞书多图角色收集链路仍在开发中，暂不能在聊天内直接提交。"
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

    async def _process_image_message(
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
            job = await self._wait_for_job(created.job.id)
            if job.status == "failed":
                self.store.mark_failed(message_id, "spatial_job_failed")
                await self._reply_safely(
                    chat_id,
                    message_id,
                    f"空间照片生成失败：{job.error or job.message}",
                    "image-job-failed",
                )
                return

            asset = await asyncio.to_thread(spatial.get_asset, job.asset_id)
            self.store.mark_completed(message_id, job.id)
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
                "image-job-completed",
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
                        self._uuid(message_id, "image-preview"),
                        media_url=asset.preview_url,
                    )
                except Exception as exc:
                    self._status = "error"
                    self._last_error = self._safe_error(exc)
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
            "list_assets": "查看个人资产",
            "capabilities": "能力列表",
            "current_time": "当前时间",
        }.get(command, "未知功能")

    def _owner_id(self, sender_id: str) -> str:
        return f"feishu:{self._settings.app_id}:{sender_id}"

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
