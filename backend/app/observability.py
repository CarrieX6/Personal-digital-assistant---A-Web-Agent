from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


class MetricsRegistry:
    """Small process-local metrics registry safe for worker threads.

    Values are deliberately aggregate-only: request bodies, prompts, image bytes,
    and credentials are never put into the registry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._durations: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "sum_ms": 0.0, "max_ms": 0.0}
        )

    def increment(self, name: str, value: float = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def observe_ms(self, name: str, value: float) -> None:
        with self._lock:
            summary = self._durations[name]
            summary["count"] += 1
            summary["sum_ms"] += max(0.0, value)
            summary["max_ms"] = max(summary["max_ms"], value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = {key: int(value) for key, value in self._counters.items()}
            durations = {
                key: {
                    "count": int(value["count"]),
                    "sum_ms": round(value["sum_ms"], 2),
                    "max_ms": round(value["max_ms"], 2),
                    "avg_ms": round(
                        value["sum_ms"] / value["count"], 2
                    )
                    if value["count"]
                    else 0,
                }
                for key, value in self._durations.items()
            }
        return {"counters": counters, "durations": durations}


class JsonLogFormatter(logging.Formatter):
    """Structured logs without serialising exception secrets or request bodies."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Opt into JSON logs for deployments; keep library defaults untouched locally."""
    if os.getenv("AGENT_LOG_FORMAT", "text").strip().lower() != "json":
        return
    root = logging.getLogger()
    root.setLevel(os.getenv("AGENT_LOG_LEVEL", "INFO").upper())
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    formatter = JsonLogFormatter()
    for handler in root.handlers:
        handler.setFormatter(formatter)


def monotonic_ms() -> float:
    return time.perf_counter() * 1000
