from __future__ import annotations

import json
import os
import subprocess
import sys
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


class AmbiguityAwareApprovalPlanner(ApprovalPlanner):
    def continue_plan(
        self,
        message: str,
        plan: PlanningResult,
        observations: list[ToolObservation],
        schemas: list[dict[str, Any]],
    ) -> PlanningResult:
        latest = observations[-1]
        if latest.output.get("error_type"):
            return PlanningResult(
                tool_calls=[],
                direct_answer="外部执行状态不明确，等待人工核对。",
            )
        return super().continue_plan(message, plan, observations, schemas)


class AsyncJobPlanner:
    mode = "test-async-job"
    is_llm = False
    model_name = None

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.decisions = 0

    def plan(
        self,
        _: str,
        __: list[dict[str, Any]],
    ) -> PlanningResult:
        return PlanningResult(
            tool_calls=[ToolCall(name=self.tool_name, arguments={})]
        )

    def continue_plan(
        self,
        _: str,
        __: PlanningResult,
        ___: list[ToolObservation],
        ____: list[dict[str, Any]],
    ) -> PlanningResult:
        self.decisions += 1
        return PlanningResult(
            tool_calls=[
                ToolCall(
                    name="get_job_status",
                    arguments={"job_id": "job-1"},
                )
            ]
        )

    def compose_answer(
        self,
        _: str,
        __: PlanningResult,
        ___: list[ToolObservation],
    ) -> str:
        raise AssertionError("异步任务创建后不应再次调用模型")


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


@pytest.mark.parametrize(
    ("tool_name", "answer_fragment"),
    [
        ("create_spatial_scene", "空间照片任务已创建"),
        ("create_photo_style_transfer", "图片风格化任务已创建"),
    ],
)
def test_async_visual_job_hands_off_without_model_polling(
    tmp_path: Path,
    tool_name: str,
    answer_fragment: str,
) -> None:
    planner = AsyncJobPlanner(tool_name)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name=tool_name,
            description="创建异步视觉任务",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=lambda _: {
                "job_id": "job-1",
                "asset_id": "asset-1",
                "status": "queued",
            },
        )
    )
    orchestrator = LangGraphOrchestrator(
        planner=planner,
        registry=registry,
        checkpoint_path=tmp_path / f"{tool_name}.sqlite3",
        max_steps=4,
        max_replans=3,
    )
    try:
        state = orchestrator.invoke(
            message="创建视觉任务",
            context=ToolExecutionContext(
                owner_id="user-a",
                thread_id=f"test:{tool_name}",
                channel="test",
            ),
        )

        assert state["status"] == "completed"
        assert answer_fragment in state["answer"]
        assert state["step_count"] == 1
        assert state["replan_count"] == 0
        assert planner.decisions == 0
        decision = next(
            step
            for step in state["steps"]
            if step["label"] == "判断下一步"
        )
        assert decision["output"]["decision"] == "async_handoff"
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
        context = ToolExecutionContext(
            owner_id="user-a",
            thread_id="test:loop",
            channel="test",
        )
        snapshot = second.graph.get_state(
            {
                "configurable": {
                    "thread_id": second.checkpoint_thread_id(
                        context,
                        state["run_id"],
                    )
                }
            }
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


@pytest.mark.parametrize(
    ("node", "mode"),
    [
        ("plan", "success"),
        ("policy", "success"),
        ("approval", "approval"),
        ("execute_tool", "success"),
        ("observe", "success"),
        ("decide", "success"),
        ("finalize", "success"),
        ("fail", "failure"),
    ],
)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_real_process_crash_recovers_consistently_at_every_node_boundary(
    tmp_path: Path,
    node: str,
    mode: str,
    phase: str,
) -> None:
    checkpoint_path = tmp_path / f"{node}-{phase}.sqlite3"
    worker = Path(__file__).with_name("crash_matrix_worker.py")

    def run_worker(action: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(worker),
                action,
                str(checkpoint_path),
                mode,
                *extra,
            ],
            cwd=Path(__file__).resolve().parents[2],
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            },
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    crash_action = "invoke"
    if mode == "approval":
        setup = run_worker("invoke")
        assert setup.returncode == 0, setup.stderr
        crash_action = "resume"

    crashed = run_worker(crash_action, node, phase)
    assert crashed.returncode == 91, crashed.stderr

    recovered = run_worker("recover")
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout.strip().splitlines()[-1])
    if mode == "failure":
        assert result["status"] == "failed"
        assert result["last_error"]
    else:
        assert result == {
            "status": "completed",
            "answer": "matrix-complete",
            "last_error": None,
            "step_count": 1,
        }


def test_interrupted_run_persists_user_event_and_resumes_from_own_checkpoint(
    tmp_path: Path,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    attempts = 0
    executed: list[int] = []

    def build_crashing_registry() -> ToolRegistry:
        registry = ToolRegistry()

        def increment(arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SimulatedProcessCrash()
            value = int(arguments["value"]) + 1
            executed.append(value)
            return {"value": value}

        registry.register(
            ToolSpec(
                name="increment",
                description="测试崩溃恢复用自增工具",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=increment,
                idempotent=True,
            )
        )
        return registry

    memory_path = tmp_path / "memory.sqlite3"
    checkpoint_path = tmp_path / "checkpoints.sqlite3"
    first_store = SQLiteMemoryStore(memory_path)
    first = AgentRunner(
        build_crashing_registry(),
        ReplanningPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=first_store,
        checkpoint_path=checkpoint_path,
    )
    with pytest.raises(SimulatedProcessCrash):
        first.run(
            "从安全边界继续完成计算",
            owner_id="user-a",
            thread_id="recovery:run",
            channel="test",
        )
    interrupted = first_store.list_runs(status="running", limit=10)
    assert len(interrupted) == 1
    run_id = interrupted[0].run_id
    assert interrupted[0].checkpoint_thread_id
    messages = first_store.list_messages(
        owner_id="user-a",
        thread_id="recovery:run",
    )
    assert [(item.role, item.content, item.run_id) for item in messages] == [
        ("user", "从安全边界继续完成计算", run_id)
    ]
    first.close()
    first_store.close()

    second_store = SQLiteMemoryStore(memory_path)
    second = AgentRunner(
        build_crashing_registry(),
        ReplanningPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=second_store,
        checkpoint_path=checkpoint_path,
    )
    try:
        recovered = second_store.get_run(run_id)
        assert recovered is not None and recovered.status == "recoverable"
        completed = second.continue_run(
            run_id,
            requester_owner_id="user-a",
        )
        assert completed.status == "completed"
        assert completed.answer == "最终值是 2。"
        assert executed == [1, 2]
        messages = second_store.list_messages(
            owner_id="user-a",
            thread_id="recovery:run",
        )
        assert [(item.role, item.run_id) for item in messages] == [
            ("user", run_id),
            ("assistant", run_id),
        ]
    finally:
        second.close()
        second_store.close()


def test_missing_checkpoint_requires_attention_and_can_be_abandoned(
    tmp_path: Path,
) -> None:
    memory_path = tmp_path / "memory.sqlite3"
    store = SQLiteMemoryStore(memory_path)
    store.create_run(
        run_id="run-without-checkpoint",
        owner_id="user-a",
        thread_id="recovery:missing",
        channel="test",
        message="执行有副作用的任务",
        checkpoint_thread_id="agent-run:missing",
        persist_user_message=True,
    )

    runner = AgentRunner(
        ToolRegistry(),
        ReplanningPlanner(),
        JsonlTraceStore(tmp_path / "runs.jsonl"),
        memory_store=store,
        checkpoint_path=tmp_path / "checkpoints.sqlite3",
    )
    try:
        interrupted = store.get_run("run-without-checkpoint")
        assert interrupted is not None
        assert interrupted.status == "needs_attention"

        with pytest.raises(AgentRunIsolationError):
            runner.abandon_interrupted_run(
                interrupted.run_id,
                requester_owner_id="user-b",
            )

        abandoned = runner.abandon_interrupted_run(
            interrupted.run_id,
            requester_owner_id="user-a",
        )
        assert abandoned.status == "failed"
        assert "没有重放" in abandoned.answer
        messages = store.list_messages(
            owner_id="user-a",
            thread_id="recovery:missing",
        )
        assert [(item.role, item.run_id) for item in messages] == [
            ("user", interrupted.run_id),
            ("assistant", interrupted.run_id),
        ]

        store.create_run(
            run_id="run-after-abandon",
            owner_id="user-a",
            thread_id="recovery:missing",
            channel="test",
            message="开始新任务",
        )
    finally:
        runner.close()
        store.close()


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


def test_non_idempotent_effect_is_not_replayed_after_post_effect_crash(
    tmp_path: Path,
) -> None:
    executed: list[str] = []
    checkpoint_path = tmp_path / "non-idempotent-crash.sqlite3"
    context = ToolExecutionContext(
        owner_id="user-a",
        thread_id="test:non-idempotent-crash",
        channel="test",
    )
    run_id = "non-idempotent-crash-run"
    first = LangGraphOrchestrator(
        planner=AmbiguityAwareApprovalPlanner(),
        registry=build_approval_registry(executed),
        checkpoint_path=checkpoint_path,
        max_steps=2,
    )
    checkpoint_thread_id = first.checkpoint_thread_id(context, run_id)
    try:
        pending = first.invoke(
            message="发布结果",
            context=context,
            run_id=run_id,
        )
        assert pending["status"] == "waiting_approval"

        def crash_before_ledger_commit(
            _idempotency_key: str,
            _output: dict[str, Any],
        ) -> None:
            raise SystemExit("injected crash after external effect")

        first._ledger.complete = crash_before_ledger_commit  # type: ignore[method-assign]
        with pytest.raises(SystemExit):
            first.resume(
                run_id=run_id,
                context=context,
                approved=True,
                actor_id="user-a",
                checkpoint_thread_id=checkpoint_thread_id,
            )
        assert executed == ["team-chat"]
    finally:
        first.close()

    second = LangGraphOrchestrator(
        planner=AmbiguityAwareApprovalPlanner(),
        registry=build_approval_registry(executed),
        checkpoint_path=checkpoint_path,
        max_steps=2,
    )
    try:
        recovered = second.continue_from_checkpoint(
            run_id=run_id,
            context=context,
            checkpoint_thread_id=checkpoint_thread_id,
        )
        assert executed == ["team-chat"]
        ambiguous_steps = [
            step
            for step in recovered["steps"]
            if (step.get("output") or {}).get("error_type")
            == "ambiguous_side_effect"
        ]
        assert len(ambiguous_steps) == 1
    finally:
        second.close()
