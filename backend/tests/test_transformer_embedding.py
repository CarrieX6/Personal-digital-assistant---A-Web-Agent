from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from backend.app.transformer_embedding import (
    MANIFEST_NAME,
    EmbeddingConfigurationError,
    LocalTransformerMemoryEmbeddingProvider,
    load_embedding_manifest,
)


def _write_model_fixture(
    root: Path,
    *,
    dimension: int = 3,
    runtime_max_length: int = 12,
    query_prefix: str = "Q:",
    document_prefix: str = "D:",
    trust_remote_code: bool = False,
) -> Path:
    root.mkdir(parents=True)
    files = {
        "config.json": b"{}",
        "model.safetensors": b"safe-test-weights",
        "tokenizer.json": b"{}",
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    payload = {
        "version": "memory_embedding_manifest_v1",
        "model_id": "ibm-granite/granite-embedding-97m-multilingual-r2",
        "revision": "a" * 40,
        "dimension": dimension,
        "pooling": "cls",
        "normalize": True,
        "model_max_length": 128,
        "runtime_max_length": runtime_max_length,
        "chunk_overlap": 2,
        "query_prefix": query_prefix,
        "document_prefix": document_prefix,
        "local_files_only": True,
        "trust_remote_code": trust_remote_code,
        "files": [
            {
                "path": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in files.items()
        ],
    }
    manifest_path = root / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path


def test_manifest_rejects_remote_code_and_detects_tampering(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    manifest_path = _write_model_fixture(model_path, trust_remote_code=True)
    with pytest.raises(EmbeddingConfigurationError, match="自定义代码"):
        load_embedding_manifest(manifest_path)

    manifest_path = _write_model_fixture(
        tmp_path / "safe-model",
        trust_remote_code=False,
    )
    provider = LocalTransformerMemoryEmbeddingProvider(
        manifest_path.parent,
        verify_hashes=True,
    )
    assert provider.dimension == 3
    assert provider.model_id.startswith("transformer:ibm-granite/")
    provider.close()

    (manifest_path.parent / "model.safetensors").write_bytes(
        b"x" * len(b"safe-test-weights")
    )
    with pytest.raises(EmbeddingConfigurationError, match="摘要不匹配"):
        LocalTransformerMemoryEmbeddingProvider(
            manifest_path.parent,
            verify_hashes=True,
        )


def test_transformer_provider_chunks_pools_and_normalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    model_path = tmp_path / "model"
    _write_model_fixture(model_path)
    provider = LocalTransformerMemoryEmbeddingProvider(model_path, device="cpu")

    class FakeTokenizer:
        def __init__(self) -> None:
            self.encoded_texts: list[str] = []
            self.batch_calls = 0

        def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
            assert not add_special_tokens
            self.encoded_texts.append(text)
            return [2 + (ord(character) % 31) for character in text]

        @staticmethod
        def num_special_tokens_to_add(*, pair: bool) -> int:
            assert not pair
            return 2

        @staticmethod
        def decode(
            token_ids: list[int],
            *,
            skip_special_tokens: bool,
            clean_up_tokenization_spaces: bool,
        ) -> str:
            assert skip_special_tokens and not clean_up_tokenization_spaces
            return "".join(chr(65 + token % 26) for token in token_ids)

        def __call__(self, texts: list[str], **kwargs: object) -> dict[str, object]:
            assert kwargs["truncation"] is True
            assert kwargs["max_length"] == 12
            self.batch_calls += 1
            lengths = [min(12, max(2, len(text) + 2)) for text in texts]
            width = max(lengths)
            input_ids = torch.zeros((len(texts), width), dtype=torch.long)
            attention_mask = torch.zeros((len(texts), width), dtype=torch.long)
            for index, length in enumerate(lengths):
                input_ids[index, :length] = torch.arange(1, length + 1)
                attention_mask[index, :length] = 1
            return {"input_ids": input_ids, "attention_mask": attention_mask}

    class FakeModel:
        config = SimpleNamespace(hidden_size=3)

        def __call__(self, **inputs: object) -> SimpleNamespace:
            input_ids = inputs["input_ids"]
            assert isinstance(input_ids, torch.Tensor)
            batch, width = input_ids.shape
            hidden = torch.zeros((batch, width, 3), dtype=torch.float32)
            hidden[:, :, 0] = 1.0
            hidden[:, :, 1] = input_ids.float()
            hidden[:, :, 2] = torch.arange(width).float()
            return SimpleNamespace(last_hidden_state=hidden)

    tokenizer = FakeTokenizer()
    monkeypatch.setattr(
        provider,
        "_load_components",
        lambda: (torch, tokenizer, FakeModel(), "cpu"),
    )

    document = provider.embed_documents(["这是一条需要分块的长期记忆abcdefghijk"])[0]
    query = provider.embed_query("我的长期记忆是什么？")

    assert len(document) == 3 and len(query) == 3
    assert math.isclose(sum(value * value for value in document), 1.0, rel_tol=1e-6)
    assert math.isclose(sum(value * value for value in query), 1.0, rel_tol=1e-6)
    assert any(item.startswith("D:") for item in tokenizer.encoded_texts)
    assert any(item.startswith("Q:") for item in tokenizer.encoded_texts)
    assert tokenizer.batch_calls >= 2
    provider.close()


def test_transformer_loader_forces_local_files_and_disables_remote_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    model_path = tmp_path / "model"
    _write_model_fixture(model_path)
    provider = LocalTransformerMemoryEmbeddingProvider(model_path, device="cpu")
    calls: dict[str, dict[str, object]] = {}

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, _path: str, **kwargs: object) -> object:
            calls["tokenizer"] = kwargs
            return object()

    class FakeModel:
        config = SimpleNamespace(hidden_size=3)

        @classmethod
        def from_pretrained(cls, _path: str, **kwargs: object) -> FakeModel:
            calls["model"] = kwargs
            return cls()

        def to(self, device: str) -> FakeModel:
            assert device == "cpu"
            return self

        def eval(self) -> None:
            return None

    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = FakeTokenizer
    transformers.AutoModel = FakeModel
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    _torch, _tokenizer, _model, resolved_device = provider._load_components()

    assert resolved_device == "cpu"
    for key in ("tokenizer", "model"):
        assert calls[key]["local_files_only"] is True
        assert calls[key]["trust_remote_code"] is False
    provider.close()
