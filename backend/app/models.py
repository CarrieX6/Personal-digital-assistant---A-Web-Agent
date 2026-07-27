from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr


class AgentRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=100)
    source_image_id: str | None = Field(default=None, max_length=100)


class ToolCall(BaseModel):
    call_id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TraceStep(BaseModel):
    index: int
    stage: Literal["planning", "tool", "final"]
    label: str
    detail: str
    duration_ms: int = Field(ge=0)
    output: dict[str, Any] | None = None


class AgentRunResponse(BaseModel):
    run_id: str
    status: Literal["completed", "failed"]
    mode: str
    answer: str
    steps: list[TraceStep]
    total_duration_ms: int = Field(ge=0)


class ToolInfo(BaseModel):
    name: str
    description: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    agent_mode: str
    llm_configured: bool
    model: str | None = None
    tool_count: int


class ProviderPreset(BaseModel):
    id: str
    name: str
    description: str
    base_url: str
    default_model: str
    models: list[str]
    docs_url: HttpUrl | None = None


class LLMSettingsUpdate(BaseModel):
    enabled: bool = True
    provider_id: str = Field(min_length=1, max_length=50)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=30, ge=5, le=120)


class LLMSettingsPublic(BaseModel):
    enabled: bool
    provider_id: str
    base_url: str
    model: str
    timeout_seconds: float
    has_api_key: bool
    masked_api_key: str | None = None
    secret_storage: str


class ProviderCatalogResponse(BaseModel):
    providers: list[ProviderPreset]
    settings: LLMSettingsPublic


class ConnectionTestResponse(BaseModel):
    ok: Literal[True]
    model: str
    selected_tools: list[str]
    latency_ms: int
    message: str


AssetStatus = Literal["processing", "ready", "failed"]
JobStatus = Literal["queued", "running", "completed", "failed"]


class AssetPublic(BaseModel):
    id: str
    kind: Literal["spatial_scene"]
    name: str
    status: AssetStatus
    width: int | None = None
    height: int | None = None
    source_url: str
    preview_url: str | None = None
    depth_url: str | None = None
    background_url: str | None = None
    foreground_url: str | None = None
    manifest_url: str | None = None
    model_name: str | None = None
    created_at: datetime
    updated_at: datetime


class JobPublic(BaseModel):
    id: str
    kind: Literal["spatial_scene"]
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    stage: str
    message: str
    asset_id: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class SpatialSceneCreateResponse(BaseModel):
    asset: AssetPublic
    job: JobPublic


class SourceImagePublic(BaseModel):
    id: str
    original_name: str
    width: int
    height: int
    size_bytes: int = Field(ge=1)


class AssetListResponse(BaseModel):
    assets: list[AssetPublic]


class JobListResponse(BaseModel):
    jobs: list[JobPublic]
