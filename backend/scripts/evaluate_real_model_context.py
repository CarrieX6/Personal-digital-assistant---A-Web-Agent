from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.app.context import ContextBuilder  # noqa: E402
from backend.app.llm import (  # noqa: E402
    SYSTEM_PROMPT,
    OpenAICompatiblePlanner,
)
from backend.app.memory import SQLiteMemoryStore  # noqa: E402


def _turn_messages(turn: int, code: str) -> list[tuple[str, str]]:
    if turn == 0:
        user = f"长期验收事实：客户最终批准的验收代号是 {code}。"
    elif turn == 1:
        user = "长期验收决定：本任务继续使用 SQLite 作为持久化存储。"
    elif turn == 2:
        user = "长期验收待办：完成崩溃恢复矩阵后提交最终报告。"
    elif turn % 97 == 0:
        user = f"状态记录：第 {turn} 轮检查完成，既有决定与待办保持不变。"
    else:
        user = f"普通多轮交互 {turn}：继续当前长链任务，不新增事实。"
    return [("user", user), ("assistant", "收到，继续当前任务。")]


def evaluate_session(
    *,
    planner: OpenAICompatiblePlanner,
    turn_count: int,
    batch_turns: int,
    root: Path,
) -> dict[str, Any]:
    code = f"HT-{turn_count}-Z9"
    owner_id = f"real-model-owner-{turn_count}"
    thread_id = f"web:real-model-{turn_count}"
    database_path = root / f"real-model-{turn_count}.sqlite3"
    store = SQLiteMemoryStore(database_path, summary_provider=planner)
    summary_calls = 0
    started_at = time.monotonic()
    for batch_start in range(0, turn_count, batch_turns):
        messages: list[tuple[str, str]] = []
        for turn in range(
            batch_start,
            min(turn_count, batch_start + batch_turns),
        ):
            messages.extend(_turn_messages(turn, code))
        store.append_messages_batch(
            owner_id=owner_id,
            thread_id=thread_id,
            channel="web",
            project_id=f"project-{turn_count}",
            messages=messages,
        )
        summary = store.get_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
        )
        if summary is not None and summary.provider_id == planner.summary_provider_id:
            summary_calls += 1
        completed_turns = min(turn_count, batch_start + batch_turns)
        print(
            f"progress turns={completed_turns}/{turn_count} "
            f"summary_provider={summary.provider_id if summary else 'none'} "
            f"elapsed_seconds={time.monotonic() - started_at:.1f}",
            file=sys.stderr,
            flush=True,
        )

    summary = store.get_session_summary(owner_id=owner_id, thread_id=thread_id)
    provenance = store.verify_session_summary_provenance(
        owner_id=owner_id,
        thread_id=thread_id,
    )
    context = store.context(
        owner_id=owner_id,
        thread_id=thread_id,
        channel="web",
        project_id=f"project-{turn_count}",
        query="客户最终批准的验收代号是什么？",
        message_limit=20,
    )
    builder = ContextBuilder(system_prompt=SYSTEM_PROMPT)
    built = builder.build(
        current_user_message="客户最终批准的验收代号是什么？只回答代号。",
        conversation_messages=context.messages,
        memories=[item.to_context_dict() for item in context.memory_items],
        session_summary=context.session_summary,
        open_loops=context.open_loops,
        decisions=context.decisions,
        completed_actions=context.completed_actions,
        active_assumptions=context.active_assumptions,
        artifact_refs=context.artifact_refs,
        blockers=context.blockers,
        next_goal=context.next_goal,
        attachments=[],
        selected_tool_schemas=[],
    )
    answer = planner.plan_with_context(
        built.to_planner_context(),
        [],
    ).direct_answer or ""
    gate = planner.last_request_token_gate
    decision_recalled = any("SQLite" in item for item in context.decisions)
    task_recalled = any("崩溃恢复矩阵" in item for item in context.open_loops)
    passed = bool(
        summary is not None
        and summary.provider_id == planner.summary_provider_id
        and summary.schema_version == "session-summary-v3"
        and code in answer
        and decision_recalled
        and task_recalled
        and provenance["valid"]
        and gate is not None
        and gate.serialized_byte_upper_bound + gate.reserved_output_tokens
        <= gate.context_window_tokens
    )
    result = {
        "turn_count": turn_count,
        "passed": passed,
        "summary_calls": summary_calls,
        "provider_id": summary.provider_id if summary else None,
        "schema_version": summary.schema_version if summary else None,
        "early_fact_answered": code in answer,
        "decision_recalled": decision_recalled,
        "task_recalled": task_recalled,
        "provenance_valid": provenance["valid"],
        "provenance_items": provenance["checked_items"],
        "provenance_sources": provenance["checked_sources"],
        "recent_message_count": len(context.messages),
        "request_input_upper_bound": (
            gate.serialized_byte_upper_bound if gate else None
        ),
        "request_output_reserve": gate.reserved_output_tokens if gate else None,
        "request_context_window": gate.context_window_tokens if gate else None,
    }
    store.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", nargs="+", type=int, default=[500, 1000, 2000])
    parser.add_argument("--batch-turns", type=int, default=100)
    parser.add_argument("--report-file", type=Path)
    args = parser.parse_args()
    load_dotenv(REPOSITORY_ROOT / ".env", override=False)
    os.environ["AGENT_SESSION_SUMMARY_MODE"] = "structured"
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip() or "https://api.openai.com/v1"
    if not api_key or not model:
        raise SystemExit("LLM_MODEL/LLM_API_KEY 未配置，无法执行真实模型验收。")
    timeout = max(120.0, float(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    model_client = httpx.Client(timeout=timeout, trust_env=False)
    planner = OpenAICompatiblePlanner(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout,
        client=model_client,
    )
    with tempfile.TemporaryDirectory(prefix="agent-real-model-context-") as temp:
        results = [
            evaluate_session(
                planner=planner,
                turn_count=max(1, turn_count),
                batch_turns=max(10, args.batch_turns),
                root=Path(temp),
            )
            for turn_count in args.turns
        ]
    report = {
        "model": planner.model_name,
        "passed": all(item["passed"] for item in results),
        "sessions": results,
    }
    rendered_report = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report_file is not None:
        report_path = args.report_file.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_report_path = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary_report_path.write_text(rendered_report + "\n", encoding="utf-8")
        temporary_report_path.replace(report_path)
    print(rendered_report)
    planner.close()
    model_client.close()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
