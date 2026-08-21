from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from .models import (
    FeishuConnectionTestResponse,
    FeishuRuntimePublic,
    FeishuSettingsPublic,
    FeishuSettingsUpdate,
)
from .settings import (
    EncryptedFileSecretStore,
    HybridSecretStore,
    KeyringSecretStore,
    SecretStore,
)


class StoredFeishuSettings(BaseModel):
    enabled: bool = False
    app_id: str = ""
    domain: Literal["feishu", "lark"] = "feishu"
    allowed_open_ids: list[str] = Field(default_factory=list)
    allow_group_mentions: bool = False


class FeishuSettingsError(ValueError):
    """Raised when Feishu settings are incomplete or cannot be persisted."""


class FeishuSettingsRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> StoredFeishuSettings | None:
        if not self.path.exists():
            return None
        try:
            return StoredFeishuSettings.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise FeishuSettingsError("本地飞书配置损坏，请重新保存。") from exc

    def save(self, settings: StoredFeishuSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        payload = json.dumps(
            settings.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        with self._lock:
            temporary_path.write_text(payload + "\n", encoding="utf-8")
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(self.path)


class FeishuSettingsService:
    secret_storage_label = "系统钥匙串（不可用时回退到本机加密文件）"
    domain_urls = {
        "feishu": "https://open.feishu.cn",
        "lark": "https://open.larksuite.com",
    }

    def __init__(
        self,
        repository: FeishuSettingsRepository,
        secret_store: SecretStore,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.repository = repository
        self.secret_store = secret_store
        self.http_client = http_client

    def load(self) -> StoredFeishuSettings:
        return self.repository.load() or StoredFeishuSettings()

    def app_secret(self) -> str | None:
        return self.secret_store.get()

    def public_settings(
        self,
        runtime: FeishuRuntimePublic | None = None,
    ) -> FeishuSettingsPublic:
        stored = self.load()
        app_secret = self.secret_store.get()
        return FeishuSettingsPublic(
            **stored.model_dump(),
            has_app_secret=bool(app_secret),
            masked_app_secret=self._mask(app_secret),
            secret_storage=self.secret_storage_label,
            runtime=runtime
            or FeishuRuntimePublic(status="disabled", last_error=None),
        )

    def save(self, update: FeishuSettingsUpdate) -> StoredFeishuSettings:
        app_id = update.app_id.strip()
        provided_secret = (
            update.app_secret.get_secret_value().strip()
            if update.app_secret is not None
            else ""
        )
        previous = self.load()
        same_app = bool(app_id) and previous.app_id == app_id
        existing_secret = self.secret_store.get() if same_app else None
        effective_secret = provided_secret or existing_secret

        if update.enabled and not app_id:
            raise FeishuSettingsError("启用飞书接入前必须填写 App ID。")
        if update.enabled and not effective_secret:
            raise FeishuSettingsError("启用飞书接入前必须填写对应 App Secret。")

        if app_id != previous.app_id and not provided_secret:
            self.secret_store.delete()
        elif provided_secret:
            self.secret_store.set(provided_secret)

        stored = StoredFeishuSettings(
            enabled=update.enabled,
            app_id=app_id,
            domain=update.domain,
            allowed_open_ids=self._normalize_open_ids(update.allowed_open_ids),
            allow_group_mentions=update.allow_group_mentions,
        )
        self.repository.save(stored)
        return stored

    def clear_secret(self) -> StoredFeishuSettings:
        self.secret_store.delete()
        stored = self.load()
        stored.enabled = False
        self.repository.save(stored)
        return stored

    def test_connection(
        self,
        update: FeishuSettingsUpdate,
    ) -> FeishuConnectionTestResponse:
        app_id = update.app_id.strip()
        provided_secret = (
            update.app_secret.get_secret_value().strip()
            if update.app_secret is not None
            else ""
        )
        saved = self.load()
        existing_secret = (
            self.secret_store.get()
            if saved.app_id == app_id and app_id
            else None
        )
        app_secret = provided_secret or existing_secret
        if not app_id or not app_secret:
            raise FeishuSettingsError("测试连接前请填写匹配的 App ID 和 App Secret。")

        url = (
            self.domain_urls[update.domain]
            + "/open-apis/auth/v3/tenant_access_token/internal/"
        )
        started = time.perf_counter()
        try:
            if self.http_client is not None:
                response = self.http_client.post(
                    url,
                    json={"app_id": app_id, "app_secret": app_secret},
                    timeout=15,
                )
            else:
                with httpx.Client(timeout=15) as client:
                    response = client.post(
                        url,
                        json={"app_id": app_id, "app_secret": app_secret},
                    )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FeishuSettingsError(
                "无法连接飞书开放平台，请检查网络、地区域名或代理设置。"
            ) from exc

        if response.status_code >= 400 or payload.get("code") != 0:
            platform_message = str(payload.get("msg") or "凭证校验失败")
            raise FeishuSettingsError(
                f"飞书开放平台拒绝了凭证：{platform_message}。"
            )

        return FeishuConnectionTestResponse(
            ok=True,
            app_id=app_id,
            domain=update.domain,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            message="App ID 与 App Secret 校验成功；保存并启用后才会建立长连接。",
        )

    @staticmethod
    def _normalize_open_ids(values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            open_id = value.strip()
            if not open_id:
                continue
            if len(open_id) > 100:
                raise FeishuSettingsError("Open ID 长度不能超过 100 个字符。")
            if open_id not in normalized:
                normalized.append(open_id)
        return normalized

    @staticmethod
    def _mask(value: str | None) -> str | None:
        if not value:
            return None
        if len(value) <= 8:
            return "••••"
        return f"{value[:3]}••••{value[-4:]}"


def create_default_feishu_settings_service(
    settings_path: Path,
) -> FeishuSettingsService:
    data_dir = settings_path.parent
    secret_store = HybridSecretStore(
        KeyringSecretStore(settings_path, account="feishu-app-secret"),
        EncryptedFileSecretStore(
            data_dir / "feishu_app_secret.enc",
            data_dir / ".secret_master_key",
        ),
    )
    return FeishuSettingsService(
        FeishuSettingsRepository(settings_path),
        secret_store,
    )
