from __future__ import annotations

import mimetypes
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .agent import (
    AgentRunError,
    AgentRunIsolationError,
    AgentRunner,
    JsonlTraceStore,
    Planner,
)
from .assets import (
    MAX_UPLOAD_BYTES,
    AssetError,
    SpatialSceneService,
    register_asset_tools,
)
from .capability_setup import (
    CapabilitySetupService,
    create_capability_setup_router,
)
from .llm import LLMError
from .channel_settings import (
    FeishuSettingsError,
    FeishuSettingsService,
    create_default_feishu_settings_service,
)
from .feishu import FeishuChannelRuntime, SQLiteChannelStore
from .flux_gs import FluxGSService, create_flux_gs_router, register_flux_gs_tools
from .identity import IdentityBinding, IdentityBindingError, IdentityBindingRegistry
from .lan_viewer import LanViewerService, ViewerLinkError
from .models import (
    AgentRunRequest,
    AgentRunDecisionRequest,
    AgentRunListResponse,
    AgentRunResponse,
    AssetListResponse,
    AssetPublic,
    ChannelMessageListResponse,
    CapabilityInfo,
    ConnectionTestResponse,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMessageListResponse,
    ConversationMessagePublic,
    ConversationPublic,
    ConversationRenameRequest,
    FeishuConnectionTestResponse,
    FeishuSettingsPublic,
    FeishuSettingsUpdate,
    HealthResponse,
    IdentityBindingCreateRequest,
    IdentityBindingListResponse,
    IdentityBindingPublic,
    IdentityBindingStatusUpdate,
    IdentityWorkspaceRenameRequest,
    JobListResponse,
    JobPublic,
    LLMSettingsPublic,
    LLMSettingsUpdate,
    LLMRuntimePublic,
    MemoryCreateRequest,
    MemoryEvidenceListResponse,
    MemoryEvidencePublic,
    MemoryExportResponse,
    MemoryListResponse,
    MemoryPublic,
    MemoryUpdateRequest,
    PhotoStyleCreateResponse,
    PhotoStyleProviderStatus,
    ProviderCatalogResponse,
    SourceImagePublic,
    SpatialSceneCreateResponse,
    ToolInfo,
    ViewerLinkPublic,
)
from .memory import (
    MemoryPolicyError,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SESSION_SUMMARY_SCHEMA_VERSION,
)
from .settings import (
    LLMRuntimeManager,
    SettingsError,
    SettingsService,
    create_default_settings_service,
)
from .style_transfer import PhotoStyleService, StyleParameters, register_style_tools
from .tokenization import ContextWindowExceededError
from .tools import ToolError, ToolRegistry, build_default_registry


load_dotenv()

DEFAULT_TRACE_PATH = Path(__file__).resolve().parents[1] / "data" / "runs.jsonl"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "settings.json"
DEFAULT_FEISHU_SETTINGS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "feishu_settings.json"
)
DEFAULT_CHANNEL_STORE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "channel.sqlite3"
)
DEFAULT_IDENTITY_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "identity.sqlite3"
)
DEFAULT_ASSET_DATA_PATH = Path(__file__).resolve().parents[1] / "data"
WEB_OWNER_ID = "local"
WEB_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _web_thread_id(session_id: str) -> str:
    safe_session_id = session_id.strip()
    if not WEB_SESSION_PATTERN.fullmatch(safe_session_id):
        raise HTTPException(status_code=422, detail="会话 ID 格式无效。")
    return f"web:{safe_session_id}"


def _request_idempotency_key(request: Request) -> str | None:
    raw_key = request.headers.get("Idempotency-Key")
    if raw_key is None:
        return None
    key = raw_key.strip()
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise HTTPException(status_code=422, detail="幂等键格式无效。")
    return key


def _public_session_id(thread_id: str) -> str:
    return thread_id.removeprefix("web:")


def _conversation_public(summary: object) -> ConversationPublic:
    return ConversationPublic(
        id=_public_session_id(summary.thread_id),
        channel=summary.channel,
        project_id=summary.project_id,
        title=summary.title,
        created_at=datetime.fromtimestamp(summary.created_at, tz=timezone.utc),
        updated_at=datetime.fromtimestamp(summary.updated_at, tz=timezone.utc),
        message_count=summary.message_count,
    )


def _identity_binding_public(binding: IdentityBinding) -> IdentityBindingPublic:
    return IdentityBindingPublic(
        id=binding.id,
        provider="feishu",
        app_id=binding.app_id,
        external_id=binding.external_id,
        workspace_id=binding.workspace_id,
        workspace_name=binding.workspace_name,
        device_id=binding.device_id,
        device_name=binding.device_name,
        status=binding.status,
        created_at=datetime.fromtimestamp(binding.created_at, tz=timezone.utc),
        updated_at=datetime.fromtimestamp(binding.updated_at, tz=timezone.utc),
    )


def _require_local_root(request: Request) -> None:
    client_host = request.client.host if request.client is not None else ""
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(
            status_code=403,
            detail="账号绑定管理仅允许从本机 Root 控制台访问。",
        )


def _memory_public(memory: MemoryRecord) -> MemoryPublic:
    def timestamp(value: float | None) -> datetime | None:
        return (
            datetime.fromtimestamp(value, tz=timezone.utc)
            if value is not None
            else None
        )

    return MemoryPublic(
        id=memory.id,
        memory_type=memory.memory_type,
        scope=memory.scope,
        scope_id=memory.scope_id,
        content=memory.content,
        topic_key=memory.topic_key,
        source=memory.source,
        source_message_id=memory.source_message_id,
        source_run_id=memory.source_run_id,
        confidence=memory.confidence,
        importance=memory.importance,
        sensitivity=memory.sensitivity,
        retrieval_policy=memory.retrieval_policy,
        valid_from=datetime.fromtimestamp(memory.valid_from, tz=timezone.utc),
        valid_to=timestamp(memory.valid_to),
        status=memory.status,
        supersedes_id=memory.supersedes_id,
        created_at=datetime.fromtimestamp(memory.created_at, tz=timezone.utc),
        updated_at=datetime.fromtimestamp(memory.updated_at, tz=timezone.utc),
        last_accessed_at=timestamp(memory.last_accessed_at),
        access_count=memory.access_count,
        utility_score=memory.utility_score,
        metadata=memory.metadata,
        relevance_score=memory.relevance_score,
        evidence_count=memory.evidence_count,
        evidence_refs=list(memory.evidence_refs),
    )


def create_app(
    trace_path: Path = DEFAULT_TRACE_PATH,
    planner: Planner | None = None,
    settings_service: SettingsService | None = None,
    spatial_service: SpatialSceneService | None = None,
    style_service: PhotoStyleService | None = None,
    flux_gs_service: FluxGSService | None = None,
    feishu_settings_service: FeishuSettingsService | None = None,
    feishu_runtime: FeishuChannelRuntime | None = None,
    viewer_service: LanViewerService | None = None,
    identity_registry: IdentityBindingRegistry | None = None,
    capability_setup_service: CapabilitySetupService | None = None,
) -> FastAPI:
    registry = build_default_registry()
    selected_spatial_service = spatial_service or SpatialSceneService(
        DEFAULT_ASSET_DATA_PATH
    )
    register_asset_tools(registry, selected_spatial_service)
    selected_style_service = style_service or PhotoStyleService(
        selected_spatial_service
    )
    register_style_tools(registry, selected_style_service)
    selected_flux_gs_service = flux_gs_service or FluxGSService()
    register_flux_gs_tools(registry, selected_flux_gs_service)
    selected_settings_service = settings_service or create_default_settings_service(
        DEFAULT_SETTINGS_PATH
    )
    llm_runtime = LLMRuntimeManager(
        selected_settings_service,
        registry,
        initial_planner=planner,
    )
    selected_planner = llm_runtime.planner
    runner = AgentRunner(
        registry,
        selected_planner,
        JsonlTraceStore(trace_path),
        image_loader=lambda source_image_id, owner_id: (
            selected_spatial_service.read_source_image(
                source_image_id,
                owner_id=owner_id,
            )
        ),
    )
    channel_data_path = trace_path.parent
    selected_feishu_settings_service = feishu_settings_service or (
        create_default_feishu_settings_service(
            channel_data_path / DEFAULT_FEISHU_SETTINGS_PATH.name
        )
    )
    selected_channel_store = SQLiteChannelStore(
        channel_data_path / DEFAULT_CHANNEL_STORE_PATH.name
    )
    selected_identity_registry = identity_registry or IdentityBindingRegistry(
        channel_data_path / DEFAULT_IDENTITY_REGISTRY_PATH.name
    )
    selected_viewer_service = viewer_service or LanViewerService.from_env(
        selected_spatial_service,
        data_path=channel_data_path,
    )
    selected_capability_setup_service = (
        capability_setup_service
        or CapabilitySetupService(
            channel_data_path,
            style_probe=selected_style_service.provider_status,
            flux_probe=selected_flux_gs_service.provider.status,
        )
    )
    selected_feishu_runtime = feishu_runtime or FeishuChannelRuntime(
        selected_feishu_settings_service,
        runner,
        selected_channel_store,
        spatial_service=selected_spatial_service,
        style_service=selected_style_service,
        flux_gs_service=selected_flux_gs_service,
        viewer_link_factory=selected_viewer_service.create_link,
        identity_registry=selected_identity_registry,
    )
    selected_channel_store = getattr(
        selected_feishu_runtime,
        "store",
        selected_channel_store,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await selected_feishu_runtime.start_if_enabled()
        try:
            yield
        finally:
            await selected_feishu_runtime.stop()
            selected_capability_setup_service.close()
            selected_viewer_service.close()
            runner.close()
            llm_runtime.close()
            selected_style_service.close()
            selected_flux_gs_service.close()
            selected_spatial_service.close()

    app = FastAPI(
        title="Agent Lab API",
        description=(
            "A local-first personal AI assistant with tool calling, private assets, "
            "and spatial photo generation."
        ),
        version="0.5.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        # Vinext/Vite automatically selects another free development port when
        # 3000 is occupied.  Keep the API local-only while allowing that normal
        # fallback (3001, 5173, and similar localhost ports).
        allow_origin_regex=(
            r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
    app.state.registry = registry
    app.state.runner = runner
    app.state.settings_service = selected_settings_service
    app.state.llm_runtime = llm_runtime
    app.state.spatial_service = selected_spatial_service
    app.state.style_service = selected_style_service
    app.state.flux_gs_service = selected_flux_gs_service
    app.state.feishu_settings_service = selected_feishu_settings_service
    app.state.feishu_runtime = selected_feishu_runtime
    app.state.channel_store = selected_channel_store
    app.state.viewer_service = selected_viewer_service
    app.state.identity_registry = selected_identity_registry
    app.state.capability_setup_service = selected_capability_setup_service
    app.include_router(create_flux_gs_router())
    app.include_router(
        create_capability_setup_router(selected_capability_setup_service)
    )

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        current_registry: ToolRegistry = request.app.state.registry
        current_runner: AgentRunner = request.app.state.runner
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        runtime_status = runtime.public_status()
        return HealthResponse(
            status="ok",
            agent_mode=current_runner.planner.mode,
            llm_configured=current_runner.planner.is_llm,
            llm_status=runtime_status.status,
            llm_provider=runtime_status.provider_id,
            model=current_runner.planner.model_name,
            context_tokenizer=(
                current_runner.context_builder.token_counter.identifier
            ),
            session_summary_provider=(
                str(current_runner.planner.summary_provider_id)
                if callable(
                    getattr(current_runner.planner, "summarize_session", None)
                )
                else "fallback:extractive-v2"
            ),
            session_summary_schema=SESSION_SUMMARY_SCHEMA_VERSION,
            tool_count=len(current_registry.list_tools()),
            feishu_status=request.app.state.feishu_runtime.public_status().status,
        )

    @app.get("/api/tools", response_model=list[ToolInfo])
    def list_tools(request: Request) -> list[ToolInfo]:
        current_registry: ToolRegistry = request.app.state.registry
        return current_registry.list_tools()

    @app.get("/api/capabilities", response_model=list[CapabilityInfo])
    def list_capability_manifests(request: Request) -> list[CapabilityInfo]:
        current_registry: ToolRegistry = request.app.state.registry
        return current_registry.list_capabilities()

    @app.get(
        "/api/conversations",
        response_model=ConversationListResponse,
    )
    def list_conversations(
        request: Request,
        channel: str = Query(default="web", max_length=40),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> ConversationListResponse:
        runner: AgentRunner = request.app.state.runner
        conversations = runner.memory_store.list_threads(
            owner_id=WEB_OWNER_ID,
            channel=channel,
            limit=limit,
        )
        return ConversationListResponse(
            conversations=[
                _conversation_public(conversation)
                for conversation in conversations
            ]
        )

    @app.post(
        "/api/conversations",
        response_model=ConversationPublic,
        status_code=201,
    )
    def create_conversation(
        payload: ConversationCreateRequest,
        request: Request,
    ) -> ConversationPublic:
        runner: AgentRunner = request.app.state.runner
        session_id = payload.session_id or str(uuid4())
        thread_id = _web_thread_id(session_id)
        existing = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="这个会话已经存在。")
        runner.memory_store.ensure_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
            channel="web",
            title=payload.title,
        )
        created = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        assert created is not None
        return _conversation_public(created)

    @app.get(
        "/api/conversations/{session_id}/messages",
        response_model=ConversationMessageListResponse,
    )
    def list_conversation_messages(
        session_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> ConversationMessageListResponse:
        runner: AgentRunner = request.app.state.runner
        thread_id = _web_thread_id(session_id)
        conversation = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        if conversation is None or conversation.channel != "web":
            raise HTTPException(status_code=404, detail="找不到这个会话。")
        messages = runner.memory_store.list_messages(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
            limit=limit,
        )
        return ConversationMessageListResponse(
            messages=[
                ConversationMessagePublic(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    created_at=datetime.fromtimestamp(
                        message.created_at,
                        tz=timezone.utc,
                    ),
                    run_id=message.run_id,
                    metadata=message.metadata,
                )
                for message in messages
            ]
        )

    @app.put(
        "/api/conversations/{session_id}",
        response_model=ConversationPublic,
    )
    def rename_conversation(
        session_id: str,
        payload: ConversationRenameRequest,
        request: Request,
    ) -> ConversationPublic:
        runner: AgentRunner = request.app.state.runner
        thread_id = _web_thread_id(session_id)
        existing = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        if existing is None or existing.channel != "web":
            raise HTTPException(status_code=404, detail="找不到这个会话。")
        runner.memory_store.rename_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
            title=payload.title,
        )
        updated = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        assert updated is not None
        return _conversation_public(updated)

    @app.delete("/api/conversations/{session_id}", status_code=204)
    def delete_conversation(session_id: str, request: Request) -> Response:
        runner: AgentRunner = request.app.state.runner
        thread_id = _web_thread_id(session_id)
        existing = runner.memory_store.get_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        if existing is None or existing.channel != "web":
            raise HTTPException(status_code=404, detail="找不到这个会话。")
        runner.memory_store.delete_thread(
            owner_id=WEB_OWNER_ID,
            thread_id=thread_id,
        )
        return Response(status_code=204)

    @app.get("/api/memories/export", response_model=MemoryExportResponse)
    def export_memories(request: Request) -> MemoryExportResponse:
        runner: AgentRunner = request.app.state.runner
        records = runner.memory_store.list_memory_records(
            owner_id=WEB_OWNER_ID,
            status=None,
            limit=None,
        )
        return MemoryExportResponse(
            exported_at=datetime.now(timezone.utc),
            memories=[_memory_public(record) for record in records],
        )

    @app.get("/api/memories", response_model=MemoryListResponse)
    def list_memories(
        request: Request,
        memory_type: MemoryType | None = Query(default=None, alias="type"),
        scope: MemoryScope | None = Query(default=None),
        status: MemoryStatus | None = Query(default="active"),
        query: str | None = Query(default=None, max_length=160),
        include_inactive: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> MemoryListResponse:
        runner: AgentRunner = request.app.state.runner
        records = runner.memory_store.list_memory_records(
            owner_id=WEB_OWNER_ID,
            memory_type=memory_type,
            scope=scope,
            status=None if include_inactive else status,
            query=query,
            limit=limit,
        )
        return MemoryListResponse(
            memories=[_memory_public(record) for record in records]
        )

    @app.post(
        "/api/memories",
        response_model=MemoryPublic,
        status_code=201,
    )
    def create_memory(
        payload: MemoryCreateRequest,
        request: Request,
    ) -> MemoryPublic:
        runner: AgentRunner = request.app.state.runner
        try:
            memory_id = runner.memory_store.remember(
                owner_id=WEB_OWNER_ID,
                content=payload.content,
                source="web:memory-center",
                memory_type=payload.memory_type,
                scope=payload.scope,
                scope_id=payload.scope_id,
                confidence=payload.confidence,
                importance=payload.importance,
                sensitivity=payload.sensitivity,
                retrieval_policy=payload.retrieval_policy,
                valid_from=(
                    payload.valid_from.timestamp()
                    if payload.valid_from is not None
                    else None
                ),
                valid_to=(
                    payload.valid_to.timestamp()
                    if payload.valid_to is not None
                    else None
                ),
                metadata=payload.metadata,
            )
        except (MemoryPolicyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        created = runner.memory_store.get_memory(
            owner_id=WEB_OWNER_ID,
            memory_id=memory_id,
        )
        assert created is not None
        return _memory_public(created)

    @app.post(
        "/api/memories/atomic",
        response_model=MemoryListResponse,
        status_code=201,
    )
    def create_atomic_memories(
        payload: MemoryCreateRequest,
        request: Request,
    ) -> MemoryListResponse:
        runner: AgentRunner = request.app.state.runner
        try:
            memory_ids = runner.memory_store.remember_many(
                owner_id=WEB_OWNER_ID,
                content=payload.content,
                source="web:memory-center",
                memory_type=payload.memory_type,
                scope=payload.scope,
                scope_id=payload.scope_id,
                confidence=payload.confidence,
                importance=payload.importance,
                sensitivity=payload.sensitivity,
                retrieval_policy=payload.retrieval_policy,
                valid_from=(
                    payload.valid_from.timestamp()
                    if payload.valid_from is not None
                    else None
                ),
                valid_to=(
                    payload.valid_to.timestamp()
                    if payload.valid_to is not None
                    else None
                ),
                metadata=payload.metadata,
            )
        except (MemoryPolicyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        records = [
            runner.memory_store.get_memory(
                owner_id=WEB_OWNER_ID,
                memory_id=memory_id,
            )
            for memory_id in memory_ids
        ]
        return MemoryListResponse(
            memories=[_memory_public(record) for record in records if record is not None]
        )

    @app.put("/api/memories/{memory_id}", response_model=MemoryPublic)
    def update_memory(
        memory_id: str,
        payload: MemoryUpdateRequest,
        request: Request,
    ) -> MemoryPublic:
        runner: AgentRunner = request.app.state.runner
        updates = payload.model_dump(exclude_unset=True)
        for field in ("valid_from", "valid_to"):
            value = updates.get(field)
            if isinstance(value, datetime):
                updates[field] = value.timestamp()
        try:
            updated = runner.memory_store.update_memory(
                owner_id=WEB_OWNER_ID,
                memory_id=memory_id,
                updates=updates,
            )
        except (MemoryPolicyError, ValueError) as exc:
            status_code = 404 if "找不到" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return _memory_public(updated)

    @app.get(
        "/api/memories/{memory_id}/evidence",
        response_model=MemoryEvidenceListResponse,
    )
    def list_memory_evidence(
        memory_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> MemoryEvidenceListResponse:
        runner: AgentRunner = request.app.state.runner
        memory = runner.memory_store.get_memory(
            owner_id=WEB_OWNER_ID,
            memory_id=memory_id,
        )
        if memory is None:
            raise HTTPException(status_code=404, detail="找不到这条记忆。")
        evidence = runner.memory_store.list_memory_evidence(
            owner_id=WEB_OWNER_ID,
            memory_id=memory_id,
            limit=limit,
        )
        return MemoryEvidenceListResponse(
            evidence=[
                MemoryEvidencePublic(
                    evidence_id=item.evidence_id,
                    memory_id=item.memory_id,
                    source_type=item.source_type,
                    source=item.source,
                    excerpt=item.excerpt,
                    content_hash=item.content_hash,
                    source_message_id=item.source_message_id,
                    source_run_id=item.source_run_id,
                    confidence=item.confidence,
                    observed_at=datetime.fromtimestamp(
                        item.observed_at,
                        tz=timezone.utc,
                    ),
                    created_at=datetime.fromtimestamp(
                        item.created_at,
                        tz=timezone.utc,
                    ),
                )
                for item in evidence
            ]
        )

    @app.delete("/api/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, request: Request) -> Response:
        runner: AgentRunner = request.app.state.runner
        if not runner.memory_store.delete_memory(
            owner_id=WEB_OWNER_ID,
            memory_id=memory_id,
        ):
            raise HTTPException(status_code=404, detail="找不到这条记忆。")
        return Response(status_code=204)

    @app.get(
        "/api/channels/messages",
        response_model=ChannelMessageListResponse,
    )
    def list_channel_messages(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> ChannelMessageListResponse:
        store: SQLiteChannelStore = request.app.state.channel_store
        return ChannelMessageListResponse(messages=store.list_events(limit))

    @app.delete(
        "/api/channels/conversations/{chat_id}",
        status_code=204,
    )
    def delete_channel_conversation(chat_id: str, request: Request) -> Response:
        store: SQLiteChannelStore = request.app.state.channel_store
        if not store.delete_chat_events(chat_id):
            raise HTTPException(status_code=404, detail="找不到这个飞书会话镜像。")
        return Response(status_code=204)

    @app.post(
        "/api/source-images",
        response_model=SourceImagePublic,
        status_code=201,
    )
    async def stage_source_image(
        request: Request,
        file: UploadFile = File(...),
    ) -> SourceImagePublic:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            image_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
            return service.stage_source_image(
                image_bytes,
                original_name=file.filename or "空间照片",
            )
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await file.close()

    @app.delete("/api/source-images/{source_image_id}", status_code=204)
    def delete_source_image(source_image_id: str, request: Request) -> Response:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            service.delete_source_image(source_image_id)
            return Response(status_code=204)
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/source-images/{source_image_id}/content")
    def get_source_image_content(
        source_image_id: str,
        request: Request,
    ) -> FileResponse:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            path = service.resolve_source_image_file(
                source_image_id,
                owner_id=WEB_OWNER_ID,
            )
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="image/webp",
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post(
        "/api/spatial-scenes",
        response_model=SpatialSceneCreateResponse,
        status_code=202,
    )
    async def create_spatial_scene(
        request: Request,
        file: UploadFile = File(...),
        title: str | None = Form(default=None),
    ) -> SpatialSceneCreateResponse:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            image_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
            return service.create_scene(
                image_bytes,
                original_name=file.filename or "空间照片",
                title=title,
            )
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await file.close()

    @app.post(
        "/api/photo-style-transfers",
        response_model=PhotoStyleCreateResponse,
        status_code=202,
    )
    async def create_photo_style_transfer(
        request: Request,
        content_file: UploadFile = File(...),
        style_files: list[UploadFile] = File(...),
        title: str | None = Form(default=None),
        prompt: str = Form(default=""),
        mode: str = Form(default="preserve_layout"),
        quality: str = Form(default="standard"),
        style_preset: str = Form(default="auto"),
        style_strength: float = Form(default=0.7),
        content_strength: float = Form(default=0.8),
        detail_strength: float = Form(default=0.7),
        seed: int | None = Form(default=None),
    ) -> PhotoStyleCreateResponse:
        service: PhotoStyleService = request.app.state.style_service
        try:
            content_bytes = await content_file.read(MAX_UPLOAD_BYTES + 1)
            style_bytes = [
                await style_file.read(MAX_UPLOAD_BYTES + 1)
                for style_file in style_files
            ]
            return service.create_transfer(
                content_bytes,
                style_bytes,
                original_name=content_file.filename or "图片风格化",
                title=title,
                parameters=StyleParameters(
                    mode=mode,
                    quality=quality,
                    style_preset=style_preset,
                    style_strength=style_strength,
                    content_strength=content_strength,
                    detail_strength=detail_strength,
                    prompt=prompt,
                    seed=seed,
                ),
                idempotency_key=_request_idempotency_key(request),
            )
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await content_file.close()
            for style_file in style_files:
                await style_file.close()

    @app.get(
        "/api/photo-style-transfers/provider",
        response_model=PhotoStyleProviderStatus,
    )
    def get_photo_style_provider(request: Request) -> PhotoStyleProviderStatus:
        service: PhotoStyleService = request.app.state.style_service
        return PhotoStyleProviderStatus.model_validate(service.provider_status())

    @app.get("/api/assets", response_model=AssetListResponse)
    def list_assets(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> AssetListResponse:
        service: SpatialSceneService = request.app.state.spatial_service
        return AssetListResponse(assets=service.list_assets(limit))

    @app.get("/api/assets/{asset_id}", response_model=AssetPublic)
    def get_asset(asset_id: str, request: Request) -> AssetPublic:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            return service.get_asset(asset_id)
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/assets/{asset_id}/viewer-link",
        response_model=ViewerLinkPublic,
    )
    def create_viewer_link(
        asset_id: str,
        request: Request,
    ) -> ViewerLinkPublic:
        viewer: LanViewerService = request.app.state.viewer_service
        try:
            return ViewerLinkPublic(
                asset_id=asset_id,
                url=viewer.create_link(asset_id),
                expires_in_seconds=viewer.ttl_seconds,
            )
        except (AssetError, ViewerLinkError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/assets/{asset_id}", status_code=204)
    def delete_asset(asset_id: str, request: Request) -> Response:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            service.delete_asset(asset_id)
            return Response(status_code=204)
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/assets/{asset_id}/files/{filename}")
    def get_asset_file(
        asset_id: str,
        filename: str,
        request: Request,
    ) -> FileResponse:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            path = service.resolve_asset_file(asset_id, filename)
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/jobs", response_model=JobListResponse)
    def list_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> JobListResponse:
        service: SpatialSceneService = request.app.state.spatial_service
        return JobListResponse(jobs=service.list_jobs(limit))

    @app.get("/api/jobs/{job_id}", response_model=JobPublic)
    def get_job(job_id: str, request: Request) -> JobPublic:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            return service.get_job(job_id)
        except AssetError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/retry",
        response_model=JobPublic,
        status_code=202,
    )
    def retry_job(job_id: str, request: Request) -> JobPublic:
        service: SpatialSceneService = request.app.state.spatial_service
        try:
            job, _ = service.retry_job(job_id)
            return job
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/api/settings/providers",
        response_model=ProviderCatalogResponse,
    )
    def provider_settings(request: Request) -> ProviderCatalogResponse:
        service: SettingsService = request.app.state.settings_service
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        return service.catalog(runtime.public_status())

    @app.put(
        "/api/settings/llm",
        response_model=LLMSettingsPublic,
    )
    def save_llm_settings(
        payload: LLMSettingsUpdate,
        request: Request,
    ) -> LLMSettingsPublic:
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        current_runner: AgentRunner = request.app.state.runner
        try:
            settings, _, planner = runtime.apply(payload)
            current_runner.planner = planner
            return settings
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get(
        "/api/settings/llm/status",
        response_model=LLMRuntimePublic,
    )
    def llm_runtime_status(request: Request) -> LLMRuntimePublic:
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        return runtime.public_status()

    @app.post(
        "/api/settings/llm/test",
        response_model=ConnectionTestResponse,
    )
    def test_llm_settings(
        payload: LLMSettingsUpdate,
        request: Request,
    ) -> ConnectionTestResponse:
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        try:
            return runtime.test(payload)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete(
        "/api/settings/llm/api-key",
        response_model=LLMSettingsPublic,
    )
    def clear_llm_api_key(request: Request) -> LLMSettingsPublic:
        runtime: LLMRuntimeManager = request.app.state.llm_runtime
        current_runner: AgentRunner = request.app.state.runner
        settings, planner = runtime.clear_key()
        current_runner.planner = planner
        return settings

    @app.get(
        "/api/settings/feishu",
        response_model=FeishuSettingsPublic,
    )
    def get_feishu_settings(request: Request) -> FeishuSettingsPublic:
        service: FeishuSettingsService = request.app.state.feishu_settings_service
        runtime: FeishuChannelRuntime = request.app.state.feishu_runtime
        return service.public_settings(runtime.public_status())

    @app.put(
        "/api/settings/feishu",
        response_model=FeishuSettingsPublic,
    )
    async def save_feishu_settings(
        payload: FeishuSettingsUpdate,
        request: Request,
    ) -> FeishuSettingsPublic:
        service: FeishuSettingsService = request.app.state.feishu_settings_service
        runtime: FeishuChannelRuntime = request.app.state.feishu_runtime
        try:
            stored = service.save(payload)
            await runtime.apply_settings(stored)
            return service.public_settings(runtime.public_status())
        except FeishuSettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/settings/feishu/test",
        response_model=FeishuConnectionTestResponse,
    )
    def test_feishu_settings(
        payload: FeishuSettingsUpdate,
        request: Request,
    ) -> FeishuConnectionTestResponse:
        service: FeishuSettingsService = request.app.state.feishu_settings_service
        try:
            return service.test_connection(payload)
        except FeishuSettingsError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete(
        "/api/settings/feishu/app-secret",
        response_model=FeishuSettingsPublic,
    )
    async def clear_feishu_app_secret(request: Request) -> FeishuSettingsPublic:
        service: FeishuSettingsService = request.app.state.feishu_settings_service
        runtime: FeishuChannelRuntime = request.app.state.feishu_runtime
        stored = service.clear_secret()
        await runtime.apply_settings(stored)
        return service.public_settings(runtime.public_status())

    @app.get(
        "/api/admin/identity-bindings",
        response_model=IdentityBindingListResponse,
    )
    def list_identity_bindings(
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> IdentityBindingListResponse:
        _require_local_root(request)
        bindings: IdentityBindingRegistry = request.app.state.identity_registry
        return IdentityBindingListResponse(
            bindings=[
                _identity_binding_public(binding)
                for binding in bindings.list_bindings(limit)
            ]
        )

    @app.post(
        "/api/admin/identity-bindings",
        response_model=IdentityBindingPublic,
        status_code=201,
    )
    def create_identity_binding(
        payload: IdentityBindingCreateRequest,
        request: Request,
    ) -> IdentityBindingPublic:
        _require_local_root(request)
        registry: IdentityBindingRegistry = request.app.state.identity_registry
        service: FeishuSettingsService = request.app.state.feishu_settings_service
        app_id = (payload.app_id or service.load().app_id).strip()
        if not app_id:
            raise HTTPException(
                status_code=422,
                detail="请先配置飞书 App ID，或在请求中明确提供 App ID。",
            )
        try:
            return _identity_binding_public(
                registry.ensure_feishu_binding(
                    app_id=app_id,
                    open_id=payload.open_id,
                    workspace_name=payload.workspace_name,
                )
            )
        except IdentityBindingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put(
        "/api/admin/identity-bindings/{binding_id}/status",
        response_model=IdentityBindingPublic,
    )
    def update_identity_binding_status(
        binding_id: str,
        payload: IdentityBindingStatusUpdate,
        request: Request,
    ) -> IdentityBindingPublic:
        _require_local_root(request)
        registry: IdentityBindingRegistry = request.app.state.identity_registry
        try:
            return _identity_binding_public(
                registry.set_binding_status(binding_id, payload.status)
            )
        except IdentityBindingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put(
        "/api/admin/identity-bindings/{binding_id}/workspace",
        response_model=IdentityBindingPublic,
    )
    def rename_identity_workspace(
        binding_id: str,
        payload: IdentityWorkspaceRenameRequest,
        request: Request,
    ) -> IdentityBindingPublic:
        _require_local_root(request)
        registry: IdentityBindingRegistry = request.app.state.identity_registry
        try:
            return _identity_binding_public(
                registry.rename_workspace(binding_id, payload.name)
            )
        except IdentityBindingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agent/run", response_model=AgentRunResponse)
    def run_agent(payload: AgentRunRequest, request: Request) -> AgentRunResponse:
        current_runner: AgentRunner = request.app.state.runner
        spatial: SpatialSceneService = request.app.state.spatial_service
        try:
            attachment_ids = list(dict.fromkeys(payload.attachment_image_ids))
            attachment_contexts = [
                spatial.get_source_image(source_id).model_dump()
                for source_id in attachment_ids
            ]
            source_context = (
                spatial.get_source_image(payload.source_image_id).model_dump()
                if payload.source_image_id
                else None
            )
            style_contexts = [
                spatial.get_source_image(source_id).model_dump()
                for source_id in payload.style_image_ids
            ]
            response = current_runner.run(
                payload.message,
                attachment_contexts=attachment_contexts,
                source_image_context=source_context,
                style_image_contexts=style_contexts,
                owner_id=WEB_OWNER_ID,
                thread_id=_web_thread_id(payload.session_id or "default"),
                channel="web",
                project_id=payload.project_id,
            )
            release_ids = {
                str(source_id)
                for step in response.steps
                for source_id in (
                    (step.output or {}).get("release_image_ids", [])
                    if isinstance(
                        (step.output or {}).get("release_image_ids", []),
                        list,
                    )
                    else []
                )
            }
            for source_id in release_ids:
                try:
                    spatial.delete_source_image(source_id)
                except AssetError:
                    pass
            return response
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ContextWindowExceededError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/agent/runs", response_model=AgentRunListResponse)
    def list_agent_runs(
        request: Request,
        status: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> AgentRunListResponse:
        runner: AgentRunner = request.app.state.runner
        return AgentRunListResponse(
            runs=runner.list_runs(status=status, limit=limit)
        )

    @app.get("/api/agent/runs/{run_id}", response_model=AgentRunResponse)
    def get_agent_run(run_id: str, request: Request) -> AgentRunResponse:
        runner: AgentRunner = request.app.state.runner
        try:
            return runner.get_run(run_id, is_admin=True)
        except AgentRunError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/api/agent/runs/{run_id}/decision",
        response_model=AgentRunResponse,
    )
    async def decide_agent_run(
        run_id: str,
        payload: AgentRunDecisionRequest,
        request: Request,
    ) -> AgentRunResponse:
        runner: AgentRunner = request.app.state.runner
        try:
            stored = runner.memory_store.get_run(run_id)
            response = runner.resume_run(
                run_id,
                approved=payload.approved,
                is_admin=True,
            )
            if (
                stored is not None
                and stored.channel == "feishu"
            ):
                runtime: FeishuChannelRuntime = (
                    request.app.state.feishu_runtime
                )
                await runtime.send_agent_run_update(
                    thread_id=stored.thread_id,
                    run_id=run_id,
                    answer=response.answer,
                )
            return response
        except AgentRunIsolationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/agent/runs/{run_id}/resume",
        response_model=AgentRunResponse,
    )
    def resume_interrupted_agent_run(
        run_id: str,
        request: Request,
    ) -> AgentRunResponse:
        runner: AgentRunner = request.app.state.runner
        try:
            return runner.continue_run(run_id, is_admin=True)
        except AgentRunIsolationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/agent/runs/{run_id}/abandon",
        response_model=AgentRunResponse,
    )
    def abandon_interrupted_agent_run(
        run_id: str,
        request: Request,
    ) -> AgentRunResponse:
        runner: AgentRunner = request.app.state.runner
        try:
            return runner.abandon_interrupted_run(run_id, is_admin=True)
        except AgentRunIsolationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except AgentRunError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app


app = create_app()
