from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend.app.agent import PlanningResult, ToolObservation
from backend.app.models import ToolCall
from backend.app.orchestration import LangGraphOrchestrator
from backend.app.tools import ToolExecutionContext, ToolRegistry, ToolSpec


class MatrixPlanner:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def plan(self, _message: str, _schemas: list[dict]) -> PlanningResult:
        name = "missing_tool" if self.mode == "failure" else "matrix_tool"
        return PlanningResult(
            tool_calls=[ToolCall(call_id="matrix-call", name=name, arguments={})]
        )

    def continue_plan(
        self,
        _message: str,
        _plan: PlanningResult,
        _observations: list[ToolObservation],
        _schemas: list[dict],
    ) -> PlanningResult:
        return PlanningResult(tool_calls=[], direct_answer="matrix-complete")

    def compose_answer(
        self,
        _message: str,
        _plan: PlanningResult,
        _observations: list[ToolObservation],
    ) -> str:
        return "matrix-complete"


def build_orchestrator(
    path: Path,
    *,
    mode: str,
    crash_node: str | None,
    crash_phase: str | None,
) -> tuple[LangGraphOrchestrator, ToolExecutionContext]:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="matrix_tool",
            description="crash matrix deterministic tool",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=lambda _arguments: {"value": "stable"},
            requires_approval=mode == "approval",
        )
    )

    def failpoint(node: str, phase: str, _state: dict) -> None:
        if node == crash_node and phase == crash_phase:
            os._exit(91)

    return (
        LangGraphOrchestrator(
            planner=MatrixPlanner(mode),
            registry=registry,
            checkpoint_path=path,
            max_steps=4,
            failpoint=failpoint if crash_node else None,
        ),
        ToolExecutionContext(
            owner_id="matrix-owner",
            thread_id="matrix-thread",
            channel="test",
            project_id="matrix-project",
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("invoke", "resume", "recover"))
    parser.add_argument("path", type=Path)
    parser.add_argument("mode", choices=("success", "failure", "approval"))
    parser.add_argument("node", nargs="?", default=None)
    parser.add_argument("phase", nargs="?", default=None)
    args = parser.parse_args()
    orchestrator, context = build_orchestrator(
        args.path,
        mode=args.mode,
        crash_node=args.node,
        crash_phase=args.phase,
    )
    run_id = "matrix-run"
    checkpoint_id = orchestrator.checkpoint_thread_id(context, run_id)
    try:
        if args.action == "invoke":
            state = orchestrator.invoke(
                message="matrix",
                context=context,
                run_id=run_id,
                selected_tool_names=["matrix_tool"],
            )
        elif args.action == "resume":
            state = orchestrator.resume(
                run_id=run_id,
                context=context,
                approved=True,
                actor_id="matrix-actor",
                checkpoint_thread_id=checkpoint_id,
            )
        else:
            snapshot = orchestrator.inspect_checkpoint(
                run_id=run_id,
                context=context,
                checkpoint_thread_id=checkpoint_id,
            )
            if snapshot["waiting_approval"]:
                state = orchestrator.resume(
                    run_id=run_id,
                    context=context,
                    approved=True,
                    actor_id="matrix-actor",
                    checkpoint_thread_id=checkpoint_id,
                )
            elif snapshot["resumable"]:
                state = orchestrator.continue_from_checkpoint(
                    run_id=run_id,
                    context=context,
                    checkpoint_thread_id=checkpoint_id,
                )
            else:
                state = snapshot["state"]
        print(
            json.dumps(
                {
                    "status": state["status"],
                    "answer": state.get("answer", ""),
                    "last_error": state.get("last_error"),
                    "step_count": state.get("step_count", 0),
                },
                ensure_ascii=False,
            )
        )
    finally:
        orchestrator.close()


if __name__ == "__main__":
    main()
