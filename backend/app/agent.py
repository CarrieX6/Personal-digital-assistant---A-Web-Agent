from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .models import AgentRunResponse, ToolCall, TraceStep
from .tools import ToolRegistry


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _task_payload(message: str) -> str:
    for separator in ("：", ":"):
        if separator in message:
            payload = message.split(separator, maxsplit=1)[1].strip()
            if payload:
                return payload
    return message.strip()


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

        if source_match and style_matches and any(
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
                    sections.append("个人资产库还是空的，可以先生成空间照片或风格化图片。")
            elif name == "create_spatial_scene":
                sections.append(
                    "空间照片任务已创建，正在本机进行深度估计和分层处理。"
                    "完成后会自动打开可交互视角。"
                )
            elif name == "create_photo_style_transfer":
                sections.append(
                    "图片风格化任务已创建，正在按参考图迁移色彩与纹理。"
                    "完成后会写入本地个人资产库。"
                )
            elif name == "list_capabilities":
                examples = "\n".join(f"- {item}" for item in output["examples"])
                sections.append("当前是无模型演示模式，可以尝试：\n" + examples)
        return "\n\n".join(sections)


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


class AgentRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        trace_store: JsonlTraceStore,
        max_steps: int = 4,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.trace_store = trace_store
        self.max_steps = max_steps

    def run(
        self,
        message: str,
        *,
        source_image_context: dict[str, Any] | None = None,
        style_image_contexts: list[dict[str, Any]] | None = None,
    ) -> AgentRunResponse:
        run_started = time.perf_counter()
        steps: list[TraceStep] = []
        planner_message = message
        if source_image_context:
            planner_message = (
                f"{message}\n\n"
                "[系统提供的本地图片附件]\n"
                f"source_image_id={source_image_context['id']}\n"
                f"original_name={source_image_context['original_name']}\n"
                f"dimensions={source_image_context['width']}x"
                f"{source_image_context['height']}\n"
                "附件原图保留在本机，模型不可查看图片内容。"
            )
        if style_image_contexts:
            style_lines = "\n".join(
                (
                    f"style_image_id={item['id']} "
                    f"original_name={item['original_name']} "
                    f"dimensions={item['width']}x{item['height']}"
                )
                for item in style_image_contexts
            )
            planner_message = (
                f"{planner_message}\n\n"
                "[系统提供的本地风格参考图附件]\n"
                f"{style_lines}\n"
                "参考图原图保留在本机，模型不可查看图片内容。"
            )

        plan_started = time.perf_counter()
        plan = self.planner.plan(planner_message, self.registry.openai_schemas())
        plan.tool_calls = plan.tool_calls[: self.max_steps]
        selected_tools = " → ".join(call.name for call in plan.tool_calls)
        plan_detail = (
            f"规划器选择了 {len(plan.tool_calls)} 个工具：{selected_tools}"
            if plan.tool_calls
            else "模型判断本次任务无需调用工具，将直接回答。"
        )
        steps.append(
            TraceStep(
                index=1,
                stage="planning",
                label="生成执行计划",
                detail=plan_detail,
                duration_ms=_elapsed_ms(plan_started),
            )
        )

        observations: list[ToolObservation] = []
        for tool_call in plan.tool_calls:
            tool_started = time.perf_counter()
            output = self.registry.execute(tool_call.name, tool_call.arguments)
            observations.append(ToolObservation(call=tool_call, output=output))
            steps.append(
                TraceStep(
                    index=len(steps) + 1,
                    stage="tool",
                    label=f"调用 {tool_call.name}",
                    detail="模型参数通过校验，工具执行完成。",
                    duration_ms=_elapsed_ms(tool_started),
                    output=output,
                )
            )

        final_started = time.perf_counter()
        answer = self.planner.compose_answer(planner_message, plan, observations)
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="final",
                label="生成最终回答",
                detail=(
                    "模型读取工具结果后生成最终回答。"
                    if self.planner.is_llm
                    else "规则规划器将结构化工具输出整理为用户可读回答。"
                ),
                duration_ms=_elapsed_ms(final_started),
            )
        )

        response = AgentRunResponse(
            run_id=str(uuid4()),
            status="completed",
            mode=self.planner.mode,
            answer=answer,
            steps=steps,
            total_duration_ms=_elapsed_ms(run_started),
        )
        self.trace_store.append(response)
        return response
