from __future__ import annotations

import mimetypes
from pathlib import Path

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

from .agent import AgentRunner, JsonlTraceStore, Planner
from .assets import (
    MAX_UPLOAD_BYTES,
    AssetError,
    SpatialSceneService,
    register_asset_tools,
)
from .llm import LLMError
from .models import (
    AgentRunRequest,
    AgentRunResponse,
    AssetListResponse,
    AssetPublic,
    ConnectionTestResponse,
    HealthResponse,
    JobListResponse,
    JobPublic,
    LLMSettingsPublic,
    LLMSettingsUpdate,
    ProviderCatalogResponse,
    SourceImagePublic,
    SpatialSceneCreateResponse,
    ToolInfo,
)
from .settings import (
    SettingsError,
    SettingsService,
    create_default_settings_service,
)
from .tools import ToolError, ToolRegistry, build_default_registry


load_dotenv()

DEFAULT_TRACE_PATH = Path(__file__).resolve().parents[1] / "data" / "runs.jsonl"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "settings.json"
DEFAULT_ASSET_DATA_PATH = Path(__file__).resolve().parents[1] / "data"


def create_app(
    trace_path: Path = DEFAULT_TRACE_PATH,
    planner: Planner | None = None,
    settings_service: SettingsService | None = None,
    spatial_service: SpatialSceneService | None = None,
) -> FastAPI:
    registry = build_default_registry()
    selected_spatial_service = spatial_service or SpatialSceneService(
        DEFAULT_ASSET_DATA_PATH
    )
    register_asset_tools(registry, selected_spatial_service)
    selected_settings_service = settings_service or create_default_settings_service(
        DEFAULT_SETTINGS_PATH
    )
    selected_planner = planner or selected_settings_service.build_planner()
    runner = AgentRunner(
        registry,
        selected_planner,
        JsonlTraceStore(trace_path),
    )

    app = FastAPI(
        title="Agent Lab API",
        description=(
            "A local-first personal AI assistant with tool calling, private assets, "
            "and spatial photo generation."
        ),
        version="0.4.0",
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
        )

    @app.get("/api/tools", response_model=list[ToolInfo])
    def list_tools(request: Request) -> list[ToolInfo]:
        current_registry: ToolRegistry = request.app.state.registry
        return current_registry.list_tools()

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
            return current_runner.run(
                payload.message,
                source_image_context=source_context,
            )
        except AssetError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ToolError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return app


app = create_app()
