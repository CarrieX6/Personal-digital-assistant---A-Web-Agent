from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx
import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError
from pydantic import BaseModel

from .agent import DemoPlanner, Planner
from .llm import LLMError, OpenAICompatiblePlanner, build_planner_from_env
from .models import (
    ConnectionTestResponse,
    LLMRuntimePublic,
    LLMSettingsPublic,
    LLMSettingsUpdate,
    ProviderCatalogResponse,
    ProviderPreset,
)
from .tools import ToolRegistry


PROVIDERS = [
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        description="高性价比文本模型，支持 Tool Calling",
        base_url="https://api.deepseek.com",
        default_model="deepseek-v4-flash",
        models=["deepseek-v4-flash", "deepseek-v4-pro"],
        docs_url="https://api-docs.deepseek.com/guides/reasoning_model_api_example_streaming",
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        description="GPT-5.6，支持视觉与 Tool Calling",
        base_url="https://api.openai.com/v1",
        default_model="gpt-5.6",
        models=["gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"],
        docs_url="https://developers.openai.com/api/docs/guides/latest-model",
    ),
    ProviderPreset(
        id="qwen",
        name="Qwen · 阿里云百炼",
        description="国内服务稳定，Plus 适合通用 Agent",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3.7-plus",
        models=["qwen3.7-plus", "qwen3.7-max", "qwen3.6-flash"],
        docs_url="https://help.aliyun.com/zh/model-studio/text-generation-model",
    ),
    ProviderPreset(
        id="glm",
        name="GLM · 智谱",
        description="中文与 Agent 能力较强，兼容 OpenAI 接口",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5.2",
        models=["glm-5.2", "glm-4.7"],
        docs_url="https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
    ),
    ProviderPreset(
        id="custom",
        name="自定义",
        description="任意兼容 Chat Completions Tool Calling 的服务",
        base_url="https://",
        default_model="",
        models=[],
        docs_url=None,
    ),
]

PROVIDER_BY_ID = {provider.id: provider for provider in PROVIDERS}


class StoredLLMSettings(BaseModel):
    enabled: bool
    provider_id: str
    base_url: str
    model: str
    timeout_seconds: float


class SettingsError(ValueError):
    """Raised when model settings are incomplete or cannot be persisted."""


class SettingsRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> StoredLLMSettings | None:
        if not self.path.exists():
            return None
        try:
            return StoredLLMSettings.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise SettingsError("本地模型配置损坏，请重新保存。") from exc

    def save(self, settings: StoredLLMSettings) -> None:
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


class SecretStore(Protocol):
    def get(self) -> str | None: ...

    def set(self, value: str) -> None: ...

    def delete(self) -> None: ...


class KeyringSecretStore:
    def __init__(self, settings_path: Path, account: str = "llm-api-key") -> None:
        workspace = str(settings_path.resolve().parents[2])
        workspace_id = hashlib.sha256(workspace.encode()).hexdigest()[:12]
        self.service = f"agent-lab-{workspace_id}"
        self.account = account

    def get(self) -> str | None:
        return keyring.get_password(self.service, self.account)

    def set(self, value: str) -> None:
        keyring.set_password(self.service, self.account, value)

    def delete(self) -> None:
        try:
            keyring.delete_password(self.service, self.account)
        except KeyringError:
            pass


class EncryptedFileSecretStore:
    """Fallback for environments without an available operating-system keyring."""

    def __init__(self, secret_path: Path, master_key_path: Path) -> None:
        self.secret_path = secret_path
        self.master_key_path = master_key_path
        self._lock = threading.Lock()

    def get(self) -> str | None:
        if not self.secret_path.exists():
            return None
        try:
            token = self.secret_path.read_bytes()
            return self._fernet().decrypt(token).decode("utf-8")
        except (OSError, InvalidToken, UnicodeDecodeError) as exc:
            raise SettingsError("本地密钥文件无法解密，请清除后重新配置。") from exc

    def set(self, value: str) -> None:
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        token = self._fernet().encrypt(value.encode("utf-8"))
        temporary_path = self.secret_path.with_suffix(".tmp")
        with self._lock:
            temporary_path.write_bytes(token)
            os.chmod(temporary_path, 0o600)
            temporary_path.replace(self.secret_path)

    def delete(self) -> None:
        self.secret_path.unlink(missing_ok=True)

    def _fernet(self) -> Fernet:
        if not self.master_key_path.exists():
            self.master_key_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.master_key_path.with_suffix(".tmp")
            temporary_path.write_bytes(Fernet.generate_key())
            os.chmod(temporary_path, 0o600)
            try:
                temporary_path.replace(self.master_key_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return Fernet(self.master_key_path.read_bytes())


class HybridSecretStore:
    def __init__(
        self,
        primary: SecretStore,
        fallback: SecretStore,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def get(self) -> str | None:
        try:
            value = self.primary.get()
        except KeyringError:
            value = None
        return value or self.fallback.get()

    def set(self, value: str) -> None:
        try:
            self.primary.set(value)
            self.fallback.delete()
        except KeyringError:
            self.fallback.set(value)

    def delete(self) -> None:
        try:
            self.primary.delete()
        finally:
            self.fallback.delete()


class SettingsService:
    secret_storage_label = "系统钥匙串（不可用时回退到本机加密文件）"

    def __init__(
        self,
        repository: SettingsRepository,
        secret_store: SecretStore,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.repository = repository
        self.secret_store = secret_store
        self.http_client = http_client

    def catalog(
        self,
        runtime: LLMRuntimePublic | None = None,
    ) -> ProviderCatalogResponse:
        return ProviderCatalogResponse(
            providers=PROVIDERS,
            settings=self.public_settings(),
            runtime=runtime,
        )

    def public_settings(self) -> LLMSettingsPublic:
        stored = self.repository.load()
        if stored is None:
            default = PROVIDER_BY_ID["deepseek"]
            stored = StoredLLMSettings(
                enabled=False,
                provider_id=default.id,
                base_url=default.base_url,
                model=default.default_model,
                timeout_seconds=30,
            )
        api_key = self.secret_store.get()
        return LLMSettingsPublic(
            **stored.model_dump(),
            has_api_key=bool(api_key),
            masked_api_key=self._mask(api_key),
            secret_storage=self.secret_storage_label,
        )

    def save(self, update: LLMSettingsUpdate) -> LLMSettingsPublic:
        self._validate_provider(update.provider_id)
        provided_key = (
            update.api_key.get_secret_value().strip()
            if update.api_key is not None
            else ""
        )
        stored = self.repository.load()
        existing_key = (
            self.secret_store.get()
            if stored is not None and stored.provider_id == update.provider_id
            else None
        )
        effective_key = provided_key or existing_key
        if update.enabled and not effective_key:
            raise SettingsError("启用真实模型前必须填写该供应商的 API Key。")

        if provided_key:
            self.secret_store.set(provided_key)

        self.repository.save(
            StoredLLMSettings(
                enabled=update.enabled,
                provider_id=update.provider_id,
                base_url=self._normalize_base_url(update.base_url),
                model=update.model.strip(),
                timeout_seconds=update.timeout_seconds,
            )
        )
        return self.public_settings()

    def clear_key(self) -> LLMSettingsPublic:
        self.secret_store.delete()
        stored = self.repository.load()
        if stored:
            stored.enabled = False
            self.repository.save(stored)
        return self.public_settings()

    def build_planner(self) -> Planner:
        stored = self.repository.load()
        if stored is None:
            return build_planner_from_env(client=self.http_client)
        if not stored.enabled:
            return DemoPlanner()
        api_key = self.secret_store.get()
        if not api_key:
            return DemoPlanner()
        return self._planner(stored, api_key)

    def test_connection(
        self,
        update: LLMSettingsUpdate,
        registry: ToolRegistry,
    ) -> ConnectionTestResponse:
        result, planner = self.validate_connection(update, registry)
        planner.close()
        return result

    def validate_connection(
        self,
        update: LLMSettingsUpdate,
        registry: ToolRegistry,
    ) -> tuple[ConnectionTestResponse, OpenAICompatiblePlanner]:
        self._validate_provider(update.provider_id)
        provided_key = (
            update.api_key.get_secret_value().strip()
            if update.api_key is not None
            else ""
        )
        saved = self.repository.load()
        existing_key = (
            self.secret_store.get()
            if saved is not None and saved.provider_id == update.provider_id
            else None
        )
        api_key = provided_key or existing_key
        if not api_key:
            raise SettingsError("测试连接前请填写该供应商的 API Key。")

        model = update.model.strip()
        if not model:
            raise SettingsError("测试连接前请填写模型 ID。")

        stored = StoredLLMSettings(
            enabled=True,
            provider_id=update.provider_id,
            base_url=self._normalize_base_url(update.base_url),
            model=model,
            timeout_seconds=update.timeout_seconds,
        )
        planner = self._planner(stored, api_key)
        try:
            result = self._validate_planner(planner, stored, registry)
        except (LLMError, SettingsError):
            planner.close()
            raise
        return result, planner

    @staticmethod
    def _validate_planner(
        planner: OpenAICompatiblePlanner,
        stored: StoredLLMSettings,
        registry: ToolRegistry,
    ) -> ConnectionTestResponse:
        started = time.perf_counter()
        qa_plan = planner.plan(
            "请用一句简短中文回复：模型连接正常。不要调用任何工具。",
            [],
        )
        answer = (qa_plan.direct_answer or "").strip()
        if not answer:
            raise LLMError("连接成功，但模型没有返回普通问答文本。")

        current_time_schema = [
            schema
            for schema in registry.openai_schemas()
            if schema.get("function", {}).get("name") == "current_time"
        ]
        plan = planner.plan(
            "请调用 current_time 工具获取当前时间。必须调用工具，不要直接回答。",
            current_time_schema,
        )
        selected_tools = [call.name for call in plan.tool_calls]
        tool_calling_ok = "current_time" in selected_tools
        return ConnectionTestResponse(
            ok=True,
            model=stored.model,
            selected_tools=selected_tools,
            tool_calling_ok=tool_calling_ok,
            answer_preview=answer[:120],
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            message=(
                "普通问答与 Tool Calling 均验证成功。"
                if tool_calling_ok
                else "普通问答可用，但 Tool Calling 未通过验证。"
            ),
        )

    def _planner(
        self,
        stored: StoredLLMSettings,
        api_key: str,
    ) -> OpenAICompatiblePlanner:
        return OpenAICompatiblePlanner(
            api_key=api_key,
            base_url=stored.base_url,
            model=stored.model,
            timeout_seconds=stored.timeout_seconds,
            client=self.http_client,
        )

    @staticmethod
    def _mask(api_key: str | None) -> str | None:
        if not api_key:
            return None
        suffix = api_key[-4:] if len(api_key) >= 4 else ""
        return f"••••••••{suffix}"

    @staticmethod
    def _validate_provider(provider_id: str) -> None:
        if provider_id not in PROVIDER_BY_ID:
            raise SettingsError("未知的模型供应商。")

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise SettingsError("Base URL 必须以 http:// 或 https:// 开头。")
        return normalized


def create_default_settings_service(settings_path: Path) -> SettingsService:
    data_dir = settings_path.parent
    secret_store = HybridSecretStore(
        KeyringSecretStore(settings_path),
        EncryptedFileSecretStore(
            data_dir / "llm_api_key.enc",
            data_dir / ".secret_master_key",
        ),
    )
    return SettingsService(SettingsRepository(settings_path), secret_store)


class LLMRuntimeManager:
    """Owns the active planner and makes configuration activation observable."""

    def __init__(
        self,
        service: SettingsService,
        registry: ToolRegistry,
        initial_planner: Planner | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self._lock = threading.RLock()
        self._planner = initial_planner or service.build_planner()
        self._retired_planners: list[Planner] = []
        public = service.public_settings()
        stored = service.repository.load()
        if self._planner.is_llm:
            self._status = "ready"
            self._qa_available = True
            self._tool_calling_available = True
        elif stored is None and not public.has_api_key:
            self._status = "unconfigured"
            self._qa_available = False
            self._tool_calling_available = False
        elif public.has_api_key and not public.enabled:
            self._status = "configured_not_enabled"
            self._qa_available = False
            self._tool_calling_available = False
        else:
            self._status = "disabled"
            self._qa_available = False
            self._tool_calling_available = False
        self._last_tested_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def planner(self) -> Planner:
        with self._lock:
            return self._planner

    def public_status(self) -> LLMRuntimePublic:
        settings = self.service.public_settings()
        with self._lock:
            return LLMRuntimePublic(
                status=self._status,
                active=self._planner.is_llm,
                provider_id=settings.provider_id,
                model=self._planner.model_name or settings.model,
                has_api_key=settings.has_api_key,
                qa_available=self._qa_available,
                tool_calling_available=self._tool_calling_available,
                last_tested_at=self._last_tested_at,
                last_error=self._last_error,
            )

    def test(self, update: LLMSettingsUpdate) -> ConnectionTestResponse:
        with self._lock:
            previous_status = self._status
            self._status = "testing"
        try:
            result = self.service.test_connection(update, self.registry)
        except (LLMError, SettingsError) as exc:
            with self._lock:
                self._status = previous_status if self._planner.is_llm else "error"
                self._last_tested_at = datetime.now(timezone.utc)
                self._last_error = str(exc)
            raise
        with self._lock:
            self._status = previous_status
            self._last_tested_at = datetime.now(timezone.utc)
            self._last_error = None
        return result

    def apply(
        self,
        update: LLMSettingsUpdate,
    ) -> tuple[LLMSettingsPublic, ConnectionTestResponse | None, Planner]:
        if not update.enabled:
            settings = self.service.save(update)
            planner = self.service.build_planner()
            with self._lock:
                if self._planner is not planner:
                    self._retired_planners.append(self._planner)
                self._planner = planner
                self._status = "disabled"
                self._qa_available = False
                self._tool_calling_available = False
                self._last_error = None
            return settings, None, planner

        with self._lock:
            previous_planner = self._planner
            previous_status = self._status
            self._status = "testing"
        candidate: OpenAICompatiblePlanner | None = None
        try:
            result, candidate = self.service.validate_connection(
                update,
                self.registry,
            )
            settings = self.service.save(update)
        except (LLMError, SettingsError) as exc:
            if candidate is not None:
                candidate.close()
            with self._lock:
                self._planner = previous_planner
                self._status = previous_status if previous_planner.is_llm else "error"
                self._last_tested_at = datetime.now(timezone.utc)
                self._last_error = str(exc)
            raise

        with self._lock:
            if self._planner is not candidate:
                self._retired_planners.append(self._planner)
            self._planner = candidate
            self._status = (
                "ready" if result.tool_calling_ok else "degraded"
            )
            self._qa_available = True
            self._tool_calling_available = result.tool_calling_ok
            self._last_tested_at = datetime.now(timezone.utc)
            self._last_error = None
        return settings, result, candidate

    def clear_key(self) -> tuple[LLMSettingsPublic, Planner]:
        settings = self.service.clear_key()
        planner = self.service.build_planner()
        with self._lock:
            if self._planner is not planner:
                self._retired_planners.append(self._planner)
            self._planner = planner
            self._status = "unconfigured"
            self._qa_available = False
            self._tool_calling_available = False
            self._last_error = None
        return settings, planner

    def close(self) -> None:
        with self._lock:
            planners = [self._planner, *self._retired_planners]
            self._retired_planners = []
        for planner in planners:
            close = getattr(planner, "close", None)
            if callable(close):
                close()
