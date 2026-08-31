from __future__ import annotations

import argparse
import gc
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.app.memory import SQLiteMemoryStore  # noqa: E402


DEFAULT_CASES = REPOSITORY_ROOT / "backend/tests/fixtures/memory_eval_cases.json"


def evaluate(cases_path: Path, *, limit: int = 5) -> dict[str, Any]:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = fixture.get("cases", [])
    case_results: list[dict[str, Any]] = []
    expected_total = 0
    recalled_total = 0
    leakage_total = 0
    abstention_total = 0
    abstention_success = 0
    evidence_expectation_total = 0
    evidence_expectation_success = 0
    retrieval_latencies_ms: list[float] = []

    with tempfile.TemporaryDirectory(prefix="agent-memory-eval-") as temp_dir:
        store = SQLiteMemoryStore(
            Path(temp_dir) / "memory.sqlite3",
            semantic_enabled=True,
        )
        for index, case in enumerate(cases):
            owner_id = f"eval:{index}:{case['id']}"
            for memory_index, memory in enumerate(case.get("memories", [])):
                store.remember(
                    owner_id=owner_id,
                    content=str(memory["content"]),
                    source=f"eval:{case['id']}:{memory_index}",
                    memory_type=memory.get("memory_type"),
                    sensitivity=memory.get("sensitivity"),
                    retrieval_policy=memory.get("retrieval_policy"),
                )
            started = time.perf_counter()
            selected = store.search_memories(
                owner_id=owner_id,
                query=str(case["query"]),
                limit=limit,
            )
            retrieval_latencies_ms.append(
                (time.perf_counter() - started) * 1_000
            )
            found = [item.content for item in selected]
            expected = [str(item) for item in case.get("expected_contains", [])]
            forbidden = [str(item) for item in case.get("expected_excludes", [])]
            recalled = sum(item in found for item in expected)
            leaked = [item for item in forbidden if item in found]
            expected_evidence = {
                str(content): int(count)
                for content, count in case.get("expected_min_evidence", {}).items()
            }
            evidence_by_content = {
                item.content: item.evidence_count for item in selected
            }
            missing_evidence = {
                content: {
                    "expected": minimum,
                    "found": evidence_by_content.get(content, 0),
                }
                for content, minimum in expected_evidence.items()
                if evidence_by_content.get(content, 0) < minimum
            }
            evidence_expectation_total += len(expected_evidence)
            evidence_expectation_success += len(expected_evidence) - len(
                missing_evidence
            )
            expected_total += len(expected)
            recalled_total += recalled
            leakage_total += len(leaked)
            if not expected:
                abstention_total += 1
                abstention_success += int(not found)
            case_results.append(
                {
                    "id": case["id"],
                    "passed": (
                        recalled == len(expected)
                        and not leaked
                        and not missing_evidence
                    ),
                    "found": found,
                    "missing": [item for item in expected if item not in found],
                    "leaked": leaked,
                    "evidence_counts": evidence_by_content,
                    "missing_evidence": missing_evidence,
                }
            )
        del store
        gc.collect()

    sorted_latencies = sorted(retrieval_latencies_ms)
    p95_index = max(0, math.ceil(len(sorted_latencies) * 0.95) - 1)
    return {
        "version": fixture.get("version", 1),
        "case_count": len(case_results),
        "passed_cases": sum(item["passed"] for item in case_results),
        "recall_at_k": recalled_total / max(1, expected_total),
        "leakage_count": leakage_total,
        "abstention_accuracy": abstention_success / max(1, abstention_total),
        "evidence_accuracy": (
            evidence_expectation_success / max(1, evidence_expectation_total)
        ),
        "latency_ms": {
            "p50": round(statistics.median(sorted_latencies), 3)
            if sorted_latencies
            else 0.0,
            "p95": round(sorted_latencies[p95_index], 3)
            if sorted_latencies
            else 0.0,
        },
        "cases": case_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic long-term memory retrieval evaluations."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--minimum-recall", type=float, default=1.0)
    args = parser.parse_args()
    report = evaluate(args.cases, limit=max(1, min(args.limit, 20)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(
        report["recall_at_k"] < args.minimum_recall
        or report["leakage_count"] > 0
        or report["passed_cases"] < report["case_count"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
