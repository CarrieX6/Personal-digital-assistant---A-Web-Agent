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

from .context import ContextBuilder
from .models import AgentRunResponse, ToolCall, TraceStep
from .memory import SQLiteMemoryStore
from .tools import ToolExecutionContext, ToolRegistry


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

        if source_match and any(
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
        if isinstance(attachments, list) and attachments:
            trusted = attachments[0].get("trusted_system", {})
            source_image_id = (
                trusted.get("source_image_id")
                if isinstance(trusted, dict)
                else None
            )
            if isinstance(source_image_id, str) and source_image_id:
                message = f"{message}\nsource_image_id={source_image_id}"
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
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.trace_store = trace_store
        self.max_steps = max_steps
        self.memory_store = memory_store or SQLiteMemoryStore(
            trace_store.path.parent / "agent_memory.sqlite3"
        )
        self.context_builder = context_builder or ContextBuilder()
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

    def run(
        self,
        message: str,
        *,
        source_image_context: dict[str, Any] | None = None,
        owner_id: str = "local",
        thread_id: str = "local:default",
        channel: str = "web",
    ) -> AgentRunResponse:
        run_started = time.perf_counter()
        execution_context = ToolExecutionContext(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
        )
        conversation = self.memory_store.context(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
        )
        memory_answer = self._handle_memory_command(
            message,
            execution_context,
        )
        if memory_answer is not None:
            return self._direct_response(
                message=message,
                answer=memory_answer,
                context=execution_context,
                started_at=run_started,
            )

        context_builder = ContextBuilder(
            self.context_builder.config,
            system_prompt=str(getattr(self.planner, "system_prompt", "")),
        )
        all_tool_schemas = self.registry.openai_schemas()
        selected_tool_schemas = context_builder.select_tools(
            user_message=message,
            tool_schemas=all_tool_schemas,
            attachment_present=source_image_context is not None,
        )
        built_context = context_builder.build(
            current_user_message=message,
            conversation_messages=conversation.messages,
            memories=conversation.memories,
            attachments=(
                [source_image_context] if source_image_context else []
            ),
            selected_tool_schemas=selected_tool_schemas,
        )

        run_id = str(uuid4())
        try:
            self.memory_store.create_run(
                run_id=run_id,
                owner_id=owner_id,
                thread_id=thread_id,
                channel=channel,
                message=message,
            )
        except ValueError as exc:
            raise AgentRunError(str(exc)) from exc

        self.orchestrator.planner = self.planner
        graph_state = self.orchestrator.invoke(
            message=message,
            context=execution_context,
            run_id=run_id,
            planner_context=built_context.to_planner_context(),
            selected_tool_names=list(built_context.selected_tool_names),
        )
        response = self._response_from_state(
            graph_state,
            started_at=run_started,
        )
        user_metadata: dict[str, Any] = {}
        if source_image_context:
            user_metadata["attachment"] = {
                "name": source_image_context["original_name"],
                "source_image_id": source_image_context["id"],
                "width": source_image_context["width"],
                "height": source_image_context["height"],
            }
        self.memory_store.append_message(
            owner_id=owner_id,
            thread_id=thread_id,
            role="user",
            content=message,
            metadata=user_metadata,
        )
        self._persist_response(
            response,
            owner_id=owner_id,
            thread_id=thread_id,
        )
        return response

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
        self.trace_store.append(response)
        assistant_metadata: dict[str, Any] = {"run": response_data}
        for step in response.steps:
            output = step.output or {}
            asset_id = output.get("asset_id")
            if isinstance(asset_id, str):
                assistant_metadata["asset_id"] = asset_id
                break
        self.memory_store.upsert_assistant_run_message(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=response.run_id,
            content=response.answer,
            metadata=assistant_metadata,
        )

    def _handle_memory_command(
        self,
        message: str,
        context: ToolExecutionContext,
    ) -> str | None:
        stripped = message.strip()
        remember_match = re.fullmatch(
            r"(?:请)?记住[：:，,]?\s*(.+)",
            stripped,
            flags=re.DOTALL,
        )
        if remember_match:
            content = remember_match.group(1).strip()
            self.memory_store.remember(
                owner_id=context.owner_id,
                content=content,
                source=f"{context.channel}:{context.thread_id}",
            )
            return f"已为你保存这条记忆：{content}"
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

    def _direct_response(
        self,
        *,
        message: str,
        answer: str,
        context: ToolExecutionContext,
        started_at: float,
    ) -> AgentRunResponse:
        response = AgentRunResponse(
            run_id=str(uuid4()),
            status="completed",
            mode="deterministic-memory",
            answer=answer,
            steps=[
                TraceStep(
                    index=1,
                    stage="final",
                    label="执行记忆操作",
                    detail="记忆操作由本地确定性规则执行，未发送给模型。",
                    duration_ms=_elapsed_ms(started_at),
                )
            ],
            total_duration_ms=_elapsed_ms(started_at),
        )
        self.trace_store.append(response)
        self.memory_store.append_message(
            owner_id=context.owner_id,
            thread_id=context.thread_id,
            role="user",
            content=message,
        )
        self.memory_store.append_message(
            owner_id=context.owner_id,
            thread_id=context.thread_id,
            role="assistant",
            content=answer,
            run_id=response.run_id,
            metadata={"run": response.model_dump(mode="json")},
        )
        return response

    def close(self) -> None:
        self.orchestrator.close()
