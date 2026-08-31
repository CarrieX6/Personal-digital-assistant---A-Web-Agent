from __future__ import annotations

from pathlib import Path

import pytest
import transformers

from backend.app.tokenization import (
    ContextWindowExceededError,
    LocalTransformersTokenCounter,
    clip_text_to_tokens,
    enforce_request_token_gate,
)


def test_local_transformers_counter_is_offline_and_drives_exact_clipping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeTokenizer:
        def encode(self, value: str, *, add_special_tokens: bool) -> list[int]:
            assert add_special_tokens is False
            return list(range(len(value)))

    def load(path: str, **kwargs):
        calls.append((path, kwargs))
        return FakeTokenizer()

    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", load)
    counter = LocalTransformersTokenCounter(tmp_path)

    assert counter.count("中文 Agent") == len("中文 Agent")
    assert calls == [
        (
            str(tmp_path.resolve()),
            {"local_files_only": True, "trust_remote_code": False},
        )
    ]

    clipped, was_clipped = clip_text_to_tokens(
        "前置内容" * 20 + "最终要求：保留结尾",
        30,
        token_counter=counter,
        preserve_tail=True,
    )
    assert was_clipped is True
    assert clipped.endswith("最终要求：保留结尾")
    assert counter.count(clipped) <= 30


def test_complete_request_gate_blocks_before_transport_window_overflow() -> None:
    with pytest.raises(ContextWindowExceededError, match="超过上下文窗口"):
        enforce_request_token_gate(
            {
                "model": "test",
                "messages": [{"role": "user", "content": "x" * 2_000}],
                "tools": [],
            },
            context_window_tokens=2_048,
            reserved_output_tokens=256,
        )

    result = enforce_request_token_gate(
        {"model": "test", "messages": [{"role": "user", "content": "ok"}]},
        context_window_tokens=2_048,
        reserved_output_tokens=256,
    )
    assert result.serialized_byte_upper_bound + 256 <= 2_048
