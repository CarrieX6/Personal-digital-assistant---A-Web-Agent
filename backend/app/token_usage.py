from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class TokenBudgetExceeded(RuntimeError):
    """Raised before a model request would exceed the current Run budget."""


@dataclass
class TokenUsageContext:
    owner_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    channel: str | None = None
    project_id: str | None = None
    stage: str = "unknown"
    max_total_tokens: int | None = None
    spent_tokens: int = 0


@dataclass(frozen=True)
class TokenUsageSummary:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    actual_calls: int = 0
    estimated_calls: int = 0
    latency_ms: int = 0
    max_budget_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "actual_calls": self.actual_calls,
            "estimated_calls": self.estimated_calls,
            "latency_ms": self.latency_ms,
            "max_budget_tokens": self.max_budget_tokens,
        }


_USAGE_CONTEXT: ContextVar[TokenUsageContext | None] = ContextVar(
    "agent_token_usage_context", default=None
)


def current_usage_context() -> TokenUsageContext | None:
    return _USAGE_CONTEXT.get()


@contextmanager
def use_usage_context(context: TokenUsageContext) -> Iterator[TokenUsageContext]:
    """Reuse one Run budget across context summarization and graph calls."""

    token = _USAGE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _USAGE_CONTEXT.reset(token)


@contextmanager
def bind_usage_context(
    *,
    owner_id: str | None,
    run_id: str | None,
    thread_id: str | None,
    channel: str | None,
    project_id: str | None,
    max_total_tokens: int | None = None,
    stage: str = "agent",
) -> Iterator[TokenUsageContext]:
    context = TokenUsageContext(
        owner_id=owner_id,
        run_id=run_id,
        thread_id=thread_id,
        channel=channel,
        project_id=project_id,
        stage=stage,
        max_total_tokens=max_total_tokens,
    )
    token = _USAGE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _USAGE_CONTEXT.reset(token)


@contextmanager
def bind_usage_stage(stage: str) -> Iterator[None]:
    context = current_usage_context()
    if context is None:
        yield
        return
    previous = context.stage
    context.stage = stage
    try:
        yield
    finally:
        context.stage = previous


def configured_run_token_budget() -> int | None:
    """Read an opt-in per-run budget; zero or invalid values disable the gate."""

    try:
        value = int(os.getenv("AGENT_RUN_TOKEN_BUDGET", "0"))
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else None


def reserve_token_budget(estimated_tokens: int) -> None:
    context = current_usage_context()
    if context is None or context.max_total_tokens is None:
        return
    estimated_tokens = max(0, int(estimated_tokens))
    projected = context.spent_tokens + estimated_tokens
    if projected > context.max_total_tokens:
        raise TokenBudgetExceeded(
            "本次 Agent 运行预计超过 Token 预算："
            f"已用 {context.spent_tokens}，本次最多 {estimated_tokens}，"
            f"预算 {context.max_total_tokens}。"
        )


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_provider_usage(usage: Any) -> tuple[int, int, int] | None:
    """Normalize common OpenAI-compatible usage field names."""

    if not isinstance(usage, dict):
        return None
    input_tokens = _number(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = _number(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    total_tokens = _number(usage.get("total_tokens")) or (
        input_tokens + output_tokens
    )
    if not total_tokens and not input_tokens and not output_tokens:
        return None
    return input_tokens, output_tokens, total_tokens


class TokenUsageStore:
    """Durable, content-free token telemetry for Run-level cost control."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(path), check_same_thread=False, timeout=30
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT,
                run_id TEXT,
                thread_id TEXT,
                channel TEXT,
                project_id TEXT,
                stage TEXT NOT NULL,
                provider TEXT,
                model TEXT,
                tokenizer TEXT,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                estimated_input_tokens INTEGER NOT NULL,
                estimated_output_tokens INTEGER NOT NULL,
                is_estimate INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_token_usage_run
                ON token_usage(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_token_usage_owner
                ON token_usage(owner_id, created_at);
            """
        )
        self._connection.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def record(
        self,
        *,
        provider: str | None,
        model: str | None,
        tokenizer: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        is_estimate: bool,
        estimated_input_tokens: int | None = None,
        estimated_output_tokens: int | None = None,
        latency_ms: int = 0,
        context: TokenUsageContext | None = None,
    ) -> None:
        context = context or current_usage_context()
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        total_tokens = max(0, int(total_tokens or input_tokens + output_tokens))
        estimated_input_tokens = max(
            0,
            int(input_tokens if estimated_input_tokens is None else estimated_input_tokens),
        )
        estimated_output_tokens = max(
            0,
            int(output_tokens if estimated_output_tokens is None else estimated_output_tokens),
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO token_usage (
                    owner_id, run_id, thread_id, channel, project_id, stage,
                    provider, model, tokenizer, input_tokens, output_tokens,
                    total_tokens, estimated_input_tokens, estimated_output_tokens,
                    is_estimate, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.owner_id if context else None,
                    context.run_id if context else None,
                    context.thread_id if context else None,
                    context.channel if context else None,
                    context.project_id if context else None,
                    context.stage if context else "unknown",
                    provider,
                    model,
                    tokenizer,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    estimated_input_tokens,
                    estimated_output_tokens,
                    int(is_estimate),
                    max(0, int(latency_ms)),
                    time.time(),
                ),
            )
            self._connection.commit()
        if context is not None:
            context.spent_tokens += total_tokens

    def summary(
        self,
        *,
        run_id: str | None = None,
        owner_id: str | None = None,
    ) -> TokenUsageSummary:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            values.append(owner_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._connection.execute(
                f"""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(estimated_input_tokens), 0) AS estimated_input_tokens,
                       COALESCE(SUM(estimated_output_tokens), 0) AS estimated_output_tokens,
                       COALESCE(SUM(CASE WHEN is_estimate = 0 THEN 1 ELSE 0 END), 0) AS actual_calls,
                       COALESCE(SUM(CASE WHEN is_estimate = 1 THEN 1 ELSE 0 END), 0) AS estimated_calls,
                       COALESCE(SUM(latency_ms), 0) AS latency_ms
                  FROM token_usage{where}
                """,
                values,
            ).fetchone()
        return TokenUsageSummary(
            calls=_number(row["calls"]),
            input_tokens=_number(row["input_tokens"]),
            output_tokens=_number(row["output_tokens"]),
            total_tokens=_number(row["total_tokens"]),
            estimated_input_tokens=_number(row["estimated_input_tokens"]),
            estimated_output_tokens=_number(row["estimated_output_tokens"]),
            actual_calls=_number(row["actual_calls"]),
            estimated_calls=_number(row["estimated_calls"]),
            latency_ms=_number(row["latency_ms"]),
        )

    def events(
        self,
        *,
        run_id: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            values.append(owner_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT stage, provider, model, tokenizer, input_tokens,
                       output_tokens, total_tokens, is_estimate, latency_ms,
                       created_at
                  FROM token_usage{where}
              ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def estimate_request_tokens(serialized_byte_upper_bound: int, output_limit: int) -> int:
    """Conservative request estimate used only for the optional Run budget."""

    return max(0, int(serialized_byte_upper_bound)) + max(0, int(output_limit))


def json_size_tokens(value: Any) -> int:
    """Small deterministic fallback for providers that omit ``usage``."""

    return max(
        1,
        len(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        )
        // 3,
    )
