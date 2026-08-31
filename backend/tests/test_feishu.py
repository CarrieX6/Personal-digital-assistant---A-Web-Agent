from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend.app.assets import SpatialSceneService
from backend.app.channel_settings import (
    FeishuSettingsRepository,
    FeishuSettingsService,
    StoredFeishuSettings,
)
from backend.app.identity import IdentityBindingRegistry
from backend.app.feishu import (
    FeishuChannelRuntime,
    SQLiteChannelStore,
    _bind_lark_channel_to_loop,
    _drain_and_close_event_loop,
    _install_bounded_lark_disconnect,
    _install_isolated_lark_ws_loop,
    _install_lark_clean_close_handler,
)
from backend.app.main import create_app
from backend.app.models import (
    FeishuRuntimePublic,
    FeishuSettingsUpdate,
)
from backend.app.style_transfer import (
    LocalColorStyleProvider,
    PhotoStyleService,
)
from backend.tests.test_api import (
    FakeForegroundSegmenter,
    MemorySecretStore,
    assert_private_file_permissions,
    build_test_settings,
    build_test_spatial,
)


def build_feishu_settings(
    tmp_path: Path,
    http_client: httpx.Client | None = None,
) -> tuple[FeishuSettingsService, MemorySecretStore]:
    secrets = MemorySecretStore()
    return (
        FeishuSettingsService(
            FeishuSettingsRepository(tmp_path / "feishu_settings.json"),
            secrets,
            http_client,
        ),
        secrets,
    )


class FakeRunner:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def run(self, message: str, **_: Any) -> Any:
        self.messages.append(message)
        return SimpleNamespace(
            answer=f"本地 Agent 已处理：{message}",
            run_id=f"run-{len(self.messages)}",
        )


class FakeChannel:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.sent: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.connected = False
        self.download_bytes: bytes | None = None
        self.download_by_key: dict[str, bytes] = {}
        self.download_requests: list[tuple[str, str, str | None]] = []

    def on(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    async def connect_until_ready(self, *, timeout: float | None = 30) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def send(
        self,
        to: str,
        message: dict[str, Any],
        opts: dict[str, Any] | None = None,
    ) -> Any:
        self.sent.append((to, message, opts or {}))
        return SimpleNamespace(success=True)

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None:
        self.download_requests.append((file_key, resource_type, message_id))
        return self.download_by_key.get(file_key, self.download_bytes)


class PermissionDeniedChannel(FakeChannel):
    async def send(
        self,
        to: str,
        message: dict[str, Any],
        opts: dict[str, Any] | None = None,
    ) -> Any:
        self.sent.append((to, message, opts or {}))
        return SimpleNamespace(
            success=False,
            error=SimpleNamespace(
                raw_code=99991672,
                hint="Access denied",
            ),
        )


class ResourceDeniedChannel(FakeChannel):
    def __init__(self) -> None:
        super().__init__()
        self.download_calls: list[str | None] = []

    async def download_resource(
        self,
        file_key: str,
        resource_type: str = "image",
        message_id: str | None = None,
    ) -> bytes | None:
        self.download_calls.append(message_id)
        return None


class FakeRuntime:
    def __init__(self) -> None:
        self.status = FeishuRuntimePublic(status="disabled")
        self.applied: list[StoredFeishuSettings] = []

    def public_status(self) -> FeishuRuntimePublic:
        return self.status

    async def start_if_enabled(self) -> None:
        return None

    async def stop(self) -> None:
        self.status = FeishuRuntimePublic(status="disabled")

    async def apply_settings(self, settings: StoredFeishuSettings) -> None:
        self.applied.append(settings)
        self.status = FeishuRuntimePublic(
            status="connected" if settings.enabled else "disabled"
        )


def test_lark_ws_loop_isolated_from_running_asgi_loop() -> None:
    from lark_channel.ws import client as ws_client_module

    previous_loop = ws_client_module.loop

    async def scenario() -> None:
        ws_client_module.loop = asyncio.get_running_loop()
        isolated_loop = _install_isolated_lark_ws_loop()
        try:
            assert isolated_loop is ws_client_module.loop
            assert isolated_loop is not asyncio.get_running_loop()
            assert isolated_loop.is_running() is False
            if os.name == "nt":
                assert isinstance(isolated_loop, asyncio.SelectorEventLoop)
            await asyncio.to_thread(
                isolated_loop.run_until_complete,
                asyncio.sleep(0),
            )
        finally:
            if not isolated_loop.is_closed():
                isolated_loop.close()
            ws_client_module.loop = previous_loop

    asyncio.run(scenario())


def test_lark_clean_close_reconnects_without_raising() -> None:
    from lark_channel.ws import client as ws_client_module
    from websockets.exceptions import ConnectionClosedOK
    from websockets.frames import Close

    _install_lark_clean_close_handler()

    class CleanCloseConnection:
        async def recv(self) -> bytes:
            raise ConnectionClosedOK(
                Close(1000, "bye"),
                Close(1000, ""),
                False,
            )

    class ClientProbe:
        _auto_reconnect = True
        reconnects = 0

        async def _schedule_handle_message(self, _: bytes) -> None:
            raise AssertionError("正常关闭不应产生消息")

        async def _disconnect_and_reconnect(
            self,
            *,
            expected_conn: Any,
        ) -> None:
            self.reconnects += 1

    probe = ClientProbe()
    asyncio.run(
        ws_client_module.Client._receive_message_loop(
            probe,
            CleanCloseConnection(),
        )
    )

    assert probe.reconnects == 1


def test_lark_local_disconnect_clears_connection_without_sdk_lock() -> None:
    class ConnectionProbe:
        closed = False

        async def close(self) -> None:
            self.closed = True

    connection = ConnectionProbe()
    client = SimpleNamespace(
        _conn=connection,
        _conn_url="wss://example.invalid",
        _conn_id="connection-id",
        _service_id="service-id",
    )
    _install_bounded_lark_disconnect(client)

    disconnected = asyncio.run(client._disconnect())

    assert disconnected is True
    assert connection.closed is True
    assert client._conn is None
    assert client._conn_url == ""
    assert client._conn_id == ""
    assert client._service_id == ""


def test_lark_channel_executor_thread_uses_isolated_loop() -> None:
    isolated_loop = asyncio.new_event_loop()

    class StartProbe:
        captured_loop: asyncio.AbstractEventLoop | None = None

        def start(self) -> None:
            self.captured_loop = asyncio.get_event_loop()
            self.captured_loop.create_task(asyncio.sleep(60))

    probe = StartProbe()
    _bind_lark_channel_to_loop(probe, isolated_loop)  # type: ignore[arg-type]
    asyncio.run(asyncio.to_thread(probe.start))

    assert probe.captured_loop is isolated_loop
    _drain_and_close_event_loop(isolated_loop)
    assert isolated_loop.is_closed()


def test_feishu_settings_keep_secret_out_of_json_and_public_response(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)

    stored = service.save(
        FeishuSettingsUpdate(
            enabled=True,
            app_id="cli_test_app",
            app_secret="secret-sensitive-value",
            domain="feishu",
            allowed_open_ids=[" ou_user_a ", "ou_user_a", "ou_user_b"],
        )
    )
    public = service.public_settings(
        FeishuRuntimePublic(status="connected")
    )

    assert stored.allowed_open_ids == ["ou_user_a", "ou_user_b"]
    assert public.has_app_secret is True
    assert public.masked_app_secret is not None
    assert "secret-sensitive-value" not in public.model_dump_json()
    assert secrets.value == "secret-sensitive-value"
    settings_path = tmp_path / "feishu_settings.json"
    assert "secret-sensitive-value" not in settings_path.read_text()
    assert_private_file_permissions(settings_path)

    service.save(
        FeishuSettingsUpdate(
            enabled=False,
            app_id="cli_different_app",
            domain="feishu",
        )
    )
    assert secrets.value is None


def test_feishu_credential_test_does_not_save_or_return_token(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "tenant_access_token": "t-secret-token",
                "expire": 7200,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service, secrets = build_feishu_settings(tmp_path, client)
    result = service.test_connection(
        FeishuSettingsUpdate(
            app_id="cli_test_app",
            app_secret="temporary-secret",
            domain="feishu",
        )
    )

    assert result.ok is True
    assert result.domain == "feishu"
    assert "t-secret-token" not in result.model_dump_json()
    assert secrets.value is None
    assert len(requests) == 1
    assert (
        str(requests[0].url)
        == "https://open.feishu.cn/open-apis/auth/v3/"
        "tenant_access_token/internal/"
    )
    assert json.loads(requests[0].content)["app_secret"] == "temporary-secret"


def test_sqlite_channel_store_persists_dedup_and_atomic_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "channel.sqlite3"
    first = SQLiteChannelStore(path)
    first.mark("event-1", 3600)
    assert first.claim_message("message-1", "ou_user", "oc_chat") is True
    assert first.claim_message("message-1", "ou_user", "oc_chat") is False

    second = SQLiteChannelStore(path)
    assert second.seen("event-1") is True
    assert second.claim_message("message-1", "ou_user", "oc_chat") is False
    assert_private_file_permissions(path)


def test_sqlite_style_collection_is_persistent_and_user_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "channel.sqlite3"
    first = SQLiteChannelStore(path)
    assert first.start_style_collection(
        "oc_chat",
        "ou_a",
        description="保留人物构图",
    ) == []
    assert first.append_style_image(
        "oc_chat",
        "ou_a",
        source_image_id="source-content",
        message_id="om_content",
        max_age_seconds=1800,
    ) == [("source-content", "om_content")]
    first.start_style_collection("oc_chat", "ou_b")
    assert first.update_style_description(
        "oc_chat",
        "ou_a",
        description="增加水彩纸纹理",
        append=True,
        max_age_seconds=1800,
    ) == (
        [("source-content", "om_content")],
        "保留人物构图\n增加水彩纸纹理",
    )

    second = SQLiteChannelStore(path)
    assert second.style_collection(
        "oc_chat",
        "ou_a",
        max_age_seconds=1800,
    ) == [("source-content", "om_content")]
    assert second.style_collection_draft(
        "oc_chat",
        "ou_a",
        max_age_seconds=1800,
    ) == (
        [("source-content", "om_content")],
        "保留人物构图\n增加水彩纸纹理",
    )
    assert second.style_collection(
        "oc_chat",
        "ou_b",
        max_age_seconds=1800,
    ) == []
    assert second.clear_style_collection("oc_chat", "ou_a") == [
        ("source-content", "om_content")
    ]
    assert (
        second.style_collection(
            "oc_chat",
            "ou_a",
            max_age_seconds=1800,
        )
        is None
    )


def test_sqlite_style_collection_migrates_existing_drafts(tmp_path: Path) -> None:
    path = tmp_path / "channel.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE channel_style_collections (
                chat_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                images_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (chat_id, sender_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO channel_style_collections
                (chat_id, sender_id, images_json, updated_at)
            VALUES ('oc_chat', 'ou_user', '[]', 9999999999)
            """
        )

    store = SQLiteChannelStore(path)

    assert store.style_collection_draft(
        "oc_chat",
        "ou_user",
        max_age_seconds=1800,
    ) == ([], "")


def test_runtime_authorizes_deduplicates_and_replies(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    repository_settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        domain="feishu",
        allowed_open_ids=["ou_allowed"],
        allow_group_mentions=False,
    )
    service.repository.save(repository_settings)
    runner = FakeRunner()
    channel = FakeChannel()
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
    )
    message = SimpleNamespace(
        id="om_message_1",
        message_id="om_message_1",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="text",
        body_text="现在几点？",
        safe_content_text="现在几点？",
    )

    async def scenario() -> None:
        await runtime.apply_settings(repository_settings)
        await channel.handlers["message"](message)
        await channel.handlers["message"](message)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    assert runtime.public_status().status == "connected"
    assert runner.messages == ["现在几点？"]
    assert [item[1]["text"] for item in channel.sent if "text" in item[1]] == [
        "已收到，正在调用本地 Agent 处理。",
    ]
    assert [
        item[1]["markdown"] for item in channel.sent if "markdown" in item[1]
    ] == ["本地 Agent 已处理：现在几点？"]
    cards = [item[1]["card"] for item in channel.sent if "card" in item[1]]
    assert len(cards) == 1
    assert cards[0]["schema"] == "2.0"
    assert channel.sent[0][2]["reply_to"] == "om_message_1"
    assert len(channel.sent[0][2]["uuid"]) == 48

    events = runtime.store.list_events()
    assert [event.direction for event in events] == [
        "inbound",
        "outbound",
        "outbound",
        "outbound",
    ]
    assert events[-1].kind == "markdown"


def test_runtime_denies_unknown_user_and_returns_bootstrap_open_id(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=[],
    )
    service.repository.save(settings)
    runner = FakeRunner()
    channel = FakeChannel()
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
    )
    message = SimpleNamespace(
        id="om_unknown",
        message_id="om_unknown",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_new_user",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="text",
        body_text="你好",
        safe_content_text="你好",
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](message)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    assert runner.messages == []
    assert len(channel.sent) == 1
    assert "ou_new_user" in channel.sent[0][1]["text"]


def test_runtime_downloads_image_creates_spatial_scene_and_returns_preview(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()
    image = Image.new("RGB", (160, 120), "#7fae98")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    channel.download_bytes = image_buffer.getvalue()
    spatial = build_test_spatial(tmp_path)
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        job_poll_interval=0.01,
        job_timeout_seconds=5,
    )
    message = SimpleNamespace(
        id="om_image",
        message_id="om_image",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="image",
        body_text="",
        safe_content_text="",
        resources=[
            SimpleNamespace(
                type="image",
                file_key="img_test",
                file_name=None,
            )
        ],
        content=SimpleNamespace(image_key="img_test"),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](message)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    text_replies = [
        item[1]["text"] for item in channel.sent if "text" in item[1]
    ]
    image_replies = [item[1]["image"] for item in channel.sent if "image" in item[1]]
    assert text_replies[0].startswith("已收到图片")
    assert any("空间照片任务已创建" in text for text in text_replies)
    assert any("已生成完成" in text for text in text_replies)
    assert len(image_replies) == 1
    assert Path(image_replies[0]["source"]).is_file()
    assert spatial.list_assets()[0].status == "ready"
    image_events = [event for event in store.list_events() if event.kind == "image"]
    assert len(image_events) == 2
    assert all(event.media_url for event in image_events)
    assert all(
        event.media_url.startswith("/api/assets/")
        for event in image_events
        if event.media_url
    )


def test_runtime_image_permission_error_is_actionable(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = ResourceDeniedChannel()
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
        spatial_service=build_test_spatial(tmp_path),
    )
    message = SimpleNamespace(
        id="om_denied_image",
        message_id="om_denied_image",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="image",
        body_text="",
        safe_content_text="",
        resources=[SimpleNamespace(type="image", file_key="img_denied")],
        content=SimpleNamespace(image_key="img_denied"),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](message)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    replies = [item[1].get("text", "") for item in channel.sent]
    assert channel.download_calls == ["om_denied_image", None]
    assert any("im:resource" in text for text in replies)
    assert any("im:message:readonly" in text for text in replies)
    assert any("发布新版本" in text for text in replies)


def test_runtime_tracks_reconnect_state(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(enabled=True, app_id="cli_test")
    channel = FakeChannel()
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        channel.handlers["reconnecting"]()
        assert runtime.public_status().status == "reconnecting"
        channel.handlers["reconnected"]()

    asyncio.run(scenario())
    assert runtime.public_status().status == "connected"


def test_runtime_handles_feature_card_action(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    runner = FakeRunner()
    channel = FakeChannel()
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
    )
    event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_card",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(value={"command": "capabilities"}),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["cardAction"](event)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    assert runner.messages == ["你能做什么？请简洁列出当前可用能力。"]
    assert any("正在执行：能力列表" in item[1].get("text", "") for item in channel.sent)
    assert any("markdown" in item[1] for item in channel.sent)


def test_runtime_feature_menu_includes_photo_style_and_explains_entry(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    runner = FakeRunner()
    channel = FakeChannel()
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        style_service=style,
    )
    event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_style_card",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(value={"command": "photo_style_transfer"}),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await runtime._send_feature_menu("oc_chat", "om_menu")
        await channel.handlers["cardAction"](event)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    try:
        asyncio.run(scenario())

        card_payload = next(
            item[1]["card"] for item in channel.sent if "card" in item[1]
        )
        assert "图片风格化" in json.dumps(card_payload, ensure_ascii=False)
        draft_card = next(
            item[1]["card"]
            for item in channel.sent
            if "card" in item[1]
            and "图片风格化草稿" in json.dumps(item[1]["card"], ensure_ascii=False)
        )
        draft_text = json.dumps(draft_card, ensure_ascii=False)
        assert "支持逐张发送" in draft_text
        assert "待上传" in draft_text
        assert runner.messages == []
        assert "图片风格化" in store.list_events()[-2].content
    finally:
        style.close()
        spatial.close()


def test_runtime_natural_style_request_auto_menu_and_single_image_routing(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    runner = FakeRunner()
    channel = FakeChannel()
    buffer = BytesIO()
    Image.new("RGB", (160, 120), "#b88d72").save(buffer, "PNG")
    channel.download_by_key = {"img_content": buffer.getvalue()}
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        style_service=style,
    )

    def text_message(message_id: str, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            message_id=message_id,
            chat_id="oc_chat",
            chat_type="p2p",
            sender_id="ou_allowed",
            sender_is_bot=False,
            mentioned_bot=False,
            raw_content_type="text",
            body_text=text,
            safe_content_text=text,
            resources=[],
            content=None,
        )

    image_message = SimpleNamespace(
        id="om_style_content",
        message_id="om_style_content",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="image",
        body_text="",
        safe_content_text="",
        resources=[
            SimpleNamespace(
                type="image",
                file_key="img_content",
                file_name="content.png",
            )
        ],
        content=SimpleNamespace(image_key="img_content"),
    )

    async def drain_tasks() -> None:
        await asyncio.sleep(0)
        while runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))
            await asyncio.sleep(0)

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](
            text_message("om_style_request", "我要进行图片风格化任务")
        )
        await drain_tasks()
        await channel.handlers["message"](image_message)
        await drain_tasks()
        await channel.handlers["message"](
            text_message("om_feature_card", "功能卡片")
        )
        await drain_tasks()
        await channel.handlers["message"](text_message("om_hello", "你好"))
        await drain_tasks()

    try:
        asyncio.run(scenario())

        sent_payload = json.dumps([item[1] for item in channel.sent], ensure_ascii=False)
        menu_cards = [
            item[1]["card"]
            for item in channel.sent
            if "card" in item[1]
            and "个人数字助手" in json.dumps(item[1]["card"], ensure_ascii=False)
        ]
        draft = store.style_collection_draft(
            "oc_chat",
            "ou_allowed",
            max_age_seconds=runtime.style_collection_ttl_seconds,
        )
        assert len(menu_cards) == 3
        assert draft is not None and len(draft[0]) == 1
        assert runner.messages == []
        assert spatial.list_assets(owner_id="feishu:cli_test:ou_allowed") == []
        assert "图片风格化草稿" in sent_payload
        assert "不预设固定风格" in sent_payload
        assert "油画" not in sent_payload
        assert "空间照片任务已创建" not in sent_payload
    finally:
        style.close()
        spatial.close()


def test_runtime_collects_style_images_and_returns_preview_and_download(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()

    def png(color: str) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (160, 120), color).save(buffer, "PNG")
        return buffer.getvalue()

    channel.download_by_key = {
        "img_content": png("#b88d72"),
        "img_style": png("#315a84"),
    }
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        style_service=style,
        job_poll_interval=0.01,
        job_timeout_seconds=5,
    )
    card_event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_style_card",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(value={"command": "photo_style_transfer"}),
    )

    def image_message(message_id: str, file_keys: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            message_id=message_id,
            chat_id="oc_chat",
            chat_type="p2p",
            sender_id="ou_allowed",
            sender_is_bot=False,
            mentioned_bot=False,
            raw_content_type="image",
            body_text="",
            safe_content_text="",
            resources=[
                SimpleNamespace(
                    type="image",
                    file_key=file_key,
                    file_name=f"{file_key}.png",
                )
                for file_key in file_keys
            ],
            content=SimpleNamespace(image_key=file_keys[-1]),
        )

    start_event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_style_start",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(value={"command": "photo_style_start"}),
    )

    async def drain_tasks() -> None:
        await asyncio.sleep(0)
        while runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))
            await asyncio.sleep(0)

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        # Mobile Feishu may dispatch the card click and a multi-select image
        # message back-to-back. Do not drain between them: this guards the
        # routing race that previously sent the images to spatial-photo.
        await channel.handlers["cardAction"](card_event)
        await channel.handlers["message"](
            image_message(
                "om_style_mobile_batch",
                ["img_content", "img_style"],
            )
        )
        await drain_tasks()
        await channel.handlers["cardAction"](start_event)
        await drain_tasks()

    try:
        asyncio.run(scenario())

        replies = [item[1].get("text", "") for item in channel.sent]
        assert any(
            "图片风格化草稿" in json.dumps(item[1].get("card", {}), ensure_ascii=False)
            for item in channel.sent
        )
        draft_cards = [
            json.dumps(item[1]["card"], ensure_ascii=False)
            for item in channel.sent
            if "card" in item[1]
            and "图片风格化草稿" in json.dumps(item[1]["card"], ensure_ascii=False)
        ]
        assert any("内容图" in card and "已上传 1 张" in card for card in draft_cards)
        assert any("风格参考图" in card and "已上传 1 / 3 张" in card for card in draft_cards)
        assert any("开始生成" in card for card in draft_cards)
        assert any("图片风格化任务已创建" in text for text in replies)
        assert any("已完成" in text for text in replies)

        image_messages = [
            item[1]["image"] for item in channel.sent if "image" in item[1]
        ]
        file_messages = [
            item[1]["file"] for item in channel.sent if "file" in item[1]
        ]
        assert len(image_messages) == 1
        assert len(file_messages) == 1
        assert Path(image_messages[0]["source"]).is_file()
        assert Path(file_messages[0]["source"]).is_file()
        assert file_messages[0]["file_name"].startswith("style-result-")

        assets = spatial.list_assets(owner_id="feishu:cli_test:ou_allowed")
        assert len(assets) == 1
        assert assets[0].kind == "photo_style_transfer"
        assert assets[0].status == "ready"
        assert assets[0].result_url
        assert list(spatial.source_image_dir.iterdir()) == []
        assert (
            store.style_collection(
                "oc_chat",
                "ou_allowed",
                max_age_seconds=runtime.style_collection_ttl_seconds,
            )
            is None
        )
        assert any(event.kind == "file" for event in store.list_events())
    finally:
        style.close()
        spatial.close()


def test_runtime_style_mode_never_falls_back_to_spatial_when_provider_missing(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()
    spatial = build_test_spatial(tmp_path)
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        style_service=None,
    )
    card_event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_style_card_unavailable",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(value={"command": "photo_style_transfer"}),
    )
    image_message = SimpleNamespace(
        id="om_style_image_unavailable",
        message_id="om_style_image_unavailable",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="image",
        body_text="",
        safe_content_text="",
        resources=[
            SimpleNamespace(
                type="image",
                file_key="img_should_not_be_spatial",
                file_name="style.png",
            )
        ],
        content=SimpleNamespace(image_key="img_should_not_be_spatial"),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["cardAction"](card_event)
        await channel.handlers["message"](image_message)
        await asyncio.sleep(0)
        while runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))
            await asyncio.sleep(0)

    try:
        asyncio.run(scenario())

        replies = [item[1].get("text", "") for item in channel.sent]
        sent_payload = json.dumps([item[1] for item in channel.sent], ensure_ascii=False)
        assert "图片不会转入空间照片" in sent_payload
        assert any("图片风格化能力当前不可用" in text for text in replies)
        assert not any("空间照片任务已创建" in text for text in replies)
        assert spatial.list_assets(owner_id="feishu:cli_test:ou_allowed") == []
        assert store.style_collection(
            "oc_chat",
            "ou_allowed",
            max_age_seconds=runtime.style_collection_ttl_seconds,
        ) == []
    finally:
        spatial.close()


def test_runtime_accepts_rich_post_file_and_incremental_style_description(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()

    def png(color: str) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (160, 120), color).save(buffer, "PNG")
        return buffer.getvalue()

    channel.download_by_key = {
        "img_content": png("#b88d72"),
        "file_reference": png("#315a84"),
    }
    spatial = build_test_spatial(tmp_path)
    style = PhotoStyleService(spatial, provider=LocalColorStyleProvider())
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        style_service=style,
        job_poll_interval=0.01,
        job_timeout_seconds=5,
    )
    rich_post = SimpleNamespace(
        id="om_rich_style",
        message_id="om_rich_style",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="post",
        body_text="保留人物构图，改成柔和水彩",
        safe_content_text="保留人物构图，改成柔和水彩",
        resources=[
            SimpleNamespace(
                type="image",
                file_key="img_content",
                file_name="content.png",
            ),
            SimpleNamespace(
                type="file",
                file_key="file_reference",
                file_name="reference.webp",
            ),
        ],
        content=None,
    )

    def text_message(message_id: str, text: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            message_id=message_id,
            chat_id="oc_chat",
            chat_type="p2p",
            sender_id="ou_allowed",
            sender_is_bot=False,
            mentioned_bot=False,
            raw_content_type="text",
            body_text=text,
            safe_content_text=text,
            resources=[],
            content=None,
        )

    async def drain_tasks() -> None:
        await asyncio.sleep(0)
        while runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))
            await asyncio.sleep(0)

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](rich_post)
        await drain_tasks()
        await channel.handlers["message"](
            text_message("om_style_description", "补充描述：加强纸张纹理")
        )
        await drain_tasks()
        await channel.handlers["message"](
            text_message("om_style_generate", "开始风格化：不要添加文字")
        )
        await drain_tasks()

    try:
        asyncio.run(scenario())

        assets = spatial.list_assets(owner_id="feishu:cli_test:ou_allowed")
        assert len(assets) == 1
        assert assets[0].kind == "photo_style_transfer"
        assert assets[0].parameters["prompt"] == (
            "保留人物构图，改成柔和水彩\n加强纸张纹理\n不要添加文字"
        )
        assert ("img_content", "image", "om_rich_style") in channel.download_requests
        assert (
            "file_reference",
            "file",
            "om_rich_style",
        ) in channel.download_requests
        sent_payload = json.dumps([item[1] for item in channel.sent], ensure_ascii=False)
        assert "图片风格化草稿" in sent_payload
        assert "开始生成" in sent_payload
        assert "图片风格化任务已创建" in sent_payload
        assert "空间照片任务已创建" not in sent_payload
    finally:
        style.close()
        spatial.close()


def test_image_resource_entries_keep_batched_source_message_ids() -> None:
    first = SimpleNamespace(
        id="om_first",
        message_id="om_first",
        resources=[SimpleNamespace(type="image", file_key="img_first")],
        content=None,
    )
    second = SimpleNamespace(
        id="om_second",
        message_id="om_second",
        resources=[SimpleNamespace(type="image", file_key="img_second")],
        content=None,
    )
    merged = SimpleNamespace(
        id="om_second",
        message_id="om_second",
        resources=[*first.resources, *second.resources],
        content=None,
        batched_sources=[first, second],
    )

    assert FeishuChannelRuntime._image_resource_entries(merged) == [
        ("img_first", None, "om_first", "image"),
        ("img_second", None, "om_second", "image"),
    ]


def test_runtime_retry_card_reuses_failed_spatial_job(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()
    spatial = build_test_spatial(tmp_path)
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        job_poll_interval=0.01,
        job_timeout_seconds=5,
    )
    image = Image.new("RGB", (160, 120), "#7fae98")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    owner_id = "feishu:cli_test:ou_allowed"
    created = spatial.create_scene(
        image_buffer.getvalue(),
        original_name="retry.png",
        owner_id=owner_id,
    )
    spatial.wait_for_idle()
    spatial.repository.update_job(
        created.job.id,
        status="failed",
        progress=100,
        stage="interrupted",
        message="任务因本地服务重启而中断，可以重新生成。",
        error="本地服务重启中断任务。",
    )
    spatial.repository.fail_asset(created.asset.id)
    event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_retry",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(
            value={
                "command": "retry_spatial_job",
                "job_id": created.job.id,
            }
        ),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await runtime._send_spatial_retry_card(
            "oc_chat",
            "om_failed",
            created.job.id,
            "本地服务重启中断任务。",
        )
        await channel.handlers["cardAction"](event)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    card_payload = next(item[1]["card"] for item in channel.sent if "card" in item[1])
    card_json = json.dumps(card_payload, ensure_ascii=False)
    assert "重新生成" in card_json
    assert "retry_spatial_job" in card_json
    assert created.job.id in card_json
    assert any(
        "重新进入本地处理队列" in item[1].get("text", "")
        for item in channel.sent
    )
    assert any(
        "已生成完成" in item[1].get("text", "") for item in channel.sent
    )
    assert spatial.get_job(created.job.id, owner_id=owner_id).status == "completed"
    assert any(
        "重新生成空间照片" in event.content
        for event in store.list_events()
        if event.kind == "card"
    )
    spatial.close()


def test_channel_message_history_api_returns_local_events(
    tmp_path: Path,
) -> None:
    llm_settings, _ = build_test_settings(tmp_path)
    feishu_settings, _ = build_feishu_settings(tmp_path)
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    store.record_event(
        chat_id="oc_chat",
        sender_id="ou_user",
        direction="inbound",
        kind="image",
        content="[图片]",
        message_id="om_image",
    )
    assert store.attach_event_media(
        "om_image",
        "/api/assets/asset-1/files/source.webp",
    )
    runtime = FakeRuntime()
    runtime.store = store  # type: ignore[attr-defined]
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=llm_settings,
            spatial_service=build_test_spatial(tmp_path),
            feishu_settings_service=feishu_settings,
            feishu_runtime=runtime,  # type: ignore[arg-type]
        )
    )

    response = client.get("/api/channels/messages?limit=10")

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert message["content"] == "[图片]"
    assert message["media_url"] == "/api/assets/asset-1/files/source.webp"

    deleted = client.delete("/api/channels/conversations/oc_chat")
    assert deleted.status_code == 204
    assert client.get("/api/channels/messages?limit=10").json()["messages"] == []
    assert client.delete("/api/channels/conversations/oc_chat").status_code == 404


def test_runtime_error_status_redacts_app_secret(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret-sensitive")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)

    def fail_factory(**_: Any) -> FakeChannel:
        raise ValueError("cannot connect with app-secret-sensitive")

    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=fail_factory,
    )

    asyncio.run(runtime.apply_settings(settings))

    public = runtime.public_status()
    assert public.status == "error"
    assert public.last_error is not None
    assert "app-secret-sensitive" not in public.last_error
    assert "[已隐藏]" in public.last_error


def test_runtime_surfaces_missing_send_permission(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=[],
    )
    service.repository.save(settings)
    channel = PermissionDeniedChannel()
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await runtime._reply_safely(
            "oc_chat",
            "om_message",
            "测试回复",
            "permission-test",
        )

    asyncio.run(scenario())

    public = runtime.public_status()
    assert public.status == "error"
    assert public.last_error is not None
    assert "发送消息权限" in public.last_error
    assert "im:message:send_as_bot" in public.last_error


def test_feishu_settings_api_applies_runtime_and_never_echoes_secret(
    tmp_path: Path,
) -> None:
    llm_settings, _ = build_test_settings(tmp_path)
    feishu_settings, secrets = build_feishu_settings(tmp_path)
    runtime = FakeRuntime()
    client = TestClient(
        create_app(
            tmp_path / "runs.jsonl",
            settings_service=llm_settings,
            spatial_service=build_test_spatial(tmp_path),
            feishu_settings_service=feishu_settings,
            feishu_runtime=runtime,  # type: ignore[arg-type]
        )
    )

    response = client.put(
        "/api/settings/feishu",
        json={
            "enabled": True,
            "app_id": "cli_api_test",
            "app_secret": "api-secret-value",
            "domain": "feishu",
            "allowed_open_ids": ["ou_user"],
            "allow_group_mentions": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["status"] == "connected"
    assert body["has_app_secret"] is True
    assert "api-secret-value" not in response.text
    assert secrets.value == "api-secret-value"
    assert runtime.applied[-1].allow_group_mentions is True
    assert client.get("/health").json()["feishu_status"] == "connected"


class ImageUploadDeniedChannel(FakeChannel):
    async def send(
        self,
        to: str,
        message: dict[str, Any],
        opts: dict[str, Any] | None = None,
    ) -> Any:
        self.sent.append((to, message, opts or {}))
        if "image" in message:
            return SimpleNamespace(
                success=False,
                error=SimpleNamespace(
                    raw_code=99991672,
                    hint="im:resource:upload required",
                ),
            )
        return SimpleNamespace(success=True)


class SlowDepthEstimator:
    model_name = "Slow Test Depth"

    def estimate(self, image: Image.Image, progress: Any) -> Image.Image:
        progress(24, "loading_model", "测试慢模型加载")
        time.sleep(0.08)
        return Image.linear_gradient("L").resize(image.size)


def test_runtime_provisions_workspace_and_rejects_suspended_binding(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    runner = FakeRunner()
    channel = FakeChannel()
    identities = IdentityBindingRegistry(tmp_path / "identity.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        runner,  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
        identity_registry=identities,
    )
    message = SimpleNamespace(
        id="om_suspended",
        message_id="om_suspended",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="text",
        body_text="查看资产",
        safe_content_text="查看资产",
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        binding = identities.list_bindings()[0]
        assert binding.owner_key == "feishu:cli_test:ou_allowed"
        identities.set_binding_status(binding.id, "suspended")
        await channel.handlers["message"](message)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    assert runner.messages == []
    assert any(
        "个人工作区已停用" in item[1].get("text", "")
        for item in channel.sent
    )


def test_runtime_refreshes_spatial_viewer_link_from_card(tmp_path: Path) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()
    spatial = build_test_spatial(tmp_path)
    image = Image.new("RGB", (160, 120), "#7fae98")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    created = spatial.create_scene(
        image_buffer.getvalue(),
        original_name="refresh.png",
        owner_id="feishu:cli_test:ou_allowed",
    )
    spatial.wait_for_idle()
    link_calls: list[str] = []

    def viewer_link(asset_id: str) -> str:
        link_calls.append(asset_id)
        return f"https://fresh-viewer.example.test/v/{asset_id}"

    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        viewer_link_factory=viewer_link,
    )
    event = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_refresh_viewer",
        operator=SimpleNamespace(open_id="ou_allowed"),
        action=SimpleNamespace(
            value={
                "command": "refresh_spatial_viewer",
                "asset_id": created.asset.id,
            }
        ),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["cardAction"](event)
        await asyncio.sleep(0)
        if runtime._tasks:
            await asyncio.gather(*list(runtime._tasks))

    asyncio.run(scenario())

    assert link_calls == [created.asset.id]
    refreshed_card = next(
        item[1]["card"]
        for item in channel.sent
        if "card" in item[1]
        and "空间照片预览链接已刷新"
        in json.dumps(item[1]["card"], ensure_ascii=False)
    )
    refreshed_json = json.dumps(refreshed_card, ensure_ascii=False)
    assert "打开最新预览" in refreshed_json
    assert "https://fresh-viewer.example.test/v/" in refreshed_json
    assert "refresh_spatial_viewer" in refreshed_json
    spatial.close()


def test_runtime_keeps_delivering_after_initial_spatial_timeout(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = FakeChannel()
    image = Image.new("RGB", (160, 120), "#7fae98")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    channel.download_bytes = image_buffer.getvalue()
    spatial = SpatialSceneService(
        tmp_path / "slow-personal-assets",
        estimator=SlowDepthEstimator(),
        segmenter=FakeForegroundSegmenter(),
    )
    store = SQLiteChannelStore(tmp_path / "channel.sqlite3")
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        store,
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        viewer_link_factory=lambda asset_id: f"https://viewer.test/{asset_id}",
        job_poll_interval=0.005,
        job_timeout_seconds=0.02,
        background_job_timeout_seconds=1,
    )
    message = SimpleNamespace(
        id="om_slow_image",
        message_id="om_slow_image",
        chat_id="oc_chat",
        chat_type="p2p",
        sender_id="ou_allowed",
        sender_is_bot=False,
        mentioned_bot=False,
        raw_content_type="image",
        body_text="",
        safe_content_text="",
        resources=[SimpleNamespace(type="image", file_key="img_slow")],
        content=SimpleNamespace(image_key="img_slow"),
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await channel.handlers["message"](message)
        await asyncio.sleep(0)

        async def drain_runtime_tasks() -> None:
            while runtime._tasks:
                await asyncio.gather(*list(runtime._tasks))
                await asyncio.sleep(0)

        await asyncio.wait_for(drain_runtime_tasks(), timeout=2)

    asyncio.run(scenario())

    replies = [item[1] for item in channel.sent]
    assert any(
        "任务仍在本机后台运行" in item.get("text", "") for item in replies
    )
    assert any("image" in item for item in replies)
    assert any(
        "2.5D 空间照片已生成" in json.dumps(item.get("card", {}), ensure_ascii=False)
        for item in replies
    )
    spatial.close()


def test_runtime_returns_card_when_result_image_upload_scope_is_missing(
    tmp_path: Path,
) -> None:
    service, secrets = build_feishu_settings(tmp_path)
    secrets.set("app-secret")
    settings = StoredFeishuSettings(
        enabled=True,
        app_id="cli_test",
        allowed_open_ids=["ou_allowed"],
    )
    service.repository.save(settings)
    channel = ImageUploadDeniedChannel()
    spatial = build_test_spatial(tmp_path)
    image = Image.new("RGB", (160, 120), "#7fae98")
    image_buffer = BytesIO()
    image.save(image_buffer, "PNG")
    owner_id = "feishu:cli_test:ou_allowed"
    created = spatial.create_scene(
        image_buffer.getvalue(),
        original_name="result.png",
        owner_id=owner_id,
    )
    spatial.wait_for_idle()
    runtime = FeishuChannelRuntime(
        service,
        FakeRunner(),  # type: ignore[arg-type]
        SQLiteChannelStore(tmp_path / "channel.sqlite3"),
        channel_factory=lambda **_: channel,
        spatial_service=spatial,
        viewer_link_factory=lambda asset_id: f"https://viewer.test/{asset_id}",
        job_poll_interval=0.01,
        job_timeout_seconds=5,
    )

    async def scenario() -> None:
        await runtime.apply_settings(settings)
        await runtime._deliver_spatial_job_result(
            chat_id="oc_chat",
            message_id="om_result",
            job_id=created.job.id,
            tracked_message_id=None,
            owner_id=owner_id,
        )

    asyncio.run(scenario())

    replies = [item[1] for item in channel.sent]
    assert any(
        "im:resource:upload" in json.dumps(item, ensure_ascii=False)
        and "开通图片上传权限" in json.dumps(item, ensure_ascii=False)
        for item in replies
    )
    assert any(
        "2.5D 空间照片已生成" in json.dumps(item.get("card", {}), ensure_ascii=False)
        for item in replies
    )
    assert runtime.public_status().status == "connected"
    spatial.close()
