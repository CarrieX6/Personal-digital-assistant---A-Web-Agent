from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Sequence

from .retrieval import HashingMemoryEmbeddingProvider, MemoryEmbeddingProvider


MANIFEST_NAME = "embedding-manifest.json"
MANIFEST_VERSION = "memory_embedding_manifest_v1"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
EmbeddingPooling = Literal["cls", "mean", "last_token"]


class EmbeddingConfigurationError(ValueError):
    """Raised when a local embedding model is absent or unsafe to load."""


class EmbeddingRuntimeError(RuntimeError):
    """Raised when a prepared local model cannot produce embeddings."""


@dataclass(frozen=True)
class EmbeddingModelFile:
    path: str
    size_bytes: int
    sha256: str

    @classmethod
    def from_value(cls, value: Any) -> EmbeddingModelFile:
        if not isinstance(value, dict):
            raise EmbeddingConfigurationError("模型清单中的 files 条目必须是对象。")
        path = str(value.get("path", "")).strip()
        candidate = PurePosixPath(path)
        if (
            not path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or "\\" in path
        ):
            raise EmbeddingConfigurationError("模型文件必须使用安全的相对 POSIX 路径。")
        try:
            size_bytes = int(value.get("size_bytes", 0))
        except (TypeError, ValueError) as exc:
            raise EmbeddingConfigurationError("模型文件大小无效。") from exc
        sha256 = str(value.get("sha256", "")).strip().lower()
        if size_bytes <= 0 or not _SHA256.fullmatch(sha256):
            raise EmbeddingConfigurationError("模型文件大小或 SHA-256 无效。")
        return cls(path=path, size_bytes=size_bytes, sha256=sha256)


@dataclass(frozen=True)
class EmbeddingModelManifest:
    version: str
    model_id: str
    revision: str
    dimension: int
    pooling: EmbeddingPooling
    normalize: bool
    model_max_length: int
    runtime_max_length: int
    chunk_overlap: int
    query_prefix: str
    document_prefix: str
    local_files_only: bool
    trust_remote_code: bool
    files: tuple[EmbeddingModelFile, ...]

    @classmethod
    def from_value(cls, value: Any) -> EmbeddingModelManifest:
        if not isinstance(value, dict):
            raise EmbeddingConfigurationError("Embedding 模型清单必须是对象。")
        version = str(value.get("version", ""))
        model_id = str(value.get("model_id", "")).strip()
        revision = str(value.get("revision", "")).strip().lower()
        pooling = str(value.get("pooling", "")).strip()
        try:
            dimension = int(value.get("dimension", 0))
            model_max_length = int(value.get("model_max_length", 0))
            runtime_max_length = int(value.get("runtime_max_length", 0))
            chunk_overlap = int(value.get("chunk_overlap", 0))
        except (TypeError, ValueError) as exc:
            raise EmbeddingConfigurationError("Embedding 模型清单中的数值无效。") from exc
        if version != MANIFEST_VERSION:
            raise EmbeddingConfigurationError("不支持的 Embedding 模型清单版本。")
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", model_id):
            raise EmbeddingConfigurationError("Embedding model_id 无效。")
        if not _COMMIT_SHA.fullmatch(revision):
            raise EmbeddingConfigurationError("Embedding revision 必须是固定的 40 位提交 SHA。")
        if pooling not in {"cls", "mean", "last_token"}:
            raise EmbeddingConfigurationError("Embedding pooling 配置无效。")
        if dimension <= 0 or dimension > 8192:
            raise EmbeddingConfigurationError("Embedding 维度无效。")
        if model_max_length <= 0 or model_max_length > 131_072:
            raise EmbeddingConfigurationError("Embedding 模型最大长度无效。")
        if runtime_max_length <= 8 or runtime_max_length > model_max_length:
            raise EmbeddingConfigurationError("Embedding 运行时最大长度无效。")
        if chunk_overlap < 0 or chunk_overlap >= runtime_max_length - 2:
            raise EmbeddingConfigurationError("Embedding 分块重叠长度无效。")
        local_files_only = value.get("local_files_only")
        trust_remote_code = value.get("trust_remote_code")
        if local_files_only is not True:
            raise EmbeddingConfigurationError("运行时必须只加载本地 Embedding 文件。")
        if trust_remote_code is not False:
            raise EmbeddingConfigurationError("运行时禁止执行 Embedding 仓库自定义代码。")
        files_value = value.get("files")
        if not isinstance(files_value, list) or not files_value:
            raise EmbeddingConfigurationError("Embedding 模型清单缺少文件摘要。")
        files = tuple(EmbeddingModelFile.from_value(item) for item in files_value)
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise EmbeddingConfigurationError("Embedding 模型清单包含重复文件。")
        required = {"config.json", "model.safetensors"}
        if not required.issubset(paths):
            raise EmbeddingConfigurationError("Embedding 模型缺少配置或 Safetensors 权重。")
        if not any(
            path in paths
            for path in ("tokenizer.json", "tokenizer.model", "vocab.txt")
        ):
            raise EmbeddingConfigurationError("Embedding 模型缺少 Tokenizer 文件。")
        return cls(
            version=version,
            model_id=model_id,
            revision=revision,
            dimension=dimension,
            pooling=pooling,  # type: ignore[arg-type]
            normalize=value.get("normalize") is True,
            model_max_length=model_max_length,
            runtime_max_length=runtime_max_length,
            chunk_overlap=chunk_overlap,
            query_prefix=str(value.get("query_prefix", "")),
            document_prefix=str(value.get("document_prefix", "")),
            local_files_only=True,
            trust_remote_code=False,
            files=files,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "model_id": self.model_id,
            "revision": self.revision,
            "dimension": self.dimension,
            "pooling": self.pooling,
            "normalize": self.normalize,
            "model_max_length": self.model_max_length,
            "runtime_max_length": self.runtime_max_length,
            "chunk_overlap": self.chunk_overlap,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
            "local_files_only": self.local_files_only,
            "trust_remote_code": self.trust_remote_code,
            "files": [item.__dict__ for item in self.files],
        }

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_embedding_manifest(path: Path) -> EmbeddingModelManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EmbeddingConfigurationError("无法读取本地 Embedding 模型清单。") from exc
    return EmbeddingModelManifest.from_value(payload)


def verify_embedding_model_files(
    model_path: Path,
    manifest: EmbeddingModelManifest,
    *,
    verify_hashes: bool,
) -> None:
    resolved_root = model_path.resolve()
    if not resolved_root.is_dir():
        raise EmbeddingConfigurationError("本地 Embedding 模型目录不存在。")
    for item in manifest.files:
        candidate = (resolved_root / Path(item.path)).resolve()
        if resolved_root not in candidate.parents:
            raise EmbeddingConfigurationError("Embedding 模型文件路径越界。")
        if not candidate.is_file():
            raise EmbeddingConfigurationError(f"缺少 Embedding 模型文件：{item.path}")
        if candidate.stat().st_size != item.size_bytes:
            raise EmbeddingConfigurationError(f"Embedding 模型文件大小不匹配：{item.path}")
        if verify_hashes and sha256_file(candidate) != item.sha256:
            raise EmbeddingConfigurationError(f"Embedding 模型文件摘要不匹配：{item.path}")


class LocalTransformerMemoryEmbeddingProvider:
    """Strictly-offline Transformer embeddings with encrypted-store-friendly IDs."""

    def __init__(
        self,
        model_path: Path,
        *,
        manifest_path: Path | None = None,
        device: str = "auto",
        dtype: str = "auto",
        batch_size: int = 16,
        max_length: int | None = None,
        verify_hashes: bool = True,
    ) -> None:
        self.model_path = model_path.expanduser().resolve()
        selected_manifest_path = (
            manifest_path.expanduser().resolve()
            if manifest_path is not None
            else self.model_path / MANIFEST_NAME
        )
        self.manifest = load_embedding_manifest(selected_manifest_path)
        verify_embedding_model_files(
            self.model_path,
            self.manifest,
            verify_hashes=verify_hashes,
        )
        safe_device = device.strip().lower()
        if safe_device not in {"auto", "cpu", "cuda"}:
            raise EmbeddingConfigurationError("Embedding device 只能是 auto、cpu 或 cuda。")
        safe_dtype = dtype.strip().lower()
        if safe_dtype not in {"auto", "float32", "float16", "bfloat16"}:
            raise EmbeddingConfigurationError("Embedding dtype 配置无效。")
        safe_batch_size = int(batch_size)
        if safe_batch_size <= 0 or safe_batch_size > 256:
            raise EmbeddingConfigurationError("Embedding batch_size 必须在 1 到 256 之间。")
        safe_max_length = int(max_length or self.manifest.runtime_max_length)
        if safe_max_length <= 8 or safe_max_length > self.manifest.model_max_length:
            raise EmbeddingConfigurationError("Embedding max_length 配置无效。")
        if self.manifest.chunk_overlap >= safe_max_length - 2:
            raise EmbeddingConfigurationError("Embedding chunk_overlap 大于运行时长度。")
        self.dimension = self.manifest.dimension
        self.device = safe_device
        self.dtype = safe_dtype
        self.batch_size = safe_batch_size
        self.max_length = safe_max_length
        self.model_id = (
            f"transformer:{self.manifest.model_id}@{self.manifest.revision}:"
            f"{self.manifest.fingerprint[:20]}:max{self.max_length}:dtype{self.dtype}"
        )
        self._runtime_lock = threading.RLock()
        self._torch: Any | None = None
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._resolved_device = ""
        self._load_error: EmbeddingRuntimeError | None = None

    def _load_components(self) -> tuple[Any, Any, Any, str]:
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise EmbeddingRuntimeError(
                "本地 Transformer Embedding 缺少 torch 或 transformers。"
            ) from exc
        resolved_device = self.device
        if resolved_device == "auto":
            resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
        if resolved_device == "cuda" and not torch.cuda.is_available():
            raise EmbeddingRuntimeError("已配置 CUDA Embedding，但当前 CUDA 不可用。")
        dtype_name = self.dtype
        if dtype_name == "auto":
            dtype_name = "float32"
            if resolved_device == "cuda":
                supports_bf16 = getattr(torch.cuda, "is_bf16_supported", lambda: False)
                dtype_name = "bfloat16" if supports_bf16() else "float16"
        torch_dtype = getattr(torch, dtype_name)
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                str(self.model_path),
                local_files_only=True,
                trust_remote_code=False,
            )
            model = AutoModel.from_pretrained(
                str(self.model_path),
                local_files_only=True,
                trust_remote_code=False,
                dtype=torch_dtype,
            )
            model = model.to(resolved_device)
            model.eval()
        except Exception as exc:
            raise EmbeddingRuntimeError("无法加载已准备的本地 Transformer Embedding。") from exc
        hidden_size = int(getattr(getattr(model, "config", None), "hidden_size", 0))
        if hidden_size and hidden_size != self.dimension:
            raise EmbeddingRuntimeError("Transformer 隐藏维度与模型清单不一致。")
        return torch, tokenizer, model, resolved_device

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if self._load_error is not None:
            raise self._load_error
        with self._runtime_lock:
            if self._model is not None:
                return
            try:
                torch, tokenizer, model, resolved_device = self._load_components()
            except EmbeddingRuntimeError as exc:
                self._load_error = exc
                raise
            self._torch = torch
            self._tokenizer = tokenizer
            self._model = model
            self._resolved_device = resolved_device

    def _chunk_text(self, text: str, prefix: str) -> list[tuple[str, int]]:
        assert self._tokenizer is not None
        tokenizer = self._tokenizer
        body = str(text)
        prefix_ids = tokenizer.encode(prefix, add_special_tokens=False) if prefix else []
        body_ids = tokenizer.encode(body, add_special_tokens=False)
        special_tokens = int(tokenizer.num_special_tokens_to_add(pair=False))
        capacity = self.max_length - special_tokens - len(prefix_ids)
        if capacity <= 0:
            raise EmbeddingRuntimeError("Embedding 前缀占满了全部 Token 预算。")
        if len(body_ids) <= capacity:
            return [(prefix + body, max(1, len(body_ids)))]
        overlap = min(self.manifest.chunk_overlap, max(0, capacity - 1))
        stride = max(1, capacity - overlap)
        chunks: list[tuple[str, int]] = []
        for start in range(0, len(body_ids), stride):
            token_slice = body_ids[start : start + capacity]
            if not token_slice:
                break
            decoded = tokenizer.decode(
                token_slice,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            chunks.append((prefix + decoded, len(token_slice)))
            if start + capacity >= len(body_ids):
                break
        return chunks

    def _pool(self, hidden_states: Any, attention_mask: Any) -> Any:
        assert self._torch is not None
        torch = self._torch
        if self.manifest.pooling == "cls":
            return hidden_states[:, 0]
        if self.manifest.pooling == "last_token":
            sequence_lengths = attention_mask.sum(dim=1) - 1
            batch_indexes = torch.arange(
                hidden_states.shape[0],
                device=hidden_states.device,
            )
            return hidden_states[batch_indexes, sequence_lengths]
        mask = attention_mask.unsqueeze(-1).to(dtype=hidden_states.dtype)
        total = (hidden_states * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        return total / counts

    def _encode_chunks(self, texts: Sequence[str]) -> list[list[float]]:
        assert self._torch is not None
        assert self._tokenizer is not None
        assert self._model is not None
        torch = self._torch
        vectors: list[list[float]] = []
        for offset in range(0, len(texts), self.batch_size):
            batch = list(texts[offset : offset + self.batch_size])
            encoded = self._tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            model_inputs = {
                key: value.to(self._resolved_device)
                for key, value in encoded.items()
            }
            with torch.inference_mode():
                output = self._model(**model_inputs)
                hidden_states = getattr(output, "last_hidden_state", None)
                if hidden_states is None:
                    hidden_states = output[0]
                pooled = self._pool(hidden_states, model_inputs["attention_mask"])
                if self.manifest.normalize:
                    pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            values = pooled.detach().to(device="cpu", dtype=torch.float32).tolist()
            vectors.extend([[float(item) for item in vector] for vector in values])
        return vectors

    @staticmethod
    def _aggregate(
        vectors: Sequence[Sequence[float]],
        weights: Sequence[int],
        dimension: int,
    ) -> list[float]:
        if not vectors or len(vectors) != len(weights):
            raise EmbeddingRuntimeError("Transformer 分块向量数量无效。")
        total_weight = float(sum(max(1, weight) for weight in weights))
        combined = [0.0] * dimension
        for vector, weight in zip(vectors, weights):
            if len(vector) != dimension:
                raise EmbeddingRuntimeError("Transformer 返回的向量维度无效。")
            safe_weight = max(1, weight)
            for index, value in enumerate(vector):
                combined[index] += float(value) * safe_weight
        combined = [value / total_weight for value in combined]
        norm = math.sqrt(sum(value * value for value in combined))
        if norm <= 0 or not math.isfinite(norm):
            raise EmbeddingRuntimeError("Transformer 返回了无效的零向量。")
        return [value / norm for value in combined]

    def _embed(self, texts: Sequence[str], *, prefix: str) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_loaded()
        with self._runtime_lock:
            chunk_texts: list[str] = []
            chunk_weights: list[int] = []
            ranges: list[tuple[int, int]] = []
            for text in texts:
                start = len(chunk_texts)
                for chunk, weight in self._chunk_text(str(text), prefix):
                    chunk_texts.append(chunk)
                    chunk_weights.append(weight)
                ranges.append((start, len(chunk_texts)))
            chunk_vectors = self._encode_chunks(chunk_texts)
            return [
                self._aggregate(
                    chunk_vectors[start:end],
                    chunk_weights[start:end],
                    self.dimension,
                )
                for start, end in ranges
            ]

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], prefix=self.manifest.query_prefix)[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, prefix=self.manifest.document_prefix)

    def warmup(self) -> None:
        vector = self.embed_query("本地记忆召回模型自检")
        if len(vector) != self.dimension:
            raise EmbeddingRuntimeError("本地 Embedding 自检维度无效。")

    def close(self) -> None:
        with self._runtime_lock:
            torch = self._torch
            self._model = None
            self._tokenizer = None
            self._torch = None
            self._resolved_device = ""
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def create_memory_embedding_provider_from_env() -> MemoryEmbeddingProvider:
    provider_name = os.getenv("AGENT_MEMORY_EMBEDDING_PROVIDER", "hash").strip().lower()
    if provider_name in {"hash", "hashing"}:
        return HashingMemoryEmbeddingProvider()
    if provider_name not in {"transformer", "granite"}:
        raise EmbeddingConfigurationError("AGENT_MEMORY_EMBEDDING_PROVIDER 配置无效。")
    model_path_value = os.getenv("AGENT_MEMORY_EMBEDDING_MODEL_PATH", "").strip()
    if not model_path_value:
        raise EmbeddingConfigurationError("未配置本地 Embedding 模型目录。")
    manifest_value = os.getenv("AGENT_MEMORY_EMBEDDING_MANIFEST", "").strip()
    try:
        batch_size = int(os.getenv("AGENT_MEMORY_EMBEDDING_BATCH_SIZE", "16"))
        max_length_value = os.getenv("AGENT_MEMORY_EMBEDDING_MAX_LENGTH", "").strip()
        max_length = int(max_length_value) if max_length_value else None
    except ValueError as exc:
        raise EmbeddingConfigurationError("Embedding 数值环境变量无效。") from exc
    return LocalTransformerMemoryEmbeddingProvider(
        Path(model_path_value),
        manifest_path=Path(manifest_value) if manifest_value else None,
        device=os.getenv("AGENT_MEMORY_EMBEDDING_DEVICE", "auto"),
        dtype=os.getenv("AGENT_MEMORY_EMBEDDING_DTYPE", "auto"),
        batch_size=batch_size,
        max_length=max_length,
        verify_hashes=_env_flag("AGENT_MEMORY_EMBEDDING_VERIFY_HASHES", True),
    )


def embedding_provider_required() -> bool:
    return _env_flag("AGENT_MEMORY_EMBEDDING_REQUIRED", False)
