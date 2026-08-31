from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr


class AgentRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=100)
    source_image_id: str | None = Field(default=None, max_length=100)
    style_image_ids: list[str] = Field(default_factory=list, max_length=3)


class ToolCall(BaseModel):
    call_id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TraceStep(BaseModel):
    index: int
    stage: Literal[
        "planning",
        "policy",
        "approval",
        "tool",
        "decision",
        "final",
    ]
    label: str
    detail: str
    duration_ms: int = Field(ge=0)
    output: dict[str, Any] | None = None


class AgentRunResponse(BaseModel):
    run_id: str
    status: Literal["waiting_approval", "completed", "failed"]
    mode: str
    answer: str
    steps: list[TraceStep]
    total_duration_ms: int = Field(ge=0)
    approval: dict[str, Any] | None = None


class AgentRunDecisionRequest(BaseModel):
    approved: bool


class AgentRunListResponse(BaseModel):
    runs: list[AgentRunResponse]


class ConversationCreateRequest(BaseModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(default="新对话", min_length=1, max_length=80)


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class ConversationPublic(BaseModel):
    id: str
    channel: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(ge=0)


class ConversationListResponse(BaseModel):
    conversations: list[ConversationPublic]


class ConversationMessagePublic(BaseModel):
    id: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    created_at: datetime
    run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationMessageListResponse(BaseModel):
    messages: list[ConversationMessagePublic]


class ToolInfo(BaseModel):
    name: str
    description: str
    risk_level: Literal[
        "read", "local_write", "external_write", "destructive"
    ] = "read"
    requires_approval: bool = False


class CapabilityRequirements(BaseModel):
    local_model: str
    storage: str
    permissions: list[str]
    downloads: str


class CapabilityInfo(BaseModel):
    id: str
    name: str
    version: str
    author: str
    description: str
    entrypoint: str
    async_task: bool = True
    input_schema: dict[str, Any]
    requirements: CapabilityRequirements


class HealthResponse(BaseModel):
    status: Literal["ok"]
    agent_mode: str
    llm_configured: bool
    model: str | None = None
    tool_count: int
    feishu_status: Literal[
        "disabled", "starting", "connected", "reconnecting", "error"
    ] = "disabled"


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


class FeishuRuntimePublic(BaseModel):
    status: Literal[
        "disabled", "starting", "connected", "reconnecting", "error"
    ]
    last_error: str | None = None
    reconnect_attempts: int = 0
    last_connected_at: datetime | None = None


class FeishuSettingsUpdate(BaseModel):
    enabled: bool = False
    app_id: str = Field(default="", max_length=100)
    app_secret: SecretStr | None = None
    domain: Literal["feishu", "lark"] = "feishu"
    allowed_open_ids: list[str] = Field(default_factory=list, max_length=100)
    allow_group_mentions: bool = False


class FeishuSettingsPublic(BaseModel):
    enabled: bool
    app_id: str
    domain: Literal["feishu", "lark"]
    allowed_open_ids: list[str]
    allow_group_mentions: bool
    has_app_secret: bool
    masked_app_secret: str | None = None
    secret_storage: str
    runtime: FeishuRuntimePublic


class FeishuConnectionTestResponse(BaseModel):
    ok: Literal[True]
    app_id: str
    domain: Literal["feishu", "lark"]
    latency_ms: int
    message: str


class IdentityBindingPublic(BaseModel):
    id: str
    provider: Literal["feishu"]
    app_id: str
    external_id: str
    workspace_id: str
    workspace_name: str
    device_id: str
    device_name: str
    status: Literal["active", "suspended", "revoked"]
    created_at: datetime
    updated_at: datetime


class IdentityBindingListResponse(BaseModel):
    bindings: list[IdentityBindingPublic]


class IdentityBindingCreateRequest(BaseModel):
    app_id: str | None = Field(default=None, max_length=100)
    open_id: str = Field(min_length=1, max_length=160)
    workspace_name: str | None = Field(default=None, max_length=80)


class IdentityBindingStatusUpdate(BaseModel):
    status: Literal["active", "suspended", "revoked"]


class IdentityWorkspaceRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ChannelMessagePublic(BaseModel):
    id: int
    platform: Literal["feishu"]
    chat_id: str
    sender_id: str | None = None
    direction: Literal["inbound", "outbound", "system"]
    kind: Literal["text", "markdown", "image", "card", "status"]
    content: str
    media_url: str | None = None
    created_at: datetime


class ChannelMessageListResponse(BaseModel):
    messages: list[ChannelMessagePublic]


AssetStatus = Literal["processing", "ready", "failed"]
JobStatus = Literal["queued", "running", "completed", "failed"]


class AssetPublic(BaseModel):
    id: str
    kind: Literal["spatial_scene", "photo_style_transfer"]
    name: str
    status: AssetStatus
    width: int | None = None
    height: int | None = None
    source_url: str
    preview_url: str | None = None
    depth_url: str | None = None
    background_url: str | None = None
    foreground_url: str | None = None
    foreground_mask_url: str | None = None
    manifest_url: str | None = None
    result_url: str | None = None
    style_reference_urls: list[str] = Field(default_factory=list)
    model_name: str | None = None
    segmentation_model: str | None = None
    segmentation_quality: float | None = Field(default=None, ge=0, le=1)
    segmentation_warnings: list[str] = Field(default_factory=list)
    recommended_strength: float | None = Field(default=None, ge=0, le=1)
    provider_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class JobPublic(BaseModel):
    id: str
    kind: Literal["spatial_scene", "photo_style_transfer"]
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


class PhotoStyleCreateResponse(BaseModel):
    asset: AssetPublic
    job: JobPublic


class PhotoStyleProviderStatus(BaseModel):
    name: str
    model_name: str
    ready: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ViewerLinkPublic(BaseModel):
    asset_id: str
    url: str
    expires_in_seconds: int = Field(ge=300)


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
