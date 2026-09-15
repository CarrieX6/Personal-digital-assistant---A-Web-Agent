from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol


LOGGER = logging.getLogger(__name__)


class TokenizerConfigurationError(ValueError):
    """Raised when a required tokenizer cannot be loaded safely."""


class ContextWindowExceededError(ValueError):
    """Raised before transport when a complete request cannot fit the window."""


@dataclass(frozen=True)
class RequestTokenGateResult:
    serialized_byte_upper_bound: int
    reserved_output_tokens: int
    context_window_tokens: int
    inline_image_count: int = 0
    estimated_vision_tokens: int = 0


_INLINE_IMAGE_PREFIXES = (
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/webp;base64,",
)
_TOKENS_PER_INLINE_IMAGE = 8_192


def _redact_inline_images(value: object) -> tuple[object, int]:
    """Remove transport-only base64 bytes from a model context estimate.

    Vision APIs decode data URLs before constructing model tokens. Counting the
    base64 characters as text rejects ordinary images even though they fit the
    model context. A deliberately conservative fixed reserve still accounts for
    visual tokens while keeping unrelated large strings fully covered.
    """

    if isinstance(value, list):
        items: list[object] = []
        count = 0
        for item in value:
            redacted, nested_count = _redact_inline_images(item)
            items.append(redacted)
            count += nested_count
        return items, count
    if isinstance(value, dict):
        items: dict[object, object] = {}
        count = 0
        for key, item in value.items():
            if (
                key == "image_url"
                and value.get("type") == "image_url"
                and isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and item["url"].startswith(_INLINE_IMAGE_PREFIXES)
            ):
                media_type = item["url"][5 : item["url"].index(";base64,")]
                items[key] = {
                    **item,
                    "url": f"[inline-{media_type}-payload]",
                }
                count += 1
                continue
            redacted, nested_count = _redact_inline_images(item)
            items[key] = redacted
            count += nested_count
        return items, count
    return value, 0


def enforce_request_token_gate(
    payload: dict[str, object],
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
) -> RequestTokenGateResult:
    """Reject a request unless a serialization-level upper bound fits.

    The ASCII-escaped JSON byte count is deliberately conservative for textual
    content: common subword tokenizers cannot emit more content tokens than the
    bytes supplied, and counting the entire body also covers wrapper overhead.
    Inline image base64 is transport data rather than text tokens, so it is
    replaced by a marker and charged a conservative visual-token reserve.
    """

    gate_payload, inline_image_count = _redact_inline_images(payload)
    serialized = json.dumps(
        gate_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    estimated_vision_tokens = inline_image_count * _TOKENS_PER_INLINE_IMAGE
    upper_bound = len(serialized) + 64 + estimated_vision_tokens
    total = upper_bound + max(0, int(reserved_output_tokens))
    if total > int(context_window_tokens):
        raise ContextWindowExceededError(
            "完整模型请求超过上下文窗口："
            f"输入上界 {upper_bound} + 输出预留 {reserved_output_tokens} "
            f"> 窗口 {context_window_tokens}。"
        )
    return RequestTokenGateResult(
        serialized_byte_upper_bound=upper_bound,
        reserved_output_tokens=int(reserved_output_tokens),
        context_window_tokens=int(context_window_tokens),
        inline_image_count=inline_image_count,
        estimated_vision_tokens=estimated_vision_tokens,
    )


class TokenCounter(Protocol):
    identifier: str

    def count(self, value: str) -> int: ...


class ConservativeTokenCounter:
    """Deterministic fallback used only when no real tokenizer is available."""

    identifier = "heuristic:utf8-bytes-v1"

    def count(self, value: str) -> int:
        if not value:
            return 0
        return max(1, math.ceil(len(value.encode("utf-8")) / 3))


class LocalTransformersTokenCounter:
    """Strictly local Hugging Face tokenizer used without loading model weights."""

    def __init__(self, model_path: Path) -> None:
        resolved_path = model_path.expanduser().resolve()
        if not resolved_path.is_dir():
            raise TokenizerConfigurationError("本地 Tokenizer 目录不存在。")
        try:
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                str(resolved_path),
                local_files_only=True,
                trust_remote_code=False,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise TokenizerConfigurationError(
                "无法加载本地 Tokenizer；不会从网络下载或执行远程代码。"
            ) from exc
        self.path = resolved_path
        self.identifier = f"transformers:{resolved_path.name}"
        self._tokenizer = tokenizer
        self._lock = threading.RLock()

    def count(self, value: str) -> int:
        if not value:
            return 0
        with self._lock:
            token_ids = self._tokenizer.encode(
                value,
                add_special_tokens=False,
            )
        return len(token_ids)


def get_default_token_counter() -> TokenCounter:
    backend = os.getenv("AGENT_TOKENIZER_BACKEND", "auto").strip().lower()
    raw_path = (
        os.getenv("AGENT_TOKENIZER_PATH", "").strip()
        or os.getenv("AGENT_MEMORY_EMBEDDING_MODEL_PATH", "").strip()
    )
    return _build_token_counter(backend, raw_path)


@lru_cache(maxsize=8)
def _build_token_counter(backend: str, raw_path: str) -> TokenCounter:
    if backend not in {"auto", "transformers", "heuristic"}:
        raise TokenizerConfigurationError(
            "AGENT_TOKENIZER_BACKEND 只能是 auto、transformers 或 heuristic。"
        )
    if backend == "heuristic":
        return ConservativeTokenCounter()
    if raw_path:
        try:
            return LocalTransformersTokenCounter(Path(raw_path))
        except TokenizerConfigurationError:
            if backend == "transformers":
                raise
            LOGGER.warning(
                "Local tokenizer is unavailable; falling back to conservative "
                "token estimates."
            )
    elif backend == "transformers":
        raise TokenizerConfigurationError(
            "AGENT_TOKENIZER_BACKEND=transformers 时必须配置 AGENT_TOKENIZER_PATH。"
        )
    return ConservativeTokenCounter()


def clip_text_to_tokens(
    value: str,
    token_budget: int,
    *,
    token_counter: TokenCounter,
    preserve_tail: bool = False,
) -> tuple[str, bool]:
    """Clip source text against the selected tokenizer without byte slicing."""

    if token_counter.count(value) <= token_budget:
        return value, False
    if token_budget <= 0:
        return "", bool(value)

    marker = "\n…[中间内容已截断]…\n" if preserve_tail else "…"
    marker_cost = token_counter.count(marker)
    if marker_cost >= token_budget:
        return _longest_prefix(value, token_budget, token_counter), True

    if not preserve_tail:
        clipped = _longest_prefix(
            value,
            token_budget,
            token_counter,
            suffix=marker,
        )
        return clipped, True

    available = token_budget - marker_cost
    head_budget = max(1, available // 2)
    tail_budget = max(1, available - head_budget)
    head = _longest_prefix(value, head_budget, token_counter)
    tail = _longest_suffix(value[len(head) :], tail_budget, token_counter)
    candidate = head.rstrip() + marker + tail.lstrip()
    if token_counter.count(candidate) <= token_budget:
        return candidate, True

    # Token boundaries can merge across concatenated fragments. Scale both
    # source spans together until the final, exact count fits.
    low = 0
    high = min(len(head), len(tail)) if head and tail else max(len(head), len(tail))
    best = marker if marker_cost <= token_budget else ""
    while low <= high:
        size = (low + high) // 2
        scaled_head = head[:size] if head else ""
        scaled_tail = tail[-size:] if tail else ""
        candidate = scaled_head.rstrip() + marker + scaled_tail.lstrip()
        if token_counter.count(candidate) <= token_budget:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best, True


def _longest_prefix(
    value: str,
    token_budget: int,
    token_counter: TokenCounter,
    *,
    suffix: str = "",
) -> str:
    low = 0
    high = len(value)
    best = suffix if suffix and token_counter.count(suffix) <= token_budget else ""
    while low <= high:
        index = (low + high) // 2
        candidate = value[:index].rstrip() + suffix
        if token_counter.count(candidate) <= token_budget:
            best = candidate
            low = index + 1
        else:
            high = index - 1
    return best


def _longest_suffix(
    value: str,
    token_budget: int,
    token_counter: TokenCounter,
) -> str:
    low = 0
    high = len(value)
    best = ""
    while low <= high:
        size = (low + high) // 2
        candidate = value[-size:] if size else ""
        if token_counter.count(candidate) <= token_budget:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best
