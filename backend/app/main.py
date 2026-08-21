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
from .llm import LLMError
from .channel_settings import (
    FeishuSettingsError,
    FeishuSettingsService,
    create_default_feishu_settings_service,
)
from .feishu import FeishuChannelRuntime, SQLiteChannelStore
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
    JobListResponse,
    JobPublic,
    LLMSettingsPublic,
    LLMSettingsUpdate,
    PhotoStyleCreateResponse,
    PhotoStyleProviderStatus,
    ProviderCatalogResponse,
    SourceImagePublic,
    SpatialSceneCreateResponse,
    ToolInfo,
    ViewerLinkPublic,
)
from .settings import (
    SettingsError,
    SettingsService,
    create_default_settings_service,
)
from .style_transfer import PhotoStyleService, StyleParameters, register_style_tools
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
DEFAULT_ASSET_DATA_PATH = Path(__file__).resolve().parents[1] / "data"
WEB_OWNER_ID = "local"
WEB_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _web_thread_id(session_id: str) -> str:
    safe_session_id = session_id.strip()
    if not WEB_SESSION_PATTERN.fullmatch(safe_session_id):
        raise HTTPException(status_code=422, detail="会话 ID 格式无效。")
    return f"web:{safe_session_id}"


def _public_session_id(thread_id: str) -> str:
    return thread_id.removeprefix("web:")


def _conversation_public(summary: object) -> ConversationPublic:
    return ConversationPublic(
        id=_public_session_id(summary.thread_id),
        channel=summary.channel,
        title=summary.title,
        created_at=datetime.fromtimestamp(summary.created_at, tz=timezone.utc),
        updated_at=datetime.fromtimestamp(summary.updated_at, tz=timezone.utc),
        message_count=summary.message_count,
    )


def create_app(
    trace_path: Path = DEFAULT_TRACE_PATH,
    planner: Planner | None = None,
    settings_service: SettingsService | None = None,
    spatial_service: SpatialSceneService | None = None,
    style_service: PhotoStyleService | None = None,
    feishu_settings_service: FeishuSettingsService | None = None,
    feishu_runtime: FeishuChannelRuntime | None = None,
    viewer_service: LanViewerService | None = None,
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
    selected_settings_service = settings_service or create_default_settings_service(
        DEFAULT_SETTINGS_PATH
    )
    selected_planner = planner or selected_settings_service.build_planner()
    runner = AgentRunner(
        registry,
        selected_planner,
        JsonlTraceStore(trace_path),
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
    selected_viewer_service = viewer_service or LanViewerService.from_env(
        selected_spatial_service,
        data_path=channel_data_path,
    )
    selected_feishu_runtime = feishu_runtime or FeishuChannelRuntime(
        selected_feishu_settings_service,
        runner,
        selected_channel_store,
        spatial_service=selected_spatial_service,
        viewer_link_factory=selected_viewer_service.create_link,
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
            selected_viewer_service.close()
            runner.close()
            selected_style_service.close()
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
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    app.state.registry = registry
    app.state.runner = runner
    app.state.settings_service = selected_settings_service
    app.state.spatial_service = selected_spatial_service
    app.state.style_service = selected_style_service
    app.state.feishu_settings_service = selected_feishu_settings_service
    app.state.feishu_runtime = selected_feishu_runtime
    app.state.channel_store = selected_channel_store
    app.state.viewer_service = selected_viewer_service

    @app.get("/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        current_registry: ToolRegistry = request.app.state.registry
        current_runner: AgentRunner = request.app.state.runner
        return HealthResponse(
            status="ok",
            agent_mode=current_runner.planner.mode,
            llm_configured=current_runner.planner.is_llm,
            model=current_runner.planner.model_name,
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
                    style_strength=style_strength,
                    content_strength=content_strength,
                    detail_strength=detail_strength,
                    prompt=prompt,
                    seed=seed,
                ),
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

    @app.get(
        "/api/settings/providers",
        response_model=ProviderCatalogResponse,
    )
    def provider_settings(request: Request) -> ProviderCatalogResponse:
        service: SettingsService = request.app.state.settings_service
        return service.catalog()

    @app.put(
        "/api/settings/llm",
        response_model=LLMSettingsPublic,
    )
    def save_llm_settings(
        payload: LLMSettingsUpdate,
        request: Request,
    ) -> LLMSettingsPublic:
        service: SettingsService = request.app.state.settings_service
        current_runner: AgentRunner = request.app.state.runner
        try:
            settings = service.save(payload)
            current_runner.planner = service.build_planner()
            return settings
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/settings/llm/test",
        response_model=ConnectionTestResponse,
    )
    def test_llm_settings(
        payload: LLMSettingsUpdate,
        request: Request,
    ) -> ConnectionTestResponse:
        service: SettingsService = request.app.state.settings_service
        current_registry: ToolRegistry = request.app.state.registry
        try:
            return service.test_connection(payload, current_registry)
        except SettingsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.delete(
        "/api/settings/llm/api-key",
        response_model=LLMSettingsPublic,
    )
    def clear_llm_api_key(request: Request) -> LLMSettingsPublic:
        service: SettingsService = request.app.state.settings_service
        current_runner: AgentRunner = request.app.state.runner
        settings = service.clear_key()
        current_runner.planner = service.build_planner()
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

    @app.post("/api/agent/run", response_model=AgentRunResponse)
    def run_agent(payload: AgentRunRequest, request: Request) -> AgentRunResponse:
        current_runner: AgentRunner = request.app.state.runner
        spatial: SpatialSceneService = request.app.state.spatial_service
        try:
            source_context = (
                spatial.get_source_image(payload.source_image_id).model_dump()
                if payload.source_image_id
                else None
            )
            style_contexts = [
                spatial.get_source_image(source_id).model_dump()
                for source_id in payload.style_image_ids
            ]
            return current_runner.run(
                payload.message,
                source_image_context=source_context,
                style_image_contexts=style_contexts,
                owner_id=WEB_OWNER_ID,
                thread_id=_web_thread_id(payload.session_id or "default"),
                channel="web",
            )
        except AssetError as exc:
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

    return app


app = create_app()
