from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

from .context import ContextBuilder
from .models import AgentRunResponse, ToolCall, TraceStep
from .memory import MemoryPolicyError, SQLiteMemoryStore
from .tools import ToolExecutionContext, ToolRegistry


LOGGER = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _task_payload(message: str) -> str:
    for separator in ("：", ":"):
        if separator in message:
            payload = message.split(separator, maxsplit=1)[1].strip()
            if payload:
                return payload
    return message.strip()


_STYLE_TASK_TERMS = (
    "图片风格",
    "图片个性化",
    "风格化",
    "风格迁移",
    "参考图",
    "style transfer",
)
_SPATIAL_TASK_TERMS = (
    "空间照片",
    "空间图片",
    "空间场景",
    "可动视角",
    "可拖动视角",
    "2.5d",
    "视差",
)
_VISUAL_FOLLOWUP_TERMS = (
    "图中",
    "图片中",
    "照片中",
    "画面中",
    "这张图",
    "这张图片",
    "这幅图",
    "这个图片",
    "看图",
    "识别",
    "描述",
    "分析",
    "是什么",
    "有什么",
    "在哪里",
    "文字",
    "ocr",
    "它",
)
_ORDINAL_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
}


def _requested_attachment_index(message: str, count: int) -> int | None:
    match = re.search(r"第\s*([一二三四1-4])\s*张", message)
    if match is None:
        return None
    index = _ORDINAL_INDEX[match.group(1)]
    return index if index < count else None


def _route_image_attachments(
    message: str,
    attachments: list[dict[str, Any]],
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    str | None,
    Literal["vision", "spatial", "style"],
]:
    """Separate visual understanding from explicit image-generation tools."""

    lowered = message.casefold()
    style_requested = any(term in lowered for term in _STYLE_TASK_TERMS)
    spatial_requested = any(term in lowered for term in _SPATIAL_TASK_TERMS)
    count = len(attachments)

    if style_requested:
        if count < 2:
            return (
                None,
                [],
                "已收到 1 张图片。图片风格化还需要至少 1 张参考图；"
                "请继续添加附件，或改为说明要生成空间照片。",
                "style",
            )
        content_index = _requested_attachment_index(message, count) or 0
        content = attachments[content_index]
        styles = [
            item
            for index, item in enumerate(attachments)
            if index != content_index
        ][:3]
        return content, styles, None, "style"

    if spatial_requested:
        if count == 1:
            return attachments[0], [], None, "spatial"
        requested_index = _requested_attachment_index(message, count)
        if requested_index is not None:
            return attachments[requested_index], [], None, "spatial"
        return (
            None,
            [],
            f"已收到 {count} 张图片。空间照片一次使用一张内容图，"
            "请说明使用第几张图片，例如“用第一张生成空间照片”。",
            "spatial",
        )

    return None, [], None, "vision"


def _looks_like_visual_followup(message: str) -> bool:
    lowered = message.casefold()
    return any(term in lowered for term in _VISUAL_FOLLOWUP_TERMS)


def _looks_like_image_generation_followup(message: str) -> bool:
    lowered = message.casefold()
    return any(
        term in lowered
        for term in (*_STYLE_TASK_TERMS, *_SPATIAL_TASK_TERMS)
    )


@dataclass
class PlanningResult:
    tool_calls: list[ToolCall]
    direct_answer: str | None = None
    provider_context: dict[str, Any] | None = None


@dataclass
class ToolObservation:
    call: ToolCall
    output: dict[str, Any]


class Planner(Protocol):
    mode: str
    is_llm: bool
    model_name: str | None

    def plan(
        self, message: str, tool_schemas: list[dict[str, Any]]
    ) -> PlanningResult: ...

    def compose_answer(
        self,
        message: str,
        plan: PlanningResult,
        observations: list[ToolObservation],
    ) -> str: ...

    def continue_plan(
        self,
        message: str,
        plan: PlanningResult,
        observations: list[ToolObservation],
        tool_schemas: list[dict[str, Any]],
    ) -> PlanningResult: ...


class DemoPlanner:
    """A deterministic planner used before a real LLM is configured."""

    mode = "demo-rule-planner"
    is_llm = False
    model_name = None

    def plan(
        self, message: str, _: list[dict[str, Any]]
    ) -> PlanningResult:
        lowered = message.lower()
        payload = _task_payload(message)
        source_match = re.search(
            r"source_image_id=([A-Za-z0-9-]{1,100})",
            message,
        )
        style_matches = re.findall(
            r"style_image_id=([A-Za-z0-9-]{1,100})",
            message,
        )
        flux_dataset_match = re.search(
            r"dataset_id\s*=\s*([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
            message,
            flags=re.IGNORECASE,
        )

        if flux_dataset_match and any(
            keyword in lowered
            for keyword in ("flux-gs", "fluxgs", "3dgs", "3d 高斯", "3d高斯")
        ):
            calls = [
                ToolCall(
                    name="create_flux_gs_demo",
                    arguments={"dataset_id": flux_dataset_match.group(1)},
                )
            ]
        elif source_match and style_matches and any(
            keyword in lowered
            for keyword in ("图片风格", "风格化", "风格迁移", "style transfer")
        ):
            calls = [
                ToolCall(
                    name="create_photo_style_transfer",
                    arguments={
                        "content_image_id": source_match.group(1),
                        "style_image_ids": style_matches,
                    },
                )
            ]
        elif source_match and any(
            keyword in lowered
            for keyword in ("空间照片", "空间场景", "可动视角", "2d", "视角")
        ):
            calls = [
                ToolCall(
                    name="create_spatial_scene",
                    arguments={"source_image_id": source_match.group(1)},
                )
            ]
        elif any(keyword in lowered for keyword in ("分析", "analyze")):
            calls = [
                ToolCall(name="text_stats", arguments={"text": payload}),
                ToolCall(
                    name="extract_keywords",
                    arguments={"text": payload, "limit": 5},
                ),
            ]
        elif any(keyword in lowered for keyword in ("关键词", "keyword")):
            calls = [
                ToolCall(
                    name="extract_keywords",
                    arguments={"text": payload, "limit": 5},
                )
            ]
        elif any(keyword in lowered for keyword in ("统计", "字数", "字符", "count")):
            calls = [ToolCall(name="text_stats", arguments={"text": payload})]
        elif any(keyword in lowered for keyword in ("几点", "时间", "time")):
            calls = [ToolCall(name="current_time")]
        elif any(
            keyword in lowered
            for keyword in ("个人资产", "资产库", "我的资产", "空间照片列表")
        ):
            calls = [ToolCall(name="list_personal_assets")]
        else:
            calls = [ToolCall(name="list_capabilities")]
        return PlanningResult(tool_calls=calls)

    def plan_with_context(
        self,
        planning_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
    ) -> PlanningResult:
        current = planning_context.get("current_user_message", {})
        message = (
            str(current.get("content", ""))
            if isinstance(current, dict)
            else str(current)
        )
        attachments = planning_context.get("attachments", [])
        if isinstance(attachments, list):
            for attachment in attachments:
                trusted = attachment.get("trusted_system", {})
                if not isinstance(trusted, dict):
                    continue
                source_image_id = trusted.get("source_image_id")
                role = trusted.get("attachment_role")
                if isinstance(source_image_id, str) and source_image_id:
                    key = "style_image_id" if role == "style" else "source_image_id"
                    message = f"{message}\n{key}={source_image_id}"
        return self.plan(message, tool_schemas)

    def compose_answer(
        self,
        _: str,
        __: PlanningResult,
        observations: list[ToolObservation],
    ) -> str:
        sections: list[str] = []
        for observation in observations:
            name = observation.call.name
            output = observation.output
            if output.get("error"):
                sections.append(
                    f"工具 {name} 执行失败：{output['error']}"
                )
                continue
            if name == "text_stats":
                sections.append(
                    "文本统计："
                    f"共 {output['characters']} 个字符，"
                    f"其中中文字符 {output['chinese_characters']} 个，"
                    f"英文/数字词 {output['latin_words']} 个，"
                    f"共 {output['lines']} 行。"
                )
            elif name == "extract_keywords":
                keywords = "、".join(str(item) for item in output["keywords"])
                sections.append(f"演示关键词：{keywords}。")
            elif name == "current_time":
                sections.append(
                    f"当前时间是 {output['display']}（{output['timezone']}）。"
                )
            elif name == "list_personal_assets":
                assets = output["assets"]
                if assets:
                    items = "\n".join(
                        f"- {item['name']}（{item['status']}）"
                        for item in assets
                    )
                    sections.append("你的本地个人资产：\n" + items)
                else:
                    sections.append("个人资产库还是空的，可以先生成一张空间照片。")
            elif name == "create_spatial_scene":
                sections.append(
                    "空间照片任务已创建，正在本机进行深度估计和分层处理。"
                    "完成后会自动打开可交互视角。"
                )
            elif name == "create_photo_style_transfer":
                sections.append(
                    "图片个性化任务已创建，正在按参考图迁移视觉风格。"
                    "完成后会写入本地个人资产库。"
                )
            elif name == "create_flux_gs_demo":
                sections.append(
                    "Flux-GS 训练与 Web 发布任务已提交到独立 GPU 服务。"
                    "完成后可通过任务状态工具取得 3D WebGL 预览链接。"
                )
            elif name == "list_capabilities":
                examples = "\n".join(f"- {item}" for item in output["examples"])
                sections.append("当前是无模型演示模式，可以尝试：\n" + examples)
        return "\n\n".join(sections)

    def continue_plan(
        self,
        message: str,
        plan: PlanningResult,
        observations: list[ToolObservation],
        _: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[],
            direct_answer=self.compose_answer(message, plan, observations),
        )


class JsonlTraceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, run: AgentRunResponse) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(run.model_dump(mode="json"), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(line + "\n")
        os.chmod(self.path, 0o600)


class AgentRunError(RuntimeError):
    """Raised when a persisted Agent Run cannot be accessed or resumed."""


class AgentRunIsolationError(AgentRunError):
    """Raised when a user attempts to resume another owner's run."""


class AgentRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        trace_store: JsonlTraceStore,
        max_steps: int = 4,
        memory_store: SQLiteMemoryStore | None = None,
        checkpoint_path: Path | None = None,
        context_builder: ContextBuilder | None = None,
        image_loader: Callable[[str, str], tuple[str, bytes]] | None = None,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.trace_store = trace_store
        self.max_steps = max_steps
        self._owns_memory_store = memory_store is None
        self.context_builder = context_builder or ContextBuilder(
            token_counter=(
                memory_store.token_counter if memory_store is not None else None
            )
        )
        self.memory_store = memory_store or SQLiteMemoryStore(
            trace_store.path.parent / "agent_memory.sqlite3",
            token_counter=self.context_builder.token_counter,
        )
        if context_builder is not None and memory_store is not None:
            self.memory_store.set_token_counter(context_builder.token_counter)
        self.image_loader = image_loader
        from .orchestration import LangGraphOrchestrator

        self.orchestrator = LangGraphOrchestrator(
            planner=planner,
            registry=registry,
            checkpoint_path=(
                checkpoint_path
                or trace_store.path.parent / "agent_checkpoints.sqlite3"
            ),
            max_steps=max_steps,
        )
        self.reconcile_incomplete_runs()

    def run(
        self,
        message: str,
        *,
        attachment_contexts: list[dict[str, Any]] | None = None,
        source_image_context: dict[str, Any] | None = None,
        style_image_contexts: list[dict[str, Any]] | None = None,
        owner_id: str = "local",
        thread_id: str = "local:default",
        channel: str = "web",
        project_id: str | None = None,
    ) -> AgentRunResponse:
        run_started = time.perf_counter()
        owner_id = self.memory_store.resolve_verified_subject_id(owner_id)
        project_id = (
            project_id.strip()[:160]
            if isinstance(project_id, str) and project_id.strip()
            else None
        )
        summary_provider = (
            self.planner
            if callable(getattr(self.planner, "summarize_session", None))
            else None
        )
        self.memory_store.set_summary_provider(summary_provider)
        execution_context = ToolExecutionContext(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
        )
        conversation = self.memory_store.context(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
            query=message,
            raw_history_token_budget=(
                self.context_builder.config.recent_history_tokens
            ),
            summary_token_budget=(
                self.context_builder.config.session_summary_tokens
            ),
        )
        if self._is_memory_command(message):
            return self._run_memory_command(
                message=message,
                context=execution_context,
                started_at=run_started,
            )

        generic_attachments = list(attachment_contexts or [])
        reused_recent_attachments = False
        if (
            not generic_attachments
            and source_image_context is None
            and not style_image_contexts
            and conversation.recent_attachments
            and _looks_like_image_generation_followup(message)
        ):
            generic_attachments = list(conversation.recent_attachments)
            reused_recent_attachments = True
        vision_attachments: list[dict[str, Any]] = []
        vision_inputs: list[dict[str, str]] = []
        attachment_routing: dict[str, Any] | None = None
        if generic_attachments:
            (
                source_image_context,
                style_image_contexts,
                clarification,
                attachment_intent,
            ) = _route_image_attachments(message, generic_attachments)
            attachment_ids = [
                str(item.get("id", ""))
                for item in generic_attachments
                if item.get("id")
            ]
            if attachment_intent == "vision" and (
                not self.planner.is_llm or self.image_loader is None
            ):
                clarification = (
                    f"已收到 {len(generic_attachments)} 张图片，但当前没有启用可接收"
                    "图片输入的真实模型。请先在模型设置中启用视觉模型；也可以明确"
                    "要求生成空间照片，或在至少两张图片时进行风格化。"
                )
            if clarification is not None:
                clarification_action = (
                    "clarification_required_followup"
                    if reused_recent_attachments
                    else "clarification_required"
                )
                return self._direct_response(
                    message=message,
                    answer=clarification,
                    context=execution_context,
                    started_at=run_started,
                    mode="deterministic-attachment-router",
                    label="确认附件用途",
                    detail="Agent 尚未执行图片工具，附件角色需要用户补充说明。",
                    step_output={
                        "attachment_action": clarification_action,
                        "release_image_ids": (
                            [] if reused_recent_attachments else attachment_ids
                        ),
                    },
                    user_metadata={
                        "attachments": [
                            {
                                "name": item.get("original_name"),
                                "source_image_id": item.get("id"),
                                "width": item.get("width"),
                                "height": item.get("height"),
                            }
                            for item in generic_attachments
                        ]
                    },
                )
            if attachment_intent == "vision":
                vision_attachments = generic_attachments
                attachment_routing = {
                    "attachment_action": "vision",
                    "vision_image_ids": attachment_ids,
                    # Keep recent vision inputs briefly for preview and follow-up.
                    # The staged-image cleanup policy removes stale files.
                    "release_image_ids": [],
                }
            else:
                used_ids = {
                    str(item.get("id", ""))
                    for item in [
                        source_image_context,
                        *(style_image_contexts or []),
                    ]
                    if item and item.get("id")
                }
                attachment_routing = {
                    "attachment_action": (
                        "assigned_followup"
                        if reused_recent_attachments
                        else "assigned"
                    ),
                    "content_image_id": (
                        source_image_context.get("id")
                        if source_image_context
                        else None
                    ),
                    "style_image_ids": [
                        item.get("id") for item in (style_image_contexts or [])
                    ],
                    "release_image_ids": [
                        item
                        for item in attachment_ids
                        if not reused_recent_attachments and item not in used_ids
                    ],
                }
        elif (
            self.planner.is_llm
            and self.image_loader is not None
            and conversation.recent_attachments
            and _looks_like_visual_followup(message)
        ):
            vision_attachments = list(conversation.recent_attachments)
            attachment_routing = {
                "attachment_action": "vision_followup",
                "vision_image_ids": [
                    item.get("id") for item in vision_attachments if item.get("id")
                ],
                "release_image_ids": [],
            }

        if vision_attachments:
            vision_inputs = self._load_vision_inputs(
                vision_attachments,
                owner_id=owner_id,
            )
            if not vision_inputs:
                if generic_attachments:
                    raise AgentRunError("图片视觉输入无法读取，请重新上传后重试。")
                vision_attachments = []
                if attachment_routing is not None:
                    attachment_routing = None

        context_builder = ContextBuilder(
            self.context_builder.config,
            system_prompt=str(getattr(self.planner, "system_prompt", "")),
            token_counter=self.context_builder.token_counter,
        )
        all_tool_schemas = self.registry.openai_schemas()
        selected_tool_schemas = (
            []
            if vision_inputs
            else context_builder.select_tools(
                user_message=message,
                tool_schemas=all_tool_schemas,
                attachment_present=bool(
                    source_image_context or style_image_contexts
                ),
            )
        )
        built_context = context_builder.build(
            current_user_message=message,
            conversation_messages=conversation.messages,
            memories=[
                item.to_context_dict() for item in conversation.memory_items
            ],
            session_summary=conversation.session_summary,
            open_loops=conversation.open_loops,
            decisions=conversation.decisions,
            completed_actions=conversation.completed_actions,
            active_assumptions=conversation.active_assumptions,
            artifact_refs=conversation.artifact_refs,
            blockers=conversation.blockers,
            next_goal=conversation.next_goal,
            attachments=[
                *[
                    {**item, "attachment_role": "vision"}
                    for item in vision_attachments
                ],
                *(
                    [{**source_image_context, "attachment_role": "content"}]
                    if source_image_context
                    else []
                ),
                *[
                    {**item, "attachment_role": "style"}
                    for item in (style_image_contexts or [])
                ],
            ],
            selected_tool_schemas=selected_tool_schemas,
        )

        run_id = str(uuid4())
        initial_user_metadata: dict[str, Any] = {}
        if generic_attachments:
            initial_user_metadata["attachments"] = [
                {
                    "name": item.get("original_name"),
                    "source_image_id": item.get("id"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                }
                for item in generic_attachments
            ]
        elif source_image_context:
            initial_user_metadata["attachment"] = {
                "name": source_image_context["original_name"],
                "source_image_id": source_image_context["id"],
                "width": source_image_context["width"],
                "height": source_image_context["height"],
            }
        if style_image_contexts and not generic_attachments:
            initial_user_metadata["style_attachments"] = [
                {
                    "name": item["original_name"],
                    "source_image_id": item["id"],
                    "width": item["width"],
                    "height": item["height"],
                }
                for item in style_image_contexts
            ]
        checkpoint_thread_id = self.orchestrator.checkpoint_thread_id(
            execution_context,
            run_id,
        )
        try:
            self.memory_store.create_run(
                run_id=run_id,
                owner_id=owner_id,
                thread_id=thread_id,
                channel=channel,
                message=message,
                project_id=project_id,
                checkpoint_thread_id=checkpoint_thread_id,
                persist_user_message=True,
                user_metadata=initial_user_metadata,
            )
        except ValueError as exc:
            raise AgentRunError(str(exc)) from exc
        selected_memory_ids = set(built_context.retrieved_memory_ids)
        self.memory_store.record_memory_usage(
            owner_id=owner_id,
            run_id=run_id,
            memories=[
                item
                for item in conversation.memory_items
                if item.id in selected_memory_ids
            ],
        )

        self.orchestrator.planner = self.planner
        try:
            graph_state = self.orchestrator.invoke(
                message=message,
                context=execution_context,
                run_id=run_id,
                planner_context=built_context.to_planner_context(),
                selected_tool_names=list(built_context.selected_tool_names),
                vision_inputs=vision_inputs,
            )
        except Exception:
            self.reconcile_incomplete_runs()
            raise
        response = self._response_from_state(
            graph_state,
            started_at=run_started,
        )
        if attachment_routing is not None and response.steps:
            target_step = response.steps[-1]
            target_step.output = {
                **(target_step.output or {}),
                **attachment_routing,
            }
        durable_asset_id: str | None = None
        durable_asset_kind: str | None = None
        for step in response.steps:
            output = step.output or {}
            if isinstance(output.get("asset_id"), str):
                durable_asset_id = str(output["asset_id"])
                durable_asset_kind = (
                    str(output["kind"])
                    if isinstance(output.get("kind"), str)
                    else None
                )
                break

        user_metadata: dict[str, Any] = {}
        if generic_attachments:
            style_positions = {
                str(item.get("id", "")): index
                for index, item in enumerate(
                    style_image_contexts or [],
                    start=1,
                )
            }
            content_id = (
                str(source_image_context.get("id", ""))
                if source_image_context
                else ""
            )
            user_metadata["attachments"] = [
                {
                    "name": item.get("original_name"),
                    "source_image_id": item.get("id"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    **(
                        {
                            "preview_url": (
                                f"/api/source-images/{item.get('id')}/content"
                            )
                        }
                        if attachment_routing
                        and attachment_routing.get("attachment_action") == "vision"
                        else {}
                    ),
                    **(
                        {
                            "preview_url": (
                                f"/api/assets/{durable_asset_id}/files/source.webp"
                            )
                        }
                        if durable_asset_id
                        and str(item.get("id", "")) == content_id
                        else {}
                    ),
                    **(
                        {
                            "preview_url": (
                                f"/api/assets/{durable_asset_id}/files/"
                                f"style-{style_positions[str(item.get('id', ''))]}.webp"
                            )
                        }
                        if durable_asset_id
                        and durable_asset_kind == "photo_style_transfer"
                        and str(item.get("id", "")) in style_positions
                        else {}
                    ),
                }
                for item in generic_attachments
            ]
        elif source_image_context:
            user_metadata["attachment"] = {
                "name": source_image_context["original_name"],
                "source_image_id": source_image_context["id"],
                "width": source_image_context["width"],
                "height": source_image_context["height"],
                **(
                    {
                        "preview_url": (
                            f"/api/assets/{durable_asset_id}/files/source.webp"
                        )
                    }
                    if durable_asset_id
                    else {}
                ),
            }
        if style_image_contexts and not generic_attachments:
            user_metadata["style_attachments"] = [
                {
                    "name": item["original_name"],
                    "source_image_id": item["id"],
                    "width": item["width"],
                    "height": item["height"],
                    **(
                        {
                            "preview_url": (
                                f"/api/assets/{durable_asset_id}/files/"
                                f"style-{index}.webp"
                            )
                        }
                        if durable_asset_id
                        and durable_asset_kind == "photo_style_transfer"
                        else {}
                    ),
                }
                for index, item in enumerate(style_image_contexts, start=1)
            ]
        self.memory_store.update_run_user_message_metadata(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            metadata=user_metadata,
        )
        self._persist_response(
            response,
            owner_id=owner_id,
            thread_id=thread_id,
        )
        return response

    def reconcile_incomplete_runs(self) -> dict[str, int]:
        """Classify interrupted runs from durable checkpoints without replaying work."""

        records: dict[str, Any] = {}
        for status in ("running", "resuming"):
            for record in self.memory_store.list_runs(status=status, limit=None):
                records[record.run_id] = record
        counts = {
            "scanned": len(records),
            "recoverable": 0,
            "waiting_approval": 0,
            "terminal": 0,
            "needs_attention": 0,
        }
        for record in records.values():
            context = ToolExecutionContext(
                owner_id=record.owner_id,
                thread_id=record.thread_id,
                channel=record.channel,
                project_id=record.project_id,
            )
            checkpoint_thread_id = (
                record.checkpoint_thread_id or record.thread_id
            )
            try:
                checkpoint = self.orchestrator.inspect_checkpoint(
                    run_id=record.run_id,
                    context=context,
                    checkpoint_thread_id=checkpoint_thread_id,
                )
            except Exception as exc:
                LOGGER.warning(
                    "Unable to inspect checkpoint for interrupted run %s (%s).",
                    record.run_id,
                    type(exc).__name__,
                )
                checkpoint = {"exists": False}

            state = checkpoint.get("state")
            if checkpoint.get("terminal") and isinstance(state, dict):
                response = self._response_from_state(state, started_at=time.perf_counter())
                self._persist_response(
                    response,
                    owner_id=record.owner_id,
                    thread_id=record.thread_id,
                )
                counts["terminal"] += 1
                continue
            if checkpoint.get("waiting_approval") and isinstance(state, dict):
                waiting_state = dict(state)
                waiting_state["status"] = "waiting_approval"
                response = self._response_from_state(
                    waiting_state,
                    started_at=time.perf_counter(),
                )
                self._persist_response(
                    response,
                    owner_id=record.owner_id,
                    thread_id=record.thread_id,
                )
                counts["waiting_approval"] += 1
                continue
            if checkpoint.get("resumable") and isinstance(state, dict):
                response = AgentRunResponse(
                    run_id=record.run_id,
                    status="recoverable",
                    mode=self.planner.mode,
                    answer=(
                        "任务执行曾被中断，已找到一致的执行 Checkpoint，"
                        "可以从上次安全边界继续。"
                    ),
                    steps=[
                        TraceStep.model_validate(item)
                        for item in state.get("steps", [])
                    ],
                    total_duration_ms=0,
                )
                self.memory_store.update_run(
                    run_id=record.run_id,
                    status=response.status,
                    approval=None,
                    response=response.model_dump(mode="json"),
                )
                counts["recoverable"] += 1
                continue

            response = AgentRunResponse(
                run_id=record.run_id,
                status="needs_attention",
                mode=self.planner.mode,
                answer=(
                    "任务执行曾被中断，但没有找到可验证的一致 Checkpoint；"
                    "系统不会猜测或重复执行可能产生副作用的步骤。"
                ),
                steps=[],
                total_duration_ms=0,
            )
            self.memory_store.update_run(
                run_id=record.run_id,
                status=response.status,
                approval=None,
                response=response.model_dump(mode="json"),
            )
            counts["needs_attention"] += 1
        return counts

    def continue_run(
        self,
        run_id: str,
        *,
        requester_owner_id: str | None = None,
        is_admin: bool = False,
    ) -> AgentRunResponse:
        record = self.memory_store.get_run(run_id)
        if record is None:
            raise AgentRunError("找不到这个 Agent Run。")
        if not is_admin and record.owner_id != requester_owner_id:
            raise AgentRunIsolationError("不能继续其他用户的 Agent Run。")
        if record.status in {"completed", "failed", "waiting_approval"}:
            if record.response is not None:
                return AgentRunResponse.model_validate(record.response)
            raise AgentRunError("这个 Agent Run 当前不能直接继续。")
        if record.status != "recoverable":
            raise AgentRunError("这个 Agent Run 没有可安全继续的 Checkpoint。")

        owner_filter = None if is_admin else record.owner_id
        if not self.memory_store.begin_continue(
            run_id=run_id,
            owner_id=owner_filter,
        ):
            latest = self.memory_store.get_run(run_id)
            if latest is not None and latest.response is not None:
                return AgentRunResponse.model_validate(latest.response)
            raise AgentRunError("这个 Agent Run 正在由另一个请求恢复。")

        context = ToolExecutionContext(
            owner_id=record.owner_id,
            thread_id=record.thread_id,
            channel=record.channel,
            project_id=record.project_id,
        )
        started = time.perf_counter()
        self.orchestrator.planner = self.planner
        try:
            graph_state = self.orchestrator.continue_from_checkpoint(
                run_id=run_id,
                context=context,
                checkpoint_thread_id=(
                    record.checkpoint_thread_id or record.thread_id
                ),
            )
            response = self._response_from_state(
                graph_state,
                started_at=started,
            )
            self._persist_response(
                response,
                owner_id=record.owner_id,
                thread_id=record.thread_id,
            )
            return response
        except Exception:
            recovery_response = record.response or AgentRunResponse(
                run_id=run_id,
                status="recoverable",
                mode=self.planner.mode,
                answer="恢复尝试失败，原 Checkpoint 保持不变，可以稍后重试或明确终止。",
                steps=[],
                total_duration_ms=0,
            ).model_dump(mode="json")
            self.memory_store.update_run(
                run_id=run_id,
                status="recoverable",
                approval=None,
                response=recovery_response,
            )
            raise

    def abandon_interrupted_run(
        self,
        run_id: str,
        *,
        requester_owner_id: str | None = None,
        is_admin: bool = False,
    ) -> AgentRunResponse:
        """Close an interrupted run without replaying uncertain side effects."""

        record = self.memory_store.get_run(run_id)
        if record is None:
            raise AgentRunError("找不到这个 Agent Run。")
        if not is_admin and record.owner_id != requester_owner_id:
            raise AgentRunIsolationError("不能处置其他用户的 Agent Run。")
        if record.status in {"completed", "failed", "waiting_approval"}:
            if record.response is not None:
                return AgentRunResponse.model_validate(record.response)
            raise AgentRunError("这个 Agent Run 当前不能标记为失败。")
        if record.status not in {"recoverable", "needs_attention"}:
            raise AgentRunError("这个 Agent Run 当前正在执行，不能直接终止。")

        owner_filter = None if is_admin else record.owner_id
        if not self.memory_store.begin_abandon(
            run_id=run_id,
            owner_id=owner_filter,
        ):
            latest = self.memory_store.get_run(run_id)
            if latest is not None and latest.response is not None:
                return AgentRunResponse.model_validate(latest.response)
            raise AgentRunError("这个 Agent Run 已被另一个请求处置。")

        previous_steps = []
        if record.response is not None:
            previous_steps = [
                TraceStep.model_validate(item)
                for item in record.response.get("steps", [])
            ]
        response = AgentRunResponse(
            run_id=run_id,
            status="failed",
            mode=(
                str(record.response.get("mode"))
                if record.response is not None and record.response.get("mode")
                else self.planner.mode
            ),
            answer=(
                "该中断任务已被明确终止。系统没有重放未确认的步骤，"
                "你现在可以在原会话发起新任务。"
            ),
            steps=previous_steps,
            total_duration_ms=0,
        )
        self._persist_response(
            response,
            owner_id=record.owner_id,
            thread_id=record.thread_id,
        )
        return response

    def _load_vision_inputs(
        self,
        attachments: list[dict[str, Any]],
        *,
        owner_id: str,
    ) -> list[dict[str, str]]:
        if self.image_loader is None:
            return []
        inputs: list[dict[str, str]] = []
        for item in attachments[:4]:
            source_image_id = item.get("id")
            if not isinstance(source_image_id, str) or not source_image_id:
                continue
            try:
                media_type, image_bytes = self.image_loader(
                    source_image_id,
                    owner_id,
                )
            except (OSError, ValueError):
                continue
            if media_type not in {"image/jpeg", "image/png", "image/webp"}:
                continue
            inputs.append(
                {
                    "source_image_id": source_image_id,
                    "data_url": (
                        f"data:{media_type};base64,"
                        + base64.b64encode(image_bytes).decode("ascii")
                    ),
                }
            )
        return inputs

    def get_run(
        self,
        run_id: str,
        *,
        requester_owner_id: str | None = None,
        is_admin: bool = False,
    ) -> AgentRunResponse:
        record = self.memory_store.get_run(run_id)
        if record is None or record.response is None:
            raise AgentRunError("找不到这个 Agent Run。")
        if not is_admin and record.owner_id != requester_owner_id:
            raise AgentRunIsolationError("不能访问其他用户的 Agent Run。")
        return AgentRunResponse.model_validate(record.response)

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[AgentRunResponse]:
        return [
            AgentRunResponse.model_validate(record.response)
            for record in self.memory_store.list_runs(
                status=status,
                limit=limit,
            )
            if record.response is not None
        ]

    def resume_run(
        self,
        run_id: str,
        *,
        approved: bool,
        requester_owner_id: str | None = None,
        is_admin: bool = False,
    ) -> AgentRunResponse:
        record = self.memory_store.get_run(run_id)
        if record is None:
            raise AgentRunError("找不到这个 Agent Run。")
        if not is_admin and record.owner_id != requester_owner_id:
            raise AgentRunIsolationError("不能审批其他用户的 Agent Run。")
        if record.status != "waiting_approval":
            if record.response is not None and record.status in {
                "completed",
                "failed",
            }:
                return AgentRunResponse.model_validate(record.response)
            raise AgentRunError("这个 Agent Run 当前不在等待审批。")

        owner_filter = None if is_admin else record.owner_id
        if not self.memory_store.begin_resume(
            run_id=run_id,
            owner_id=owner_filter,
        ):
            latest = self.memory_store.get_run(run_id)
            if (
                latest is not None
                and latest.response is not None
                and latest.status in {"completed", "failed"}
            ):
                return AgentRunResponse.model_validate(latest.response)
            raise AgentRunError("这个审批正在由另一个请求处理。")

        context = ToolExecutionContext(
            owner_id=record.owner_id,
            thread_id=record.thread_id,
            channel=record.channel,
            project_id=record.project_id,
        )
        started = time.perf_counter()
        self.orchestrator.planner = self.planner
        try:
            graph_state = self.orchestrator.resume(
                run_id=run_id,
                context=context,
                approved=approved,
                actor_id=(
                    "root"
                    if is_admin
                    else requester_owner_id or record.owner_id
                ),
                checkpoint_thread_id=(
                    record.checkpoint_thread_id or record.thread_id
                ),
            )
            response = self._response_from_state(
                graph_state,
                started_at=started,
            )
            self._persist_response(
                response,
                owner_id=record.owner_id,
                thread_id=record.thread_id,
            )
            return response
        except Exception:
            if record.response is not None:
                self.memory_store.update_run(
                    run_id=run_id,
                    status="waiting_approval",
                    approval=record.approval,
                    response=record.response,
                )
            raise

    def _response_from_state(
        self,
        graph_state: dict[str, Any],
        *,
        started_at: float,
    ) -> AgentRunResponse:
        status = graph_state["status"]
        approval = graph_state.get("approval")
        answer = str(graph_state.get("answer", "") or "").strip()
        if status == "waiting_approval":
            tool = (
                str(approval.get("tool", "高风险工具"))
                if isinstance(approval, dict)
                else "高风险工具"
            )
            answer = (
                f"工具 {tool} 需要批准后才能执行。\n"
                f"Run ID：{graph_state['run_id']}\n"
                "可在 Web Root 控制台批准或拒绝；飞书用户也可以回复"
                f"“批准 {graph_state['run_id']}”或"
                f"“拒绝 {graph_state['run_id']}”。"
            )
        return AgentRunResponse(
            run_id=graph_state["run_id"],
            status=status,
            mode=self.planner.mode,
            answer=answer,
            steps=[
                TraceStep.model_validate(item)
                for item in graph_state["steps"]
            ],
            total_duration_ms=_elapsed_ms(started_at),
            approval=approval if status == "waiting_approval" else None,
        )

    def _persist_response(
        self,
        response: AgentRunResponse,
        *,
        owner_id: str,
        thread_id: str,
    ) -> None:
        response_data = response.model_dump(mode="json")
        self.memory_store.update_run(
            run_id=response.run_id,
            status=response.status,
            approval=response.approval,
            response=response_data,
        )
        if response.status in {"completed", "failed"}:
            self.memory_store.complete_memory_usage(
                run_id=response.run_id,
                outcome=response.status,
            )
        self.trace_store.append(response)
        record = self.memory_store.get_run(response.run_id)
        assistant_metadata: dict[str, Any] = {"run": response_data}
        for step in response.steps:
            output = step.output or {}
            asset_id = output.get("asset_id")
            if isinstance(asset_id, str):
                assistant_metadata["asset_id"] = asset_id
                asset_kind = output.get("kind")
                if isinstance(asset_kind, str):
                    assistant_metadata["asset_kind"] = asset_kind
                break
        self.memory_store.upsert_assistant_run_message(
            owner_id=owner_id,
            thread_id=thread_id,
            project_id=record.project_id if record is not None else None,
            run_id=response.run_id,
            content=response.answer,
            metadata=assistant_metadata,
        )
        if record is not None and response.status in {"completed", "failed"}:
            self.memory_store.record_episode(
                owner_id=owner_id,
                thread_id=thread_id,
                run_id=response.run_id,
                task=record.message,
                answer=response.answer,
                status=response.status,
                tool_names=[
                    step.label.removeprefix("调用 ")
                    for step in response.steps
                    if step.stage == "tool"
                ],
            )

    def _handle_memory_command(
        self,
        message: str,
        context: ToolExecutionContext,
        *,
        source_message_id: int | None = None,
        source_run_id: str | None = None,
    ) -> str | None:
        stripped = message.strip()
        remember_match = re.fullmatch(
            r"(?:请)?记住[：:，,]?\s*(.+)",
            stripped,
            flags=re.DOTALL,
        )
        if remember_match:
            content = remember_match.group(1).strip()
            try:
                memory_ids = self.memory_store.remember_many(
                    owner_id=context.owner_id,
                    content=content,
                    source=f"{context.channel}:{context.thread_id}",
                    scope="user",
                    source_message_id=source_message_id,
                    source_run_id=source_run_id,
                )
            except MemoryPolicyError as exc:
                return str(exc)
            if len(memory_ids) == 1:
                return f"已为你保存这条记忆：{content}"
            return f"已将这段信息拆分并保存为 {len(memory_ids)} 条独立记忆。"
        if stripped in {"我的记忆", "你记得什么", "查看我的记忆"}:
            memories = self.memory_store.list_memories(context.owner_id)
            if not memories:
                return "目前没有为你保存长期记忆。"
            return "我为你保存的长期记忆：\n" + "\n".join(
                f"- {item}" for item in memories
            )
        if stripped in {"忘记所有记忆", "清空我的记忆"}:
            count = self.memory_store.forget_all(context.owner_id)
            return f"已删除你的 {count} 条长期记忆。"
        if stripped in {"清空当前对话", "新对话"}:
            count = self.memory_store.clear_thread(
                owner_id=context.owner_id,
                thread_id=context.thread_id,
            )
            return f"已清空当前会话的 {count} 条历史消息。"
        return None

    @staticmethod
    def _is_memory_command(message: str) -> bool:
        stripped = message.strip()
        return bool(
            re.fullmatch(r"(?:请)?记住[：:，,]?\s*(.+)", stripped, flags=re.DOTALL)
            or stripped
            in {
                "我的记忆",
                "你记得什么",
                "查看我的记忆",
                "忘记所有记忆",
                "清空我的记忆",
                "清空当前对话",
                "新对话",
            }
        )

    def _run_memory_command(
        self,
        *,
        message: str,
        context: ToolExecutionContext,
        started_at: float,
    ) -> AgentRunResponse:
        run_id = str(uuid4())
        try:
            source_message_id = self.memory_store.create_run(
                run_id=run_id,
                owner_id=context.owner_id,
                thread_id=context.thread_id,
                channel=context.channel,
                message=message,
                project_id=context.project_id,
                persist_user_message=True,
            )
        except ValueError as exc:
            raise AgentRunError(str(exc)) from exc
        try:
            answer = self._handle_memory_command(
                message,
                context,
                source_message_id=source_message_id,
                source_run_id=run_id,
            )
            if answer is None:
                raise AgentRunError("无法识别这个记忆操作。")
            return self._finish_direct_response(
                run_id=run_id,
                answer=answer,
                context=context,
                started_at=started_at,
                mode="deterministic-memory",
                label="执行记忆操作",
                detail="记忆操作由本地确定性规则执行，未发送给模型。",
            )
        except Exception as exc:
            response = self._finish_direct_response(
                run_id=run_id,
                answer=f"记忆操作失败：{type(exc).__name__}",
                context=context,
                started_at=started_at,
                mode="deterministic-memory",
                label="记忆操作失败",
                detail="持久化的记忆操作未能完成。",
                status="failed",
            )
            return response

    def _direct_response(
        self,
        *,
        message: str,
        answer: str,
        context: ToolExecutionContext,
        started_at: float,
        mode: str = "deterministic-memory",
        label: str = "执行记忆操作",
        detail: str = "记忆操作由本地确定性规则执行，未发送给模型。",
        step_output: dict[str, Any] | None = None,
        user_metadata: dict[str, Any] | None = None,
    ) -> AgentRunResponse:
        run_id = str(uuid4())
        try:
            self.memory_store.create_run(
                run_id=run_id,
                owner_id=context.owner_id,
                thread_id=context.thread_id,
                channel=context.channel,
                message=message,
                project_id=context.project_id,
                persist_user_message=True,
                user_metadata=user_metadata,
            )
        except ValueError as exc:
            raise AgentRunError(str(exc)) from exc
        return self._finish_direct_response(
            run_id=run_id,
            answer=answer,
            context=context,
            started_at=started_at,
            mode=mode,
            label=label,
            detail=detail,
            step_output=step_output,
        )

    def _finish_direct_response(
        self,
        *,
        run_id: str,
        answer: str,
        context: ToolExecutionContext,
        started_at: float,
        mode: str,
        label: str,
        detail: str,
        step_output: dict[str, Any] | None = None,
        status: Literal["completed", "failed"] = "completed",
    ) -> AgentRunResponse:
        response = AgentRunResponse(
            run_id=run_id,
            status=status,
            mode=mode,
            answer=answer,
            steps=[
                TraceStep(
                    index=1,
                    stage="final",
                    label=label,
                    detail=detail,
                    duration_ms=_elapsed_ms(started_at),
                    output=step_output,
                )
            ],
            total_duration_ms=_elapsed_ms(started_at),
        )
        self._persist_response(
            response,
            owner_id=context.owner_id,
            thread_id=context.thread_id,
        )
        return response

    def close(self) -> None:
        self.orchestrator.close()
        if self._owns_memory_store:
            self.memory_store.close()
