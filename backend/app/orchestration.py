from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .agent import Planner, PlanningResult, ToolObservation, _elapsed_ms
from .models import ToolCall, TraceStep
from .tools import ToolError, ToolExecutionContext, ToolRegistry


LOGGER = logging.getLogger(__name__)
GraphStatus = Literal[
    "running", "waiting_approval", "completed", "failed"
]


class AgentGraphState(TypedDict):
    run_id: str
    message: str
    planner_context: dict[str, Any]
    selected_tool_names: list[str]
    context_budget: dict[str, Any]
    execution_context: dict[str, Any]
    plan: dict[str, Any]
    pending_calls: list[dict[str, Any]]
    round_observations: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    answer: str
    status: GraphStatus
    step_count: int
    replan_count: int
    consecutive_errors: int
    started_at: float
    last_error: str | None
    approval_required: bool
    approval: dict[str, Any] | None


class SQLiteToolExecutionLedger:
    """Prevents checkpoint replay from duplicating tool side effects."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_executions (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS tool_executions_run_idx
                ON tool_executions(run_id, updated_at DESC);
                """
            )

    def prepare(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        tool_name: str,
        retry_safe: bool,
    ) -> tuple[Literal["execute", "cached", "blocked"], dict[str, Any] | None]:
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT status, output_json, error
                FROM tool_executions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO tool_executions (
                        idempotency_key, run_id, tool_name, status,
                        output_json, error, created_at, updated_at
                    ) VALUES (?, ?, ?, 'running', NULL, NULL, ?, ?)
                    """,
                    (
                        idempotency_key,
                        run_id,
                        tool_name,
                        timestamp,
                        timestamp,
                    ),
                )
                return "execute", None

            status = str(row[0])
            if status == "completed":
                try:
                    output = json.loads(str(row[1] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    output = {}
                return (
                    "cached",
                    output if isinstance(output, dict) else {},
                )
            if retry_safe:
                connection.execute(
                    """
                    UPDATE tool_executions
                    SET status = 'running', error = NULL, updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (timestamp, idempotency_key),
                )
                return "execute", None
            return (
                "blocked",
                {
                    "error": (
                        "检测到非幂等工具的未完成执行记录，"
                        "为避免重复副作用已停止自动重试。"
                    ),
                    "error_type": "ambiguous_side_effect",
                },
            )

    def complete(
        self,
        idempotency_key: str,
        output: dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'completed', output_json = ?, error = NULL,
                    updated_at = ?
                WHERE idempotency_key = ?
                """,
                (
                    json.dumps(
                        output,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    time.time(),
                    idempotency_key,
                ),
            )

    def fail(self, idempotency_key: str, error: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE tool_executions
                SET status = 'failed', error = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (error[:1000], time.time(), idempotency_key),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection


class LangGraphOrchestrator:
    """Durable, bounded tool loop using owner-scoped LangGraph threads."""

    def __init__(
        self,
        *,
        planner: Planner,
        registry: ToolRegistry,
        checkpoint_path: Path,
        max_steps: int,
        max_replans: int = 3,
        max_consecutive_errors: int = 2,
        max_runtime_seconds: float = 120.0,
        failpoint: Callable[[str, str, AgentGraphState], None] | None = None,
    ) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.planner = planner
        self.registry = registry
        self.max_steps = max(1, max_steps)
        self.max_replans = max(0, max_replans)
        self.max_consecutive_errors = max(1, max_consecutive_errors)
        self.max_runtime_seconds = max(1.0, max_runtime_seconds)
        self._failpoint = failpoint
        self._recursion_limit = max(
            25,
            self.max_steps * 5 + self.max_replans * 3 + 10,
        )
        self._lock = threading.Lock()
        self._transient_vision_inputs: dict[str, list[dict[str, str]]] = {}
        self._connection = sqlite3.connect(
            checkpoint_path,
            check_same_thread=False,
        )
        self._checkpointer = SqliteSaver(self._connection)
        self._ledger = SQLiteToolExecutionLedger(checkpoint_path)

        builder = StateGraph(AgentGraphState)
        builder.add_node("plan", self._guard_node("plan", self._plan))
        builder.add_node("policy", self._guard_node("policy", self._policy))
        builder.add_node(
            "approval", self._guard_node("approval", self._approval)
        )
        builder.add_node(
            "execute_tool",
            self._guard_node("execute_tool", self._execute_tool),
        )
        builder.add_node("observe", self._guard_node("observe", self._observe))
        builder.add_node("decide", self._guard_node("decide", self._decide))
        builder.add_node(
            "finalize", self._guard_node("finalize", self._finalize)
        )
        builder.add_node("fail", self._guard_node("fail", self._fail))

        builder.add_edge(START, "plan")
        builder.add_conditional_edges(
            "plan",
            self._route_after_plan,
            {"policy": "policy", "finalize": "finalize"},
        )
        builder.add_conditional_edges(
            "policy",
            self._route_after_policy,
            {
                "approval": "approval",
                "execute_tool": "execute_tool",
                "fail": "fail",
            },
        )
        builder.add_conditional_edges(
            "approval",
            self._route_after_approval,
            {"execute_tool": "execute_tool", "fail": "fail"},
        )
        builder.add_edge("execute_tool", "observe")
        builder.add_conditional_edges(
            "observe",
            self._route_after_observe,
            {"policy": "policy", "decide": "decide", "fail": "fail"},
        )
        builder.add_conditional_edges(
            "decide",
            self._route_after_decide,
            {
                "policy": "policy",
                "finalize": "finalize",
                "fail": "fail",
            },
        )
        builder.add_edge("finalize", END)
        builder.add_edge("fail", END)
        self.graph = builder.compile(checkpointer=self._checkpointer)

    def _guard_node(
        self,
        name: str,
        node: Callable[[AgentGraphState], dict[str, Any]],
    ) -> Callable[[AgentGraphState], dict[str, Any]]:
        def guarded(state: AgentGraphState) -> dict[str, Any]:
            if self._failpoint is not None:
                self._failpoint(name, "before", state)
            result = node(state)
            if self._failpoint is not None:
                merged = dict(state)
                merged.update(result)
                self._failpoint(name, "after", merged)  # type: ignore[arg-type]
            return result

        return guarded

    def invoke(
        self,
        *,
        message: str,
        context: ToolExecutionContext,
        run_id: str | None = None,
        planner_context: dict[str, Any] | None = None,
        selected_tool_names: list[str] | None = None,
        vision_inputs: list[dict[str, str]] | None = None,
    ) -> AgentGraphState:
        selected_run_id = run_id or str(uuid4())
        selected_names = (
            list(selected_tool_names)
            if selected_tool_names is not None
            else [item.name for item in self.registry.list_tools()]
        )
        structured_context = dict(planner_context or {})
        initial: AgentGraphState = {
            "run_id": selected_run_id,
            "message": message,
            "planner_context": structured_context,
            "selected_tool_names": selected_names,
            "context_budget": dict(structured_context.get("budget", {})),
            "execution_context": {
                "owner_id": context.owner_id,
                "thread_id": context.thread_id,
                "channel": context.channel,
                "project_id": context.project_id,
            },
            "plan": {},
            "pending_calls": [],
            "round_observations": [],
            "observations": [],
            "steps": [],
            "answer": "",
            "status": "running",
            "step_count": 0,
            "replan_count": 0,
            "consecutive_errors": 0,
            "started_at": time.time(),
            "last_error": None,
            "approval_required": False,
            "approval": None,
        }
        config = self._config(
            self.checkpoint_thread_id(context, selected_run_id)
        )
        with self._lock:
            if vision_inputs:
                self._transient_vision_inputs[selected_run_id] = list(
                    vision_inputs
                )
            try:
                snapshot = self.graph.get_state(config)
                if snapshot.values and not self._is_finalized_state(
                    dict(snapshot.values)
                ):
                    raise ValueError("这个 Agent Run 已经存在执行 Checkpoint。")
                # Seed the durable START checkpoint before any node runs. This
                # makes a hard process exit immediately before `plan` resumable.
                self.graph.update_state(config, initial, as_node=START)
                result = self.graph.invoke(None, config)
            finally:
                self._transient_vision_inputs.pop(selected_run_id, None)
        return self._with_interrupt(result)

    def resume(
        self,
        *,
        run_id: str,
        context: ToolExecutionContext,
        approved: bool,
        actor_id: str,
        checkpoint_thread_id: str | None = None,
    ) -> AgentGraphState:
        config = self._config(checkpoint_thread_id or context.thread_id)
        with self._lock:
            snapshot = self.graph.get_state(config)
            values = snapshot.values
            if (
                values.get("run_id") != run_id
                or values.get("execution_context") != {
                    "owner_id": context.owner_id,
                    "thread_id": context.thread_id,
                    "channel": context.channel,
                    "project_id": context.project_id,
                }
            ):
                raise ValueError("待审批运行与当前用户或会话不匹配。")
            if not any(task.interrupts for task in snapshot.tasks):
                raise ValueError("这个 Agent Run 当前不在等待审批。")
            result = self.graph.invoke(
                Command(
                    resume={
                        "approved": approved,
                        "actor_id": actor_id,
                    }
                ),
                config,
            )
        return self._with_interrupt(result)

    def inspect_checkpoint(
        self,
        *,
        run_id: str,
        context: ToolExecutionContext,
        checkpoint_thread_id: str,
    ) -> dict[str, Any]:
        config = self._config(checkpoint_thread_id)
        with self._lock:
            snapshot = self.graph.get_state(config)
        values = dict(snapshot.values or {})
        if not values:
            return {
                "exists": False,
                "resumable": False,
                "waiting_approval": False,
                "terminal": False,
                "state": {},
                "next": [],
            }
        self._validate_snapshot_identity(
            values,
            run_id=run_id,
            context=context,
        )
        waiting_approval = any(task.interrupts for task in snapshot.tasks)
        next_nodes = list(snapshot.next)
        status = str(values.get("status", "running"))
        restored_state = self._with_interrupt(values)
        if waiting_approval:
            restored_state["status"] = "waiting_approval"
        steps = values.get("steps", [])
        finalized = bool(
            isinstance(steps, list)
            and steps
            and isinstance(steps[-1], dict)
            and steps[-1].get("stage") == "final"
        )
        repairable = not next_nodes and not waiting_approval and not finalized
        return {
            "exists": True,
            "resumable": (bool(next_nodes) or repairable) and not waiting_approval,
            "waiting_approval": waiting_approval,
            "terminal": (
                not next_nodes
                and finalized
                and status in {"completed", "failed"}
            ),
            "state": restored_state,
            "next": next_nodes,
        }

    def continue_from_checkpoint(
        self,
        *,
        run_id: str,
        context: ToolExecutionContext,
        checkpoint_thread_id: str,
    ) -> AgentGraphState:
        config = self._config(checkpoint_thread_id)
        with self._lock:
            snapshot = self.graph.get_state(config)
            values = dict(snapshot.values or {})
            if not values:
                raise ValueError("找不到这个 Agent Run 的执行 Checkpoint。")
            self._validate_snapshot_identity(
                values,
                run_id=run_id,
                context=context,
            )
            if any(task.interrupts for task in snapshot.tasks):
                raise ValueError("这个 Agent Run 正在等待审批，不能直接继续。")
            if not snapshot.next:
                if self._is_finalized_state(values):
                    return self._with_interrupt(values)
                self._repair_stalled_checkpoint(config, values)
                snapshot = self.graph.get_state(config)
                if not snapshot.next:
                    raise ValueError("执行 Checkpoint 无法确定安全恢复节点。")
            # The runtime guard measures active execution time. A process outage
            # is not active work, so a recovery attempt receives a fresh lease.
            self.graph.update_state(
                config,
                {"started_at": time.time()},
            )
            result = self.graph.invoke(None, config)
        return self._with_interrupt(result)

    @staticmethod
    def _is_finalized_state(values: dict[str, Any]) -> bool:
        steps = values.get("steps", [])
        return bool(
            isinstance(steps, list)
            and steps
            and isinstance(steps[-1], dict)
            and steps[-1].get("stage") == "final"
            and values.get("status") in {"completed", "failed"}
        )

    def _repair_stalled_checkpoint(
        self,
        config: dict[str, Any],
        values: dict[str, Any],
    ) -> None:
        steps = values.get("steps", [])
        as_node = START
        if isinstance(steps, list) and steps and isinstance(steps[-1], dict):
            last = steps[-1]
            label = str(last.get("label", ""))
            stage = str(last.get("stage", ""))
            if label == "观察工具结果":
                as_node = "observe"
            elif label == "判断下一步":
                as_node = "decide"
            elif stage == "planning":
                as_node = "plan"
            elif stage == "policy":
                as_node = "policy"
            elif stage == "approval":
                as_node = "approval"
            elif stage == "tool":
                as_node = "execute_tool"
        self.graph.update_state(config, {}, as_node=as_node)

    @staticmethod
    def _validate_snapshot_identity(
        values: dict[str, Any],
        *,
        run_id: str,
        context: ToolExecutionContext,
    ) -> None:
        if (
            values.get("run_id") != run_id
            or values.get("execution_context")
            != {
                "owner_id": context.owner_id,
                "thread_id": context.thread_id,
                "channel": context.channel,
                "project_id": context.project_id,
            }
        ):
            raise ValueError("执行 Checkpoint 与当前用户、会话或 Run 不匹配。")

    @staticmethod
    def checkpoint_thread_id(
        context: ToolExecutionContext,
        run_id: str,
    ) -> str:
        canonical = json.dumps(
            {
                "owner_id": context.owner_id,
                "thread_id": context.thread_id,
                "project_id": context.project_id,
                "run_id": run_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "agent-run:" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _plan(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        tool_schemas = self._selected_tool_schemas(state)
        plan_with_context = getattr(self.planner, "plan_with_context", None)
        if callable(plan_with_context) and state["planner_context"]:
            planning_context = dict(state["planner_context"])
            vision_inputs = self._transient_vision_inputs.get(state["run_id"])
            if vision_inputs:
                planning_context["_vision_inputs"] = vision_inputs
            plan = plan_with_context(
                planning_context,
                tool_schemas,
            )
        else:
            plan = self.planner.plan(state["message"], tool_schemas)
        selected = " → ".join(call.name for call in plan.tool_calls)
        available_tools = len(state["selected_tool_names"])
        detail = (
            f"规划器从 {available_tools} 个预选工具中选择了 "
            f"{len(plan.tool_calls)} 个工具：{selected}"
            if plan.tool_calls
            else "模型判断本次任务无需调用工具，将直接回答。"
        )
        step = TraceStep(
            index=1,
            stage="planning",
            label="生成执行计划",
            detail=detail,
            duration_ms=_elapsed_ms(started),
            output={
                "selected_tools": list(state["selected_tool_names"]),
                "context_budget": dict(state["context_budget"]),
            },
        )
        return {
            "plan": self._serialize_plan(plan),
            "pending_calls": [
                call.model_dump(mode="json") for call in plan.tool_calls
            ],
            "round_observations": [],
            "observations": [],
            "steps": [step.model_dump(mode="json")],
            "answer": plan.direct_answer or "",
            "status": "running",
            "last_error": None,
        }

    @staticmethod
    def _route_after_plan(state: AgentGraphState) -> str:
        return "policy" if state["pending_calls"] else "finalize"

    def _policy(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        steps = list(state["steps"])
        error = self._hard_boundary_error(state)
        call: ToolCall | None = None

        if error is None:
            try:
                call = ToolCall.model_validate(state["pending_calls"][0])
                self.registry.validate_call(call.name, call.arguments)
            except (IndexError, ToolError, ValueError) as exc:
                error = str(exc)

        spec = self.registry.get_spec(call.name) if call and error is None else None
        requires_approval = bool(spec and spec.requires_approval)
        approval = (
            {
                "type": "tool_approval",
                "run_id": state["run_id"],
                "tool": call.name,
                "description": spec.description,
                "risk_level": spec.risk_level,
                "arguments": call.arguments,
            }
            if call is not None and spec is not None and requires_approval
            else None
        )
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="policy",
                label="执行策略检查",
                detail=(
                    (
                        f"工具 {call.name} 已通过基础检查，"
                        "但必须获得用户批准。"
                    )
                    if requires_approval
                    else f"工具 {call.name} 已通过名称、参数和运行预算检查。"
                    if call is not None and error is None
                    else f"策略检查拒绝执行：{error}"
                ),
                duration_ms=_elapsed_ms(started),
                output={
                    "decision": (
                        "require_approval"
                        if requires_approval
                        else "allow"
                        if error is None
                        else "deny"
                    ),
                    "tool": call.name if call is not None else None,
                    "risk_level": spec.risk_level if spec else None,
                },
            ).model_dump(mode="json")
        )
        if error is not None:
            return {
                "status": "failed",
                "last_error": error,
                "steps": steps,
            }
        return {
            "steps": steps,
            "approval_required": requires_approval,
            "approval": approval,
        }

    @staticmethod
    def _route_after_policy(state: AgentGraphState) -> str:
        if state["status"] == "failed":
            return "fail"
        return "approval" if state["approval_required"] else "execute_tool"

    def _approval(self, state: AgentGraphState) -> dict[str, Any]:
        approval = state["approval"]
        if approval is None:
            return {
                "status": "failed",
                "last_error": "审批请求缺失，已停止工具执行。",
            }
        decision = interrupt(approval)
        approved = bool(
            decision.get("approved")
            if isinstance(decision, dict)
            else decision
        )
        actor_id = (
            str(decision.get("actor_id", "unknown"))
            if isinstance(decision, dict)
            else "unknown"
        )
        steps = list(state["steps"])
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="approval",
                label="处理人工审批",
                detail=(
                    f"审批人 {actor_id} 已批准工具执行。"
                    if approved
                    else f"审批人 {actor_id} 拒绝了工具执行。"
                ),
                duration_ms=0,
                output={
                    "approved": approved,
                    "actor_id": actor_id,
                    "tool": approval.get("tool"),
                },
            ).model_dump(mode="json")
        )
        if not approved:
            return {
                "status": "failed",
                "last_error": "用户拒绝了高风险工具执行。",
                "approval_required": False,
                "approval": None,
                "steps": steps,
                "started_at": time.time(),
            }
        return {
            "status": "running",
            "approval_required": False,
            "approval": None,
            "steps": steps,
            "started_at": time.time(),
        }

    @staticmethod
    def _route_after_approval(state: AgentGraphState) -> str:
        return "fail" if state["status"] == "failed" else "execute_tool"

    def _execute_tool(self, state: AgentGraphState) -> dict[str, Any]:
        call = ToolCall.model_validate(state["pending_calls"][0])
        started = time.perf_counter()
        context = ToolExecutionContext(**state["execution_context"])
        spec = self.registry.get_spec(call.name)
        idempotency_key = self._idempotency_key(state, call)
        context = replace(context, idempotency_key=idempotency_key)
        ledger_action, ledger_output = self._ledger.prepare(
            idempotency_key=idempotency_key,
            run_id=state["run_id"],
            tool_name=call.name,
            retry_safe=spec.idempotent,
        )
        ok = True
        replayed = ledger_action == "cached"
        if ledger_action == "cached":
            output = ledger_output or {}
        elif ledger_action == "blocked":
            ok = False
            output = ledger_output or {
                "error": "工具执行状态不明确，已停止自动重试。",
                "error_type": "ambiguous_side_effect",
            }
        else:
            try:
                output = self.registry.execute(
                    call.name,
                    call.arguments,
                    context=context,
                )
                self._ledger.complete(idempotency_key, output)
            except ToolError as exc:
                ok = False
                output = {
                    "error": str(exc),
                    "error_type": "tool_error",
                }
                self._ledger.fail(idempotency_key, str(exc))
            except Exception:
                LOGGER.exception("Unexpected tool failure: %s", call.name)
                ok = False
                output = {
                    "error": "工具执行过程中发生内部错误。",
                    "error_type": "internal_error",
                }
                self._ledger.fail(idempotency_key, str(output["error"]))

        observation = {
            "call": call.model_dump(mode="json"),
            "output": output,
            "ok": ok,
        }
        steps = list(state["steps"])
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="tool",
                label=f"调用 {call.name}",
                detail=(
                    (
                        "命中幂等执行记录，复用此前工具结果。"
                        if replayed
                        else "参数通过校验，工具执行完成。"
                    )
                    if ok
                    else f"工具执行失败：{output['error']}"
                ),
                duration_ms=_elapsed_ms(started),
                output=output,
            ).model_dump(mode="json")
        )
        return {
            "pending_calls": list(state["pending_calls"][1:]),
            "round_observations": [
                *state["round_observations"],
                observation,
            ],
            "observations": [*state["observations"], observation],
            "steps": steps,
            "step_count": state["step_count"] + 1,
            "consecutive_errors": (
                0 if ok else state["consecutive_errors"] + 1
            ),
            "last_error": None if ok else str(output["error"]),
        }

    def _observe(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        steps = list(state["steps"])
        error = self._runtime_error(state)
        if (
            error is None
            and state["consecutive_errors"] >= self.max_consecutive_errors
        ):
            error = (
                "连续工具错误达到上限 "
                f"({self.max_consecutive_errors})。"
            )

        has_pending = bool(state["pending_calls"])
        detail = (
            f"观察到失败并安全停止：{error}"
            if error
            else (
                "当前批次还有待执行工具，返回策略检查。"
                if has_pending
                else "本轮工具执行完成，交给决策节点判断是否继续。"
            )
        )
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="decision",
                label="观察工具结果",
                detail=detail,
                duration_ms=_elapsed_ms(started),
                output={
                    "pending_tools": len(state["pending_calls"]),
                    "step_count": state["step_count"],
                    "consecutive_errors": state["consecutive_errors"],
                },
            ).model_dump(mode="json")
        )
        if error:
            return {
                "status": "failed",
                "last_error": error,
                "steps": steps,
            }
        return {"steps": steps}

    @staticmethod
    def _route_after_observe(state: AgentGraphState) -> str:
        if state["status"] == "failed":
            return "fail"
        return "policy" if state["pending_calls"] else "decide"

    def _decide(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        error = self._runtime_error(state)
        if error:
            return {
                "status": "failed",
                "last_error": error,
            }

        current_plan = self._deserialize_plan(state["plan"])
        round_observations = self._deserialize_observations(
            state["round_observations"]
        )
        async_handoff = self._async_job_handoff(round_observations)
        if async_handoff:
            next_plan = PlanningResult(
                tool_calls=[],
                direct_answer=async_handoff,
            )
        else:
            continue_plan = getattr(self.planner, "continue_plan", None)
            if callable(continue_plan):
                next_plan = continue_plan(
                    state["message"],
                    current_plan,
                    round_observations,
                    self._selected_tool_schemas(state),
                )
            else:
                next_plan = PlanningResult(
                    tool_calls=[],
                    direct_answer=self.planner.compose_answer(
                        state["message"],
                        current_plan,
                        round_observations,
                    ),
                )

        if next_plan.tool_calls and state["replan_count"] >= self.max_replans:
            return {
                "status": "failed",
                "last_error": (
                    f"重新规划次数达到上限 ({self.max_replans})。"
                ),
            }

        answer = next_plan.direct_answer or ""
        if not next_plan.tool_calls and not answer:
            answer = self.planner.compose_answer(
                state["message"],
                current_plan,
                round_observations,
            )

        steps = list(state["steps"])
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="decision",
                label="判断下一步",
                detail=(
                    "异步生成任务已创建，已交给任务监控，不在 Agent 循环内轮询。"
                    if async_handoff
                    else (
                        "工具结果仍不足，模型生成了下一轮工具调用。"
                        if next_plan.tool_calls
                        else "模型判断目标已经完成，进入最终回答。"
                    )
                ),
                duration_ms=_elapsed_ms(started),
                output={
                    "decision": (
                        "async_handoff"
                        if async_handoff
                        else (
                            "replan" if next_plan.tool_calls else "complete"
                        )
                    ),
                    "next_tools": [
                        call.name for call in next_plan.tool_calls
                    ],
                },
            ).model_dump(mode="json")
        )
        return {
            "plan": self._serialize_plan(next_plan),
            "pending_calls": [
                call.model_dump(mode="json")
                for call in next_plan.tool_calls
            ],
            "round_observations": [],
            "answer": answer,
            "status": (
                "running" if next_plan.tool_calls else "completed"
            ),
            "replan_count": (
                state["replan_count"] + 1
                if next_plan.tool_calls
                else state["replan_count"]
            ),
            "steps": steps,
        }

    @staticmethod
    def _async_job_handoff(
        observations: list[ToolObservation],
    ) -> str | None:
        messages = {
            "create_spatial_scene": (
                "空间照片任务已创建，正在本机后台处理。完成后可在当前对话或"
                "个人资产库查看结果。"
            ),
            "create_photo_style_transfer": (
                "图片风格化任务已创建，正在本机后台处理。完成后可在当前对话或"
                "个人资产库查看并下载结果。"
            ),
        }
        for observation in reversed(observations):
            answer = messages.get(observation.call.name)
            if (
                answer
                and observation.output.get("job_id")
                and observation.output.get("asset_id")
                and not observation.output.get("error")
            ):
                return answer
        return None

    @staticmethod
    def _route_after_decide(state: AgentGraphState) -> str:
        if state["status"] == "failed":
            return "fail"
        return "policy" if state["pending_calls"] else "finalize"

    def _finalize(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        answer = state["answer"].strip()
        if not answer:
            plan = self._deserialize_plan(state["plan"])
            answer = plan.direct_answer or "当前工具无法完成这个任务。"
        steps = list(state["steps"])
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="final",
                label="生成最终回答",
                detail=(
                    "循环已达到完成状态，返回经过工具结果支撑的回答。"
                ),
                duration_ms=_elapsed_ms(started),
            ).model_dump(mode="json")
        )
        return {
            "answer": answer,
            "status": "completed",
            "steps": steps,
        }

    def _fail(self, state: AgentGraphState) -> dict[str, Any]:
        started = time.perf_counter()
        reason = state["last_error"] or "运行未能安全完成。"
        steps = list(state["steps"])
        steps.append(
            TraceStep(
                index=len(steps) + 1,
                stage="final",
                label="安全停止",
                detail=reason,
                duration_ms=_elapsed_ms(started),
                output={"reason": reason},
            ).model_dump(mode="json")
        )
        return {
            "answer": f"任务已安全停止：{reason}",
            "status": "failed",
            "steps": steps,
        }

    def _hard_boundary_error(
        self,
        state: AgentGraphState,
    ) -> str | None:
        runtime_error = self._runtime_error(state)
        if runtime_error:
            return runtime_error
        if state["step_count"] >= self.max_steps:
            return f"工具执行步数达到上限 ({self.max_steps})。"
        return None

    def _runtime_error(self, state: AgentGraphState) -> str | None:
        elapsed = max(0.0, time.time() - state["started_at"])
        if elapsed >= self.max_runtime_seconds:
            return (
                "运行时间达到上限 "
                f"({self.max_runtime_seconds:g} 秒)。"
            )
        return None

    @staticmethod
    def _serialize_plan(plan: PlanningResult) -> dict[str, Any]:
        return {
            "tool_calls": [
                call.model_dump(mode="json") for call in plan.tool_calls
            ],
            "direct_answer": plan.direct_answer,
            "provider_context": plan.provider_context,
        }

    @staticmethod
    def _deserialize_plan(raw_plan: dict[str, Any]) -> PlanningResult:
        return PlanningResult(
            tool_calls=[
                ToolCall.model_validate(item)
                for item in raw_plan.get("tool_calls", [])
            ],
            direct_answer=raw_plan.get("direct_answer"),
            provider_context=raw_plan.get("provider_context"),
        )

    @staticmethod
    def _deserialize_observations(
        raw_observations: list[dict[str, Any]],
    ) -> list[ToolObservation]:
        return [
            ToolObservation(
                call=ToolCall.model_validate(item["call"]),
                output=item["output"],
            )
            for item in raw_observations
        ]

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._recursion_limit,
        }

    def _selected_tool_schemas(
        self,
        state: AgentGraphState,
    ) -> list[dict[str, Any]]:
        names = state.get("selected_tool_names")
        return self.registry.openai_schemas(
            tuple(names) if names is not None else None
        )

    @staticmethod
    def _with_interrupt(raw: dict[str, Any]) -> AgentGraphState:
        result = dict(raw)
        interrupts = result.pop("__interrupt__", [])
        if interrupts:
            result["status"] = "waiting_approval"
            result["approval"] = interrupts[0].value
        return result  # type: ignore[return-value]

    @staticmethod
    def _idempotency_key(
        state: AgentGraphState,
        call: ToolCall,
    ) -> str:
        canonical = json.dumps(
            {
                "run_id": state["run_id"],
                "step_count": state["step_count"],
                "call": call.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
