from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.app.agent import (
    AgentRunIsolationError,
    AgentRunner,
    JsonlTraceStore,
    PlanningResult,
    ToolObservation,
)
from backend.app.memory import SQLiteMemoryStore
from backend.app.models import ToolCall
from backend.app.orchestration import LangGraphOrchestrator
from backend.app.tools import (
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)


class ReplanningPlanner:
    mode = "test-replanning"
    is_llm = False
    model_name = None

    def __init__(self, *, keep_replanning: bool = False) -> None:
        self.keep_replanning = keep_replanning
        self.decisions = 0

    def plan(
        self,
        _: str,
        __: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[
                ToolCall(name="increment", arguments={"value": 0})
            ]
        )

    def continue_plan(
        self,
        _: str,
        __: PlanningResult,
        observations: list[ToolObservation],
        ___: list[dict[str, Any]],
    ) -> PlanningResult:
        self.decisions += 1
        latest = observations[-1].output["value"]
        if self.keep_replanning or self.decisions == 1:
            return PlanningResult(
                tool_calls=[
                    ToolCall(
                        name="increment",
                        arguments={"value": latest},
                    )
                ]
            )
        return PlanningResult(
            tool_calls=[],
            direct_answer=f"最终值是 {latest}。",
        )

    def compose_answer(
        self,
        _: str,
        __: PlanningResult,
        observations: list[ToolObservation],
    ) -> str:
        return f"最终值是 {observations[-1].output['value']}。"


class InvalidCallPlanner(ReplanningPlanner):
    def plan(
        self,
        _: str,
        __: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[
                ToolCall(name="increment", arguments={"value": "bad"})
            ]
        )


class ApprovalPlanner:
    mode = "test-approval"
    is_llm = False
    model_name = None

    def plan(
        self,
        _: str,
        __: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[
                ToolCall(
                    name="publish_result",
                    arguments={"target": "team-chat"},
                )
            ]
        )

    def continue_plan(
        self,
        _: str,
        __: PlanningResult,
        observations: list[ToolObservation],
        ___: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[],
            direct_answer=(
                f"已发布到 {observations[-1].output['target']}。"
            ),
        )

    def compose_answer(
        self,
        _: str,
        __: PlanningResult,
        observations: list[ToolObservation],
    ) -> str:
        return f"已发布到 {observations[-1].output['target']}。"


def build_registry(executed: list[int]) -> ToolRegistry:
    registry = ToolRegistry()

    def increment(arguments: dict[str, Any]) -> dict[str, Any]:
        value = int(arguments["value"]) + 1
        executed.append(value)
        return {"value": value}

    registry.register(
        ToolSpec(
            name="increment",
            description="测试用自增工具",
            parameters={
                "type": "object",
                "properties": {
                    "value": {"type": "integer"},
                },
                "required": ["value"],
                "additionalProperties": False,
            },
            handler=increment,
        )
    )
    return registry


def build_approval_registry(executed: list[str]) -> ToolRegistry:
    registry = ToolRegistry()

    def publish(arguments: dict[str, Any]) -> dict[str, Any]:
        target = str(arguments["target"])
        executed.append(target)
        return {"target": target}

    registry.register(
        ToolSpec(
            name="publish_result",
            description="向团队聊天发布生成结果",
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            handler=publish,
            risk_level="external_write",
            requires_approval=True,
            idempotent=False,
        )
    )
    return registry


def invoke(
    tmp_path: Path,
    planner: ReplanningPlanner,
    *,
    max_steps: int = 4,
) -> tuple[LangGraphOrchestrator, dict[str, Any], list[int]]:
    executed: list[int] = []
    orchestrator = LangGraphOrchestrator(
        planner=planner,
        registry=build_registry(executed),
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        max_steps=max_steps,
        max_replans=4,
        max_consecutive_errors=2,
        max_runtime_seconds=30,
    )
    state = orchestrator.invoke(
        message="把数字连续加二",
        context=ToolExecutionContext(
            owner_id="user-a",
            thread_id="test:loop",
            channel="test",
        ),
    )
    return orchestrator, state, executed


def test_graph_replans_after_observation_and_executes_one_tool_per_loop(
    tmp_path: Path,
) -> None:
    orchestrator, state, executed = invoke(
        tmp_path,
        ReplanningPlanner(),
    )
    try:
        assert state["status"] == "completed"
        assert state["answer"] == "最终值是 2。"
        assert state["step_count"] == 2
        assert state["replan_count"] == 1
        assert executed == [1, 2]
        assert [
            step["output"]["decision"]
            for step in state["steps"]
            if step["stage"] == "decision"
            and step["label"] == "判断下一步"
        ] == ["replan", "complete"]
    finally:
        orchestrator.close()


def test_graph_stops_before_tool_when_step_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    orchestrator, state, executed = invoke(
        tmp_path,
        ReplanningPlanner(keep_replanning=True),
        max_steps=2,
    )
    try:
        assert state["status"] == "failed"
        assert "工具执行步数达到上限 (2)" in state["answer"]
        assert state["step_count"] == 2
        assert executed == [1, 2]
    finally:
        orchestrator.close()


def test_policy_rejects_invalid_tool_arguments_without_execution(
    tmp_path: Path,
) -> None:
    orchestrator, state, executed = invoke(
        tmp_path,
        InvalidCallPlanner(),
    )
    try:
        assert state["status"] == "failed"
        assert "increment.value must be integer" in state["answer"]
        assert state["step_count"] == 0
        assert executed == []
        policy = next(
            step for step in state["steps"] if step["stage"] == "policy"
        )
        assert policy["output"]["decision"] == "deny"
    finally:
        orchestrator.close()


def test_completed_graph_state_is_recoverable_from_sqlite_checkpoint(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    first, state, _ = invoke(tmp_path, ReplanningPlanner())
    first.close()

    second = LangGraphOrchestrator(
        planner=ReplanningPlanner(),
        registry=build_registry([]),
        checkpoint_path=checkpoint_path,
        max_steps=4,
    )
    try:
        snapshot = second.graph.get_state(
            {"configurable": {"thread_id": "test:loop"}}
        )
        assert snapshot.values["status"] == "completed"
        assert snapshot.values["answer"] == state["answer"]
        assert snapshot.values["step_count"] == 2
    finally:
        second.close()


def test_approval_interrupt_survives_restart_and_duplicate_decision_is_safe(
    tmp_path: Path,
) -> None:
    executed: list[str] = []
    memory_path = tmp_path / "memory.sqlite3"
    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    first = AgentRunner(
        build_approval_registry(executed),
        ApprovalPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=SQLiteMemoryStore(memory_path),
        checkpoint_path=checkpoint_path,
    )
    pending = first.run(
        "把结果发布到团队聊天",
        owner_id="user-a",
        thread_id="approval:restart",
        channel="test",
    )
    assert pending.status == "waiting_approval"
    assert pending.approval is not None
    assert pending.approval["tool"] == "publish_result"
    assert executed == []
    first.close()

    second = AgentRunner(
        build_approval_registry(executed),
        ApprovalPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=SQLiteMemoryStore(memory_path),
        checkpoint_path=checkpoint_path,
    )
    try:
        completed = second.resume_run(
            pending.run_id,
            approved=True,
            requester_owner_id="user-a",
        )
        duplicate = second.resume_run(
            pending.run_id,
            approved=True,
            requester_owner_id="user-a",
        )

        assert completed.status == "completed"
        assert duplicate == completed
        assert executed == ["team-chat"]
        messages = second.memory_store.list_messages(
            owner_id="user-a",
            thread_id="approval:restart",
        )
        assert len(messages) == 2
        assert messages[-1].content == "已发布到 team-chat。"
        assert messages[-1].metadata["run"]["status"] == "completed"
    finally:
        second.close()


def test_approval_rejection_and_owner_isolation(
    tmp_path: Path,
) -> None:
    executed: list[str] = []
    runner = AgentRunner(
        build_approval_registry(executed),
        ApprovalPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=SQLiteMemoryStore(tmp_path / "memory.sqlite3"),
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
    )
    try:
        pending = runner.run(
            "发布结果",
            owner_id="user-a",
            thread_id="approval:reject",
            channel="test",
        )
        with pytest.raises(AgentRunIsolationError):
            runner.resume_run(
                pending.run_id,
                approved=True,
                requester_owner_id="user-b",
            )

        rejected = runner.resume_run(
            pending.run_id,
            approved=False,
            requester_owner_id="user-a",
        )
        assert rejected.status == "failed"
        assert "拒绝" in rejected.answer
        assert executed == []
    finally:
        runner.close()


def test_tool_execution_ledger_reuses_completed_result(
    tmp_path: Path,
) -> None:
    executed: list[int] = []

    class SinglePlanner(ReplanningPlanner):
        def continue_plan(
            self,
            _: str,
            __: PlanningResult,
            observations: list[ToolObservation],
            ___: list[dict[str, Any]],
        ) -> PlanningResult:
            return PlanningResult(
                tool_calls=[],
                direct_answer=str(observations[-1].output["value"]),
            )

    orchestrator = LangGraphOrchestrator(
        planner=SinglePlanner(),
        registry=build_registry(executed),
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
        max_steps=2,
    )
    context = ToolExecutionContext(
        owner_id="user-a",
        thread_id="test:ledger",
        channel="test",
    )
    try:
        first = orchestrator.invoke(
            message="加一",
            context=context,
            run_id="same-run",
        )
        second = orchestrator.invoke(
            message="加一",
            context=context,
            run_id="same-run",
        )
        assert first["status"] == second["status"] == "completed"
        assert executed == [1]
        replay_step = next(
            step
            for step in second["steps"]
            if step["stage"] == "tool"
        )
        assert "复用" in replay_step["detail"]
    finally:
        orchestrator.close()
