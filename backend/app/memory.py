from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import struct
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from .memory_crypto import (
    MemoryCipher,
    MemoryEncryptionError,
    load_memory_key,
    memory_aad,
    record_aad,
)
from .tokenization import (
    TokenCounter,
    clip_text_to_tokens,
    get_default_token_counter,
)
from .retrieval import (
    MemoryEmbeddingProvider,
    cosine_similarity,
)
from .transformer_embedding import (
    EmbeddingConfigurationError,
    EmbeddingRuntimeError,
    create_memory_embedding_provider_from_env,
    embedding_provider_required,
)


MemoryType = Literal[
    "profile",
    "preference",
    "fact",
    "task_state",
    "episode",
    "procedure",
    "asset_relation",
]
MemoryScope = Literal["user", "channel", "thread", "project"]
MemoryStatus = Literal["candidate", "active", "superseded", "archived"]
MemorySensitivity = Literal["normal", "private", "sensitive"]
MemoryRetrievalPolicy = Literal["always", "explicit_only", "never"]


LOGGER = logging.getLogger(__name__)
SESSION_SUMMARY_SCHEMA_VERSION = "session-summary-v3"


class SessionSummaryProvider(Protocol):
    summary_provider_id: str

    def summarize_session(
        self,
        *,
        previous: dict[str, Any],
        messages: list[dict[str, Any]],
        token_budget: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    owner_id: str
    memory_type: MemoryType
    scope: MemoryScope
    scope_id: str | None
    content: str
    topic_key: str | None
    source: str
    source_message_id: int | None
    source_run_id: str | None
    confidence: float
    importance: float
    sensitivity: MemorySensitivity
    retrieval_policy: MemoryRetrievalPolicy
    valid_from: float
    valid_to: float | None
    status: MemoryStatus
    supersedes_id: str | None
    created_at: float
    updated_at: float
    last_accessed_at: float | None
    access_count: int
    utility_score: float
    metadata: dict[str, Any]
    relevance_score: float = 0.0
    evidence_count: int = 0
    evidence_refs: tuple[str, ...] = ()

    def to_context_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.memory_type,
            "scope": self.scope,
            "content": self.content,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "importance": round(self.importance, 3),
            "retrieval_policy": self.retrieval_policy,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "relevance_score": round(self.relevance_score, 4),
            "evidence_count": self.evidence_count,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class MemoryEvidence:
    evidence_id: str
    memory_id: str
    owner_id: str
    source_type: str
    source: str
    excerpt: str
    content_hash: str
    source_message_id: int | None
    source_run_id: str | None
    confidence: float
    observed_at: float
    created_at: float


@dataclass(frozen=True)
class SessionMemorySummary:
    summary: str
    open_loops: list[str]
    decisions: list[str]
    completed_actions: list[str]
    active_assumptions: list[str]
    artifact_refs: list[str]
    blockers: list[str]
    next_goal: str | None
    last_message_id: int
    updated_at: float
    provider_id: str = "legacy:extractive-v1"
    schema_version: str = SESSION_SUMMARY_SCHEMA_VERSION
    fallback_reason: str | None = None
    covered_from_message_id: int | None = None
    provenance: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ConversationContext:
    owner_id: str
    thread_id: str
    project_id: str | None
    messages: list[tuple[str, str]]
    memories: list[str]
    memory_items: list[MemoryRecord]
    session_summary: str = ""
    open_loops: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    completed_actions: tuple[str, ...] = ()
    active_assumptions: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_goal: str | None = None
    recent_attachments: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ConversationSummary:
    thread_id: str
    owner_id: str
    channel: str
    project_id: str | None
    title: str
    created_at: float
    updated_at: float
    message_count: int


@dataclass(frozen=True)
class StoredConversationMessage:
    id: int
    role: str
    content: str
    created_at: float
    run_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class StoredAgentRun:
    run_id: str
    owner_id: str
    thread_id: str
    channel: str
    project_id: str | None
    message: str
    status: str
    approval: dict[str, Any] | None
    response: dict[str, Any] | None
    created_at: float
    updated_at: float
    checkpoint_thread_id: str | None = None


class MemoryIsolationError(RuntimeError):
    """Raised when a thread is accessed by a different owner."""


class MemoryPolicyError(ValueError):
    """Raised when content is unsafe to persist as long-term memory."""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


class SQLiteMemoryStore:
    """Owner-scoped conversation history and explicit long-term memories."""

    def __init__(
        self,
        path: Path,
        *,
        encryption_key: bytes | None = None,
        encryption_enabled: bool | None = None,
        embedding_provider: MemoryEmbeddingProvider | None = None,
        semantic_enabled: bool | None = None,
        token_counter: TokenCounter | None = None,
        summary_provider: SessionSummaryProvider | None = None,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fts_available = False
        self._token_counter = token_counter or get_default_token_counter()
        self._summary_provider = summary_provider
        enabled = (
            os.getenv("AGENT_MEMORY_ENCRYPTION_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"}
            if encryption_enabled is None
            else bool(encryption_enabled)
        )
        self._externally_managed_encryption_key = bool(
            encryption_key is not None
            or os.getenv("AGENT_MEMORY_ENCRYPTION_KEY", "").strip()
        )
        self._cipher = (
            MemoryCipher(encryption_key or load_memory_key(path)) if enabled else None
        )
        self._ciphers = (
            {self._cipher.key_id: self._cipher} if self._cipher is not None else {}
        )
        semantic_flag = (
            os.getenv("AGENT_MEMORY_SEMANTIC_ENABLED", "false").strip().lower()
            not in {"0", "false", "no", "off"}
            if semantic_enabled is None
            else bool(semantic_enabled)
        )
        if embedding_provider is not None and semantic_enabled is None:
            semantic_flag = True
        self._embedding_provider: MemoryEmbeddingProvider | None = None
        self._embedding_configuration_error: str | None = None
        self._embedding_runtime_error: str | None = None
        if semantic_flag:
            if embedding_provider is not None:
                self._embedding_provider = embedding_provider
            else:
                try:
                    self._embedding_provider = (
                        create_memory_embedding_provider_from_env()
                    )
                    if embedding_provider_required():
                        warmup = getattr(self._embedding_provider, "warmup", None)
                        if callable(warmup):
                            warmup()
                except (EmbeddingConfigurationError, EmbeddingRuntimeError) as exc:
                    if embedding_provider_required():
                        raise
                    self._embedding_configuration_error = str(exc)
                    LOGGER.warning(
                        "Local Transformer memory embedding is unavailable; "
                        "structured and lexical retrieval remain enabled (%s).",
                        type(exc).__name__,
                    )
        default_semantic_threshold = (
            0.80
            if self._embedding_provider is not None
            and self._embedding_provider.model_id.startswith("transformer:")
            else 0.24
        )
        self._semantic_threshold = _bounded_memory_float(
            os.getenv("AGENT_MEMORY_SEMANTIC_THRESHOLD"),
            default=default_semantic_threshold,
            lower=0.0,
            upper=1.0,
        )
        self._rrf_k = _bounded_memory_int(
            os.getenv("AGENT_MEMORY_RRF_K"),
            default=60,
            lower=1,
            upper=200,
        )
        self._mmr_lambda = _bounded_memory_float(
            os.getenv("AGENT_MEMORY_MMR_LAMBDA"),
            default=0.78,
            lower=0.5,
            upper=1.0,
        )
        self._initialize()
        self._backfill_legacy_memory_evidence()
        self._backfill_memory_claim_keys()

    @property
    def token_counter(self) -> TokenCounter:
        return self._token_counter

    def set_token_counter(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def set_summary_provider(
        self,
        summary_provider: SessionSummaryProvider | None,
    ) -> None:
        self._summary_provider = summary_provider

    def _cipher_for(self, key_id: Any) -> MemoryCipher:
        if self._cipher is None:
            raise MemoryEncryptionError("上下文记忆已加密，但当前未启用解密。")
        safe_key_id = str(key_id or self._cipher.key_id)
        cipher = self._ciphers.get(safe_key_id)
        if cipher is None:
            raise MemoryEncryptionError(
                f"上下文记忆无法解密：缺少 key_id={safe_key_id} 对应的密钥。"
            )
        return cipher

    def _encrypt_payload(
        self,
        record_type: str,
        owner_id: str,
        record_id: str,
        payload: dict[str, Any],
        *,
        cipher: MemoryCipher | None = None,
    ) -> tuple[bytes, bytes, int, str]:
        selected = cipher or self._cipher
        if selected is None:
            raise MemoryEncryptionError("当前未启用上下文记忆加密。")
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        ciphertext, nonce = selected.encrypt(
            serialized,
            aad=record_aad(record_type, owner_id, record_id),
        )
        return ciphertext, nonce, selected.version, selected.key_id

    def _decrypt_payload(
        self,
        record_type: str,
        owner_id: str,
        record_id: str,
        ciphertext: Any,
        nonce: Any,
        encryption_version: Any,
        key_id: Any,
    ) -> dict[str, Any] | None:
        if int(encryption_version or 0) <= 0:
            return None
        if ciphertext is None or nonce is None:
            raise MemoryEncryptionError("上下文记忆密文缺少 nonce 或密文数据。")
        raw = self._cipher_for(key_id).decrypt(
            bytes(ciphertext),
            bytes(nonce),
            aad=record_aad(record_type, owner_id, record_id),
        )
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MemoryEncryptionError("上下文记忆解密后不是有效 JSON。") from exc
        if not isinstance(value, dict):
            raise MemoryEncryptionError("上下文记忆解密后不是对象。")
        return value

    def _decode_thread_title(
        self,
        *,
        owner_id: str,
        thread_id: str,
        plaintext: Any,
        ciphertext: Any,
        nonce: Any,
        encryption_version: Any,
        key_id: Any,
    ) -> str:
        payload = self._decrypt_payload(
            "thread",
            owner_id,
            thread_id,
            ciphertext,
            nonce,
            encryption_version,
            key_id,
        )
        return str(payload.get("title", "")) if payload is not None else str(plaintext)

    def _embedding_fingerprint(
        self,
        content: str,
        *,
        cipher: MemoryCipher | None = None,
        model_id: str | None = None,
    ) -> str:
        provider = self._embedding_provider
        selected_model_id = model_id or (provider.model_id if provider else "")
        if not selected_model_id:
            return ""
        normalized = re.sub(r"\s+", " ", content).strip().casefold()
        value = f"embedding\x1f{selected_model_id}\x1f{normalized}"
        selected = cipher or self._cipher
        if selected is not None:
            return selected.blind_index(value)
        return sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_embedding(vector: list[float]) -> bytes:
        if not vector or any(not math.isfinite(value) for value in vector):
            raise ValueError("Embedding 向量不能为空或包含非有限值。")
        return struct.pack(f"<{len(vector)}f", *vector)

    @staticmethod
    def _deserialize_embedding(raw: bytes, dimension: int) -> list[float]:
        if dimension <= 0 or len(raw) != dimension * 4:
            raise MemoryEncryptionError("Embedding 向量维度或字节长度无效。")
        return list(struct.unpack(f"<{dimension}f", raw))

    def _store_embedding_vector(
        self,
        connection: sqlite3.Connection,
        *,
        memory: MemoryRecord,
        vector: list[float],
        cipher: MemoryCipher | None = None,
    ) -> None:
        provider = self._embedding_provider
        if provider is None:
            return
        if len(vector) != provider.dimension:
            raise ValueError("Embedding Provider 返回的维度与声明不一致。")
        raw = self._serialize_embedding(vector)
        selected = cipher or self._cipher
        nonce: bytes | None = None
        version = 0
        key_id: str | None = None
        stored = raw
        if selected is not None:
            stored, nonce = selected.encrypt_bytes(
                raw,
                aad=record_aad(
                    "embedding",
                    memory.owner_id,
                    memory.id,
                    "vector",
                ),
            )
            version = selected.version
            key_id = selected.key_id
        connection.execute(
            """
            INSERT INTO agent_memory_embeddings (
                memory_id, owner_id, model_id, dimension,
                content_fingerprint, vector_ciphertext, vector_nonce,
                encryption_version, key_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                model_id = excluded.model_id,
                dimension = excluded.dimension,
                content_fingerprint = excluded.content_fingerprint,
                vector_ciphertext = excluded.vector_ciphertext,
                vector_nonce = excluded.vector_nonce,
                encryption_version = excluded.encryption_version,
                key_id = excluded.key_id,
                updated_at = excluded.updated_at
            """,
            (
                memory.id,
                memory.owner_id,
                provider.model_id,
                provider.dimension,
                self._embedding_fingerprint(memory.content, cipher=selected),
                stored,
                nonce,
                version,
                key_id,
                time.time(),
            ),
        )

    def _semantic_scores(
        self,
        *,
        owner_id: str,
        query: str,
        candidates: list[MemoryRecord],
    ) -> tuple[dict[str, float], dict[str, list[float]]]:
        provider = self._embedding_provider
        if provider is None or not query.strip() or not candidates:
            return {}, {}
        candidate_ids = [item.id for item in candidates]
        placeholders = ",".join("?" for _ in candidate_ids)
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT memory_id, model_id, dimension, content_fingerprint,
                           vector_ciphertext, vector_nonce,
                           encryption_version, key_id
                    FROM agent_memory_embeddings
                    WHERE owner_id = ? AND memory_id IN ({placeholders})
                    """,
                    (owner_id, *candidate_ids),
                ).fetchall()
            existing = {str(row[0]): row for row in rows}
            stale = [
                item
                for item in candidates
                if item.id not in existing
                or str(existing[item.id][1]) != provider.model_id
                or int(existing[item.id][2]) != provider.dimension
                or str(existing[item.id][3])
                != self._embedding_fingerprint(item.content)
            ]

            # Model inference deliberately runs outside the SQLite write lock.
            # A current-snapshot guard below prevents an older content vector
            # from being reinserted if the memory changes during inference.
            stale_vectors = provider.embed_documents(
                [item.content for item in stale]
            )
            if len(stale_vectors) != len(stale):
                raise ValueError(
                    "Embedding Provider 返回的向量数量与输入不一致。"
                )
            query_vector = provider.embed_query(query)
            if len(query_vector) != provider.dimension:
                raise ValueError("Embedding Provider 返回的查询向量无效。")

            if stale:
                with self._lock, self._connect() as connection:
                    for item, vector in zip(stale, stale_vectors):
                        current = connection.execute(
                            """
                            SELECT updated_at, status FROM agent_memories
                            WHERE owner_id = ? AND id = ?
                            """,
                            (owner_id, item.id),
                        ).fetchone()
                        if (
                            current is None
                            or str(current[1]) != "active"
                            or abs(float(current[0]) - item.updated_at) > 1e-6
                        ):
                            continue
                        self._store_embedding_vector(
                            connection,
                            memory=item,
                            vector=vector,
                        )
                    rows = connection.execute(
                        f"""
                        SELECT memory_id, model_id, dimension,
                               content_fingerprint, vector_ciphertext,
                               vector_nonce, encryption_version, key_id
                        FROM agent_memory_embeddings
                        WHERE owner_id = ? AND memory_id IN ({placeholders})
                          AND model_id = ? AND dimension = ?
                        """,
                        (
                            owner_id,
                            *candidate_ids,
                            provider.model_id,
                            provider.dimension,
                        ),
                    ).fetchall()
            vectors_by_id: dict[str, list[float]] = {}
            scores: dict[str, float] = {}
            for row in rows:
                if (
                    str(row[1]) != provider.model_id
                    or int(row[2]) != provider.dimension
                ):
                    continue
                raw = bytes(row[4])
                if int(row[6] or 0) > 0:
                    if row[5] is None:
                        raise MemoryEncryptionError("Embedding 密文缺少 nonce。")
                    raw = self._cipher_for(row[7]).decrypt_bytes(
                        raw,
                        bytes(row[5]),
                        aad=record_aad(
                            "embedding",
                            owner_id,
                            str(row[0]),
                            "vector",
                        ),
                    )
                vector = self._deserialize_embedding(raw, int(row[2]))
                vectors_by_id[str(row[0])] = vector
                scores[str(row[0])] = cosine_similarity(query_vector, vector)
            self._embedding_runtime_error = None
            return scores, vectors_by_id
        except (
            MemoryEncryptionError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
        ) as exc:
            # Semantic retrieval is optional. Lexical and structured paths stay
            # available if a local model/index is temporarily unavailable.
            safe_error = type(exc).__name__
            if self._embedding_runtime_error != safe_error:
                LOGGER.warning(
                    "Memory semantic retrieval fell back to structured and "
                    "lexical routes (%s).",
                    safe_error,
                )
                self._embedding_runtime_error = safe_error
            return {}, {}

    def embedding_status(self) -> dict[str, Any]:
        provider = self._embedding_provider
        return {
            "enabled": provider is not None,
            "provider": type(provider).__name__ if provider is not None else None,
            "model_id": provider.model_id if provider is not None else None,
            "dimension": provider.dimension if provider is not None else None,
            "threshold": self._semantic_threshold,
            "configuration_error": self._embedding_configuration_error,
            "runtime_error": self._embedding_runtime_error,
        }

    def backfill_memory_embeddings(
        self,
        *,
        owner_id: str,
        batch_size: int = 32,
        limit: int | None = None,
    ) -> dict[str, int]:
        """Generate current vectors for active, automatically retrievable memories."""

        provider = self._embedding_provider
        if provider is None:
            raise EmbeddingRuntimeError("未启用可用的 Embedding Provider。")
        safe_batch_size = max(1, min(int(batch_size), 256))
        records = self.list_memory_records(
            owner_id=owner_id,
            status="active",
            limit=None,
        )
        now = time.time()
        eligible = [
            item
            for item in records
            if item.retrieval_policy == "always"
            and item.valid_from <= now
            and (item.valid_to is None or item.valid_to > now)
        ]
        if limit is not None:
            eligible = eligible[: max(0, int(limit))]
        generated = 0
        current_count = 0
        for offset in range(0, len(eligible), safe_batch_size):
            batch = eligible[offset : offset + safe_batch_size]
            ids = [item.id for item in batch]
            placeholders = ",".join("?" for _ in ids)
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT memory_id, model_id, dimension, content_fingerprint
                    FROM agent_memory_embeddings
                    WHERE owner_id = ? AND memory_id IN ({placeholders})
                    """,
                    (owner_id, *ids),
                ).fetchall()
            existing = {str(row[0]): row for row in rows}
            stale = [
                item
                for item in batch
                if item.id not in existing
                or str(existing[item.id][1]) != provider.model_id
                or int(existing[item.id][2]) != provider.dimension
                or str(existing[item.id][3])
                != self._embedding_fingerprint(item.content)
            ]
            current_count += len(batch) - len(stale)
            if not stale:
                continue
            vectors = provider.embed_documents([item.content for item in stale])
            if len(vectors) != len(stale):
                raise EmbeddingRuntimeError(
                    "Embedding Provider 返回的回填向量数量无效。"
                )
            with self._lock, self._connect() as connection:
                for item, vector in zip(stale, vectors):
                    current = connection.execute(
                        """
                        SELECT updated_at, status FROM agent_memories
                        WHERE owner_id = ? AND id = ?
                        """,
                        (owner_id, item.id),
                    ).fetchone()
                    if (
                        current is None
                        or str(current[1]) != "active"
                        or abs(float(current[0]) - item.updated_at) > 1e-6
                    ):
                        continue
                    self._store_embedding_vector(
                        connection,
                        memory=item,
                        vector=vector,
                    )
                    generated += 1
        return {
            "scanned": len(records),
            "eligible": len(eligible),
            "generated": generated,
            "current": current_count,
            "excluded": len(records) - len(eligible),
        }

    def close(self) -> None:
        provider = self._embedding_provider
        if provider is not None:
            provider.close()

    def context(
        self,
        *,
        owner_id: str,
        thread_id: str,
        channel: str | None = None,
        project_id: str | None = None,
        query: str = "",
        message_limit: int = 12,
        memory_limit: int = 8,
        raw_history_token_budget: int | None = None,
        summary_token_budget: int | None = None,
    ) -> ConversationContext:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
        )
        self._refresh_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
            raw_history_token_budget=raw_history_token_budget,
            summary_token_budget=summary_token_budget,
        )
        summary = self.get_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
        )
        summarized_through = summary.last_message_id if summary else 0
        with self._connect() as connection:
            message_rows = connection.execute(
                """
                SELECT id, role, content, metadata_json,
                       payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND id > ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    owner_id,
                    thread_id,
                    summarized_through,
                    max(1, min(message_limit, 50)),
                ),
            ).fetchall()
            attachment_rows = connection.execute(
                """
                SELECT id, content, metadata_json,
                       payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ? AND role = 'user'
                ORDER BY id DESC
                LIMIT 20
                """,
                (owner_id, thread_id),
            ).fetchall()
        recent_attachments: list[dict[str, Any]] = []
        # Attachment continuity is independent of text summarization. The
        # latest image may have moved behind the summary boundary while its
        # short-lived local source file is still valid for a follow-up turn.
        for row in attachment_rows:
            payload = self._decrypt_payload(
                "message",
                owner_id,
                str(row[0]),
                row[3],
                row[4],
                row[5],
                row[6],
            )
            metadata = payload.get("metadata", {}) if payload is not None else None
            if payload is None:
                try:
                    metadata = json.loads(str(row[2] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
            if not isinstance(metadata, dict):
                continue
            candidates = metadata.get("attachments", [])
            if not isinstance(candidates, list):
                candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                source_image_id = candidate.get("source_image_id")
                if not isinstance(source_image_id, str) or not source_image_id:
                    continue
                recent_attachments.append(
                    {
                        "id": source_image_id[:100],
                        "original_name": str(candidate.get("name", ""))[:160],
                        "width": candidate.get("width"),
                        "height": candidate.get("height"),
                    }
                )
            if recent_attachments:
                break
        memory_items = self.search_memories(
            owner_id=owner_id,
            query=query,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
            limit=max(1, min(memory_limit, 20)),
            track_access=False,
        )
        return ConversationContext(
            owner_id=owner_id,
            thread_id=thread_id,
            project_id=project_id,
            messages=[
                (
                    str(row[1]),
                    str(
                        (
                            self._decrypt_payload(
                                "message",
                                owner_id,
                                str(row[0]),
                                row[4],
                                row[5],
                                row[6],
                                row[7],
                            )
                            or {"content": row[2]}
                        ).get("content", "")
                    ),
                )
                for row in reversed(message_rows)
            ],
            memories=[item.content for item in memory_items],
            memory_items=memory_items,
            session_summary=summary.summary if summary else "",
            open_loops=tuple(summary.open_loops if summary else []),
            decisions=tuple(summary.decisions if summary else []),
            completed_actions=tuple(summary.completed_actions if summary else []),
            active_assumptions=tuple(summary.active_assumptions if summary else []),
            artifact_refs=tuple(summary.artifact_refs if summary else []),
            blockers=tuple(summary.blockers if summary else []),
            next_goal=summary.next_goal if summary else None,
            recent_attachments=tuple(recent_attachments[:4]),
        )

    def ensure_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
        channel: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
    ) -> None:
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT owner_id, title, title_ciphertext, title_nonce,
                       encryption_version, key_id, project_id
                FROM agent_threads WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
            if row is not None and str(row[0]) != owner_id:
                raise MemoryIsolationError("该会话属于其他用户，无法访问。")
            safe_project_id = (
                project_id.strip()[:160]
                if isinstance(project_id, str)
                else None
            ) or None
            if (
                row is not None
                and row[6] is not None
                and str(row[6]) != safe_project_id
            ):
                raise MemoryIsolationError("该会话属于其他项目，无法访问。")
            safe_channel = (channel or "unknown").strip()[:40] or "unknown"
            safe_title = (title or "新对话").strip()[:80] or "新对话"
            title_ciphertext = title_nonce = None
            encryption_version = 0
            key_id = None
            stored_title = safe_title
            if self._cipher is not None:
                (
                    title_ciphertext,
                    title_nonce,
                    encryption_version,
                    key_id,
                ) = self._encrypt_payload(
                    "thread",
                    owner_id,
                    thread_id,
                    {"title": safe_title},
                )
                stored_title = ""
            connection.execute(
                """
                INSERT INTO agent_threads (
                    thread_id, owner_id, channel, title, created_at, updated_at,
                    title_ciphertext, title_nonce, encryption_version, key_id,
                    project_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id)
                DO NOTHING
                """,
                (
                    thread_id,
                    owner_id,
                    safe_channel,
                    stored_title,
                    timestamp,
                    timestamp,
                    title_ciphertext,
                    title_nonce,
                    encryption_version,
                    key_id,
                    safe_project_id,
                ),
            )
            if row is not None and (
                channel is not None
                or title is not None
                or (safe_project_id is not None and row[6] is None)
            ):
                assignments: list[str] = []
                values: list[object] = []
                if channel is not None:
                    assignments.append("channel = ?")
                    values.append(safe_channel)
                if title is not None:
                    assignments.extend(
                        [
                            "title = ?",
                            "title_ciphertext = ?",
                            "title_nonce = ?",
                            "encryption_version = ?",
                            "key_id = ?",
                        ]
                    )
                    values.extend(
                        (
                            stored_title,
                            title_ciphertext,
                            title_nonce,
                            encryption_version,
                            key_id,
                        )
                    )
                if safe_project_id is not None and row[6] is None:
                    assignments.append("project_id = ?")
                    values.append(safe_project_id)
                values.extend((timestamp, thread_id, owner_id))
                connection.execute(
                    f"""
                    UPDATE agent_threads
                    SET {", ".join(assignments)}, updated_at = ?
                    WHERE thread_id = ? AND owner_id = ?
                    """,
                    values,
                )

    def list_threads(
        self,
        *,
        owner_id: str,
        channel: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[ConversationSummary]:
        safe_limit = max(1, min(limit, 100))
        parameters: list[object] = [owner_id]
        channel_filter = ""
        if channel is not None:
            channel_filter = "AND threads.channel = ?"
            parameters.append(channel)
        project_filter = ""
        if project_id is not None:
            project_filter = "AND threads.project_id = ?"
            parameters.append(project_id.strip()[:160])
        parameters.append(safe_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    threads.thread_id,
                    threads.owner_id,
                    threads.channel,
                    threads.project_id,
                    threads.title,
                    threads.created_at,
                    threads.updated_at,
                    COUNT(messages.id) AS message_count,
                    threads.title_ciphertext,
                    threads.title_nonce,
                    threads.encryption_version,
                    threads.key_id
                FROM agent_threads AS threads
                LEFT JOIN agent_messages AS messages
                    ON messages.thread_id = threads.thread_id
                    AND messages.owner_id = threads.owner_id
                WHERE threads.owner_id = ?
                {channel_filter}
                {project_filter}
                GROUP BY threads.thread_id
                ORDER BY threads.updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            ConversationSummary(
                thread_id=str(row[0]),
                owner_id=str(row[1]),
                channel=str(row[2]),
                project_id=str(row[3]) if row[3] is not None else None,
                title=self._decode_thread_title(
                    owner_id=str(row[1]),
                    thread_id=str(row[0]),
                    plaintext=row[4],
                    ciphertext=row[8],
                    nonce=row[9],
                    encryption_version=row[10],
                    key_id=row[11],
                ),
                created_at=float(row[5]),
                updated_at=float(row[6]),
                message_count=int(row[7]),
            )
            for row in rows
        ]

    def get_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
        project_id: str | None = None,
    ) -> ConversationSummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    threads.thread_id,
                    threads.owner_id,
                    threads.channel,
                    threads.project_id,
                    threads.title,
                    threads.created_at,
                    threads.updated_at,
                    COUNT(messages.id) AS message_count,
                    threads.title_ciphertext,
                    threads.title_nonce,
                    threads.encryption_version,
                    threads.key_id
                FROM agent_threads AS threads
                LEFT JOIN agent_messages AS messages
                    ON messages.thread_id = threads.thread_id
                    AND messages.owner_id = threads.owner_id
                WHERE threads.owner_id = ? AND threads.thread_id = ?
                  AND (? IS NULL OR threads.project_id = ?)
                GROUP BY threads.thread_id
                """,
                (owner_id, thread_id, project_id, project_id),
            ).fetchone()
        if row is None:
            return None
        return ConversationSummary(
            thread_id=str(row[0]),
            owner_id=str(row[1]),
            channel=str(row[2]),
            project_id=str(row[3]) if row[3] is not None else None,
            title=self._decode_thread_title(
                owner_id=str(row[1]),
                thread_id=str(row[0]),
                plaintext=row[4],
                ciphertext=row[8],
                nonce=row[9],
                encryption_version=row[10],
                key_id=row[11],
            ),
            created_at=float(row[5]),
            updated_at=float(row[6]),
            message_count=int(row[7]),
        )

    def list_messages(
        self,
        *,
        owner_id: str,
        thread_id: str,
        limit: int = 200,
    ) -> list[StoredConversationMessage]:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        safe_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at, run_id, metadata_json,
                       payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (owner_id, thread_id, safe_limit),
            ).fetchall()
        messages: list[StoredConversationMessage] = []
        for row in reversed(rows):
            payload = self._decrypt_payload(
                "message",
                owner_id,
                str(row[0]),
                row[6],
                row[7],
                row[8],
                row[9],
            )
            if payload is None:
                try:
                    metadata = json.loads(str(row[5] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                content = str(row[2])
            else:
                metadata = payload.get("metadata", {})
                content = str(payload.get("content", ""))
            if not isinstance(metadata, dict):
                metadata = {}
            messages.append(
                StoredConversationMessage(
                    id=int(row[0]),
                    role=str(row[1]),
                    content=content,
                    created_at=float(row[3]),
                    run_id=str(row[4]) if row[4] is not None else None,
                    metadata=metadata,
                )
            )
        return messages

    def rename_thread(
        self,
        *,
        owner_id: str,
        thread_id: str,
        title: str,
    ) -> None:
        safe_title = title.strip()[:80]
        if not safe_title:
            raise ValueError("会话标题不能为空。")
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        title_ciphertext = title_nonce = None
        encryption_version = 0
        key_id = None
        stored_title = safe_title
        if self._cipher is not None:
            (
                title_ciphertext,
                title_nonce,
                encryption_version,
                key_id,
            ) = self._encrypt_payload(
                "thread",
                owner_id,
                thread_id,
                {"title": safe_title},
            )
            stored_title = ""
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_threads
                SET title = ?, title_ciphertext = ?, title_nonce = ?,
                    encryption_version = ?, key_id = ?, updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (
                    stored_title,
                    title_ciphertext,
                    title_nonce,
                    encryption_version,
                    key_id,
                    time.time(),
                    owner_id,
                    thread_id,
                ),
            )

    def delete_thread(self, *, owner_id: str, thread_id: str) -> int:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM agent_runs
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            deleted_messages = connection.execute(
                """
                DELETE FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).rowcount
            connection.execute(
                """
                DELETE FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            connection.execute(
                """
                DELETE FROM agent_threads
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
        return deleted_messages

    def _insert_message_record(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        thread_id: str,
        role: str,
        content: str,
        timestamp: float,
        run_id: str | None,
        metadata: dict[str, Any],
    ) -> int:
        safe_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cursor = connection.execute(
            """
            INSERT INTO agent_messages (
                owner_id, thread_id, role, content, created_at,
                run_id, metadata_json, payload_ciphertext, payload_nonce,
                encryption_version, key_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL)
            """,
            (
                owner_id,
                thread_id,
                role,
                content if self._cipher is None else "",
                timestamp,
                run_id,
                safe_metadata if self._cipher is None else "{}",
            ),
        )
        message_id = int(cursor.lastrowid)
        if self._cipher is not None:
            ciphertext, nonce, version, key_id = self._encrypt_payload(
                "message",
                owner_id,
                str(message_id),
                {"content": content, "metadata": metadata},
            )
            connection.execute(
                """
                UPDATE agent_messages
                SET payload_ciphertext = ?, payload_nonce = ?,
                    encryption_version = ?, key_id = ?
                WHERE id = ?
                """,
                (ciphertext, nonce, version, key_id, message_id),
            )
        return message_id

    def _touch_thread_after_message(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        thread_id: str,
        role: str,
        content: str,
        timestamp: float,
    ) -> None:
        thread_row = connection.execute(
            """
            SELECT title, title_ciphertext, title_nonce,
                   encryption_version, key_id
            FROM agent_threads
            WHERE owner_id = ? AND thread_id = ?
            """,
            (owner_id, thread_id),
        ).fetchone()
        current_title = (
            self._decode_thread_title(
                owner_id=owner_id,
                thread_id=thread_id,
                plaintext=thread_row[0],
                ciphertext=thread_row[1],
                nonce=thread_row[2],
                encryption_version=thread_row[3],
                key_id=thread_row[4],
            )
            if thread_row is not None
            else "新对话"
        )
        if role == "user" and current_title == "新对话":
            auto_title = re.sub(r"[\r\n]+", " ", content)[:24]
            if self._cipher is None:
                connection.execute(
                    """
                    UPDATE agent_threads SET title = ?, updated_at = ?
                    WHERE owner_id = ? AND thread_id = ?
                    """,
                    (auto_title, timestamp, owner_id, thread_id),
                )
            else:
                title_ciphertext, title_nonce, version, key_id = (
                    self._encrypt_payload(
                        "thread",
                        owner_id,
                        thread_id,
                        {"title": auto_title},
                    )
                )
                connection.execute(
                    """
                    UPDATE agent_threads
                    SET title = '', title_ciphertext = ?, title_nonce = ?,
                        encryption_version = ?, key_id = ?, updated_at = ?
                    WHERE owner_id = ? AND thread_id = ?
                    """,
                    (
                        title_ciphertext,
                        title_nonce,
                        version,
                        key_id,
                        timestamp,
                        owner_id,
                        thread_id,
                    ),
                )
        else:
            connection.execute(
                """
                UPDATE agent_threads SET updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (timestamp, owner_id, thread_id),
            )

    def append_message(
        self,
        *,
        owner_id: str,
        thread_id: str,
        role: str,
        content: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        safe_content = content.strip()[:12000]
        if not safe_content:
            return
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            self._insert_message_record(
                connection,
                owner_id=owner_id,
                thread_id=thread_id,
                role=role,
                content=safe_content,
                timestamp=timestamp,
                run_id=run_id,
                metadata=metadata or {},
            )
            self._touch_thread_after_message(
                connection,
                owner_id=owner_id,
                thread_id=thread_id,
                role=role,
                content=safe_content,
                timestamp=timestamp,
            )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)

    def append_messages_batch(
        self,
        *,
        owner_id: str,
        thread_id: str,
        messages: list[tuple[str, str]],
        channel: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Durably append an ordered batch and compact bounded source chunks."""

        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
        )
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            for offset, (role, content) in enumerate(messages):
                safe_content = str(content).strip()[:12000]
                if not safe_content:
                    continue
                safe_role = role if role in {"user", "assistant"} else "user"
                event_time = timestamp + offset * 1e-6
                self._insert_message_record(
                    connection,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    role=safe_role,
                    content=safe_content,
                    timestamp=event_time,
                    run_id=None,
                    metadata={},
                )
                self._touch_thread_after_message(
                    connection,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    role=safe_role,
                    content=safe_content,
                    timestamp=event_time,
                )
        while self._refresh_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
        ):
            pass

    def update_run_user_message_metadata(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Update the already-durable user event without changing its identity."""

        safe_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, content, payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND role = 'user' AND run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (owner_id, thread_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("找不到这个 Agent Run 对应的用户消息。")
            message_id = int(row[0])
            payload = self._decrypt_payload(
                "message",
                owner_id,
                str(message_id),
                row[2],
                row[3],
                row[4],
                row[5],
            )
            content = (
                str(payload.get("content", ""))
                if payload is not None
                else str(row[1])
            )
            if self._cipher is None:
                connection.execute(
                    """
                    UPDATE agent_messages SET metadata_json = ? WHERE id = ?
                    """,
                    (safe_metadata, message_id),
                )
            else:
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "message",
                    owner_id,
                    str(message_id),
                    {"content": content, "metadata": metadata},
                )
                connection.execute(
                    """
                    UPDATE agent_messages
                    SET content = '', metadata_json = '{}',
                        payload_ciphertext = ?, payload_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE id = ?
                    """,
                    (ciphertext, nonce, version, key_id, message_id),
                )
            connection.execute(
                """
                DELETE FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ? AND last_message_id >= ?
                """,
                (owner_id, thread_id, message_id),
            )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)

    def upsert_assistant_run_message(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        content: str,
        metadata: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            project_id=project_id,
        )
        safe_content = content.strip()[:12000]
        if not safe_content:
            return
        timestamp = time.time()
        safe_metadata = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND role = 'assistant' AND run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (owner_id, thread_id, run_id),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO agent_messages (
                        owner_id, thread_id, role, content, created_at,
                        run_id, metadata_json, payload_ciphertext, payload_nonce,
                        encryption_version, key_id
                    ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, NULL, NULL, 0, NULL)
                    """,
                    (
                        owner_id,
                        thread_id,
                        safe_content if self._cipher is None else "",
                        timestamp,
                        run_id,
                        safe_metadata if self._cipher is None else "{}",
                    ),
                )
                message_id = int(cursor.lastrowid)
                if self._cipher is not None:
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "message",
                        owner_id,
                        str(message_id),
                        {"content": safe_content, "metadata": metadata},
                    )
                    connection.execute(
                        """
                        UPDATE agent_messages
                        SET payload_ciphertext = ?, payload_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE id = ?
                        """,
                        (ciphertext, nonce, version, key_id, message_id),
                    )
            else:
                message_id = int(row[0])
                if self._cipher is None:
                    connection.execute(
                        """
                        UPDATE agent_messages
                        SET content = ?, metadata_json = ?
                        WHERE id = ?
                        """,
                        (safe_content, safe_metadata, message_id),
                    )
                else:
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "message",
                        owner_id,
                        str(message_id),
                        {"content": safe_content, "metadata": metadata},
                    )
                    connection.execute(
                        """
                        UPDATE agent_messages
                        SET content = '', metadata_json = '{}',
                            payload_ciphertext = ?, payload_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE id = ?
                        """,
                        (ciphertext, nonce, version, key_id, message_id),
                    )
                connection.execute(
                    """
                    DELETE FROM agent_session_summaries
                    WHERE owner_id = ? AND thread_id = ?
                      AND last_message_id >= ?
                    """,
                    (owner_id, thread_id, message_id),
                )
            connection.execute(
                """
                UPDATE agent_threads
                SET updated_at = ?
                WHERE owner_id = ? AND thread_id = ?
                """,
                (timestamp, owner_id, thread_id),
            )
        self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)

    def create_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        thread_id: str,
        channel: str,
        message: str,
        project_id: str | None = None,
        checkpoint_thread_id: str | None = None,
        persist_user_message: bool = False,
        user_metadata: dict[str, Any] | None = None,
    ) -> int | None:
        self.ensure_thread(
            owner_id=owner_id,
            thread_id=thread_id,
            channel=channel,
            project_id=project_id,
        )
        timestamp = time.time()
        safe_run_message = message.strip()[:4000]
        safe_conversation_message = message.strip()[:12000]
        if not safe_run_message:
            raise ValueError("Agent Run 请求不能为空。")
        user_message_id: int | None = None
        with self._lock, self._connect() as connection:
            pending = connection.execute(
                """
                SELECT run_id FROM agent_runs
                WHERE owner_id = ? AND thread_id = ?
                  AND status IN (
                      'running', 'waiting_approval', 'recoverable',
                      'resuming', 'needs_attention'
                  )
                LIMIT 1
                """,
                (owner_id, thread_id),
            ).fetchone()
            if pending is not None:
                raise ValueError(
                    f"当前会话仍有待审批任务：{pending[0]}"
                )
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, owner_id, thread_id, channel, message,
                    status, approval_json, response_json,
                    created_at, updated_at, payload_ciphertext, payload_nonce,
                    encryption_version, key_id, checkpoint_thread_id,
                    project_id
                ) VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_id,
                    thread_id,
                    channel,
                    safe_run_message if self._cipher is None else "",
                    timestamp,
                    timestamp,
                    *(
                        self._encrypt_payload(
                            "run",
                            owner_id,
                            run_id,
                            {
                                "message": safe_run_message,
                                "approval": None,
                                "response": None,
                            },
                        )
                        if self._cipher is not None
                        else (None, None, 0, None)
                    ),
                    checkpoint_thread_id,
                    project_id.strip()[:160]
                    if isinstance(project_id, str) and project_id.strip()
                    else None,
                ),
            )
            if persist_user_message:
                user_message_id = self._insert_message_record(
                    connection,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    role="user",
                    content=safe_conversation_message,
                    timestamp=timestamp,
                    run_id=run_id,
                    metadata=user_metadata or {},
                )
                self._touch_thread_after_message(
                    connection,
                    owner_id=owner_id,
                    thread_id=thread_id,
                    role="user",
                    content=safe_conversation_message,
                    timestamp=timestamp,
                )
        if persist_user_message:
            self._refresh_session_summary(owner_id=owner_id, thread_id=thread_id)
        return user_message_id

    def update_run(
        self,
        *,
        run_id: str,
        status: str,
        approval: dict[str, Any] | None,
        response: dict[str, Any],
    ) -> None:
        approval_json = (
            json.dumps(approval, ensure_ascii=False, separators=(",", ":"))
            if approval is not None
            else None
        )
        response_json = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            if self._cipher is None:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?, approval_json = ?, response_json = ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        approval_json,
                        response_json,
                        time.time(),
                        run_id,
                    ),
                )
            else:
                row = connection.execute(
                    """
                    SELECT owner_id, message, approval_json, response_json,
                           payload_ciphertext, payload_nonce,
                           encryption_version, key_id
                    FROM agent_runs WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("找不到这个 Agent Run。")
                payload = self._decrypt_payload(
                    "run",
                    str(row[0]),
                    run_id,
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                )
                message = (
                    str(payload.get("message", ""))
                    if payload is not None
                    else str(row[1])
                )
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "run",
                    str(row[0]),
                    run_id,
                    {
                        "message": message,
                        "approval": approval,
                        "response": response,
                    },
                )
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?, message = '', approval_json = NULL,
                        response_json = NULL, payload_ciphertext = ?,
                        payload_nonce = ?, encryption_version = ?, key_id = ?,
                        updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        ciphertext,
                        nonce,
                        version,
                        key_id,
                        time.time(),
                        run_id,
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("找不到这个 Agent Run。")

    def get_run(self, run_id: str) -> StoredAgentRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT run_id, owner_id, thread_id, channel, message,
                       status, approval_json, response_json,
                       created_at, updated_at, payload_ciphertext,
                       payload_nonce, encryption_version, key_id,
                       checkpoint_thread_id, project_id
                FROM agent_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def rotate_encryption_key(self, new_key: bytes) -> str:
        """Re-encrypt every protected record in one rollback-safe transaction.

        The caller must update its external secret/keyring to ``new_key`` only
        after this method returns. A raised exception leaves both the database
        and the active in-process key unchanged.
        """

        if self._cipher is None:
            raise MemoryEncryptionError("未启用上下文记忆加密，无法轮换密钥。")
        if not self._externally_managed_encryption_key:
            raise MemoryEncryptionError(
                "自动钥匙串/文件密钥暂不支持在线轮换；请先切换为外部 Secret 管理。"
            )
        new_cipher = MemoryCipher(new_key)
        if new_cipher.key_id == self._cipher.key_id:
            return new_cipher.key_id

        with self._lock, self._connect() as connection:
            memory_rows = connection.execute(
                f"SELECT {_MEMORY_SELECT_COLUMNS} FROM agent_memories"
            ).fetchall()
            memory_records: dict[str, MemoryRecord] = {}
            for row in memory_rows:
                record = self._memory_from_row(row)
                memory_records[record.id] = record
                normalized = re.sub(r"\s+", " ", record.content).casefold()
                content_ciphertext, content_nonce = new_cipher.encrypt(
                    record.content,
                    aad=memory_aad(record.owner_id, record.id),
                )
                metadata_ciphertext, metadata_nonce = new_cipher.encrypt(
                    json.dumps(
                        record.metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    aad=memory_aad(record.owner_id, record.id, "metadata"),
                )
                claim = _normalize_memory_claim(
                    record.content,
                    record.memory_type,
                    record.metadata,
                )
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET normalized_content = ?, content_ciphertext = ?,
                        content_nonce = ?, metadata_ciphertext = ?,
                        metadata_nonce = ?, encryption_version = ?, key_id = ?,
                        claim_key = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (
                        new_cipher.blind_index(normalized),
                        content_ciphertext,
                        content_nonce,
                        metadata_ciphertext,
                        metadata_nonce,
                        new_cipher.version,
                        new_cipher.key_id,
                        _memory_claim_storage_key(claim, new_cipher),
                        record.id,
                        record.owner_id,
                    ),
                )

            embedding_rows = connection.execute(
                """
                SELECT memory_id, owner_id, model_id, dimension,
                       vector_ciphertext, vector_nonce,
                       encryption_version, key_id
                FROM agent_memory_embeddings
                """
            ).fetchall()
            for row in embedding_rows:
                raw = bytes(row[4])
                if int(row[6] or 0) > 0:
                    if row[5] is None:
                        raise MemoryEncryptionError("Embedding 密文缺少 nonce。")
                    raw = self._cipher_for(row[7]).decrypt_bytes(
                        raw,
                        bytes(row[5]),
                        aad=record_aad(
                            "embedding",
                            str(row[1]),
                            str(row[0]),
                            "vector",
                        ),
                    )
                ciphertext, nonce = new_cipher.encrypt_bytes(
                    raw,
                    aad=record_aad(
                        "embedding",
                        str(row[1]),
                        str(row[0]),
                        "vector",
                    ),
                )
                record = memory_records.get(str(row[0]))
                fingerprint = (
                    self._embedding_fingerprint(
                        record.content,
                        cipher=new_cipher,
                        model_id=str(row[2]),
                    )
                    if record is not None
                    else ""
                )
                connection.execute(
                    """
                    UPDATE agent_memory_embeddings
                    SET content_fingerprint = ?, vector_ciphertext = ?,
                        vector_nonce = ?, encryption_version = ?, key_id = ?,
                        updated_at = ?
                    WHERE memory_id = ? AND owner_id = ?
                    """,
                    (
                        fingerprint,
                        ciphertext,
                        nonce,
                        new_cipher.version,
                        new_cipher.key_id,
                        time.time(),
                        str(row[0]),
                        str(row[1]),
                    ),
                )

            evidence_rows = connection.execute(
                """
                SELECT evidence_id, owner_id, excerpt, payload_ciphertext,
                       payload_nonce, encryption_version, key_id
                FROM agent_memory_evidence
                """
            ).fetchall()
            for row in evidence_rows:
                payload = self._decrypt_payload(
                    "evidence",
                    str(row[1]),
                    str(row[0]),
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                )
                excerpt = (
                    str(payload.get("excerpt", ""))
                    if payload is not None
                    else str(row[2])
                )
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "evidence",
                    str(row[1]),
                    str(row[0]),
                    {"excerpt": excerpt},
                    cipher=new_cipher,
                )
                connection.execute(
                    """
                    UPDATE agent_memory_evidence
                    SET excerpt = '', payload_ciphertext = ?, payload_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE evidence_id = ? AND owner_id = ?
                    """,
                    (
                        ciphertext,
                        nonce,
                        version,
                        key_id,
                        str(row[0]),
                        str(row[1]),
                    ),
                )

            thread_rows = connection.execute(
                """
                SELECT thread_id, owner_id, title, title_ciphertext,
                       title_nonce, encryption_version, key_id
                FROM agent_threads
                """
            ).fetchall()
            for row in thread_rows:
                title = self._decode_thread_title(
                    owner_id=str(row[1]),
                    thread_id=str(row[0]),
                    plaintext=row[2],
                    ciphertext=row[3],
                    nonce=row[4],
                    encryption_version=row[5],
                    key_id=row[6],
                )
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "thread",
                    str(row[1]),
                    str(row[0]),
                    {"title": title},
                    cipher=new_cipher,
                )
                connection.execute(
                    """
                    UPDATE agent_threads
                    SET title = '', title_ciphertext = ?, title_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE thread_id = ? AND owner_id = ?
                    """,
                    (ciphertext, nonce, version, key_id, str(row[0]), str(row[1])),
                )

            message_rows = connection.execute(
                """
                SELECT id, owner_id, content, metadata_json,
                       payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                """
            ).fetchall()
            for row in message_rows:
                payload = self._decrypt_payload(
                    "message",
                    str(row[1]),
                    str(row[0]),
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                )
                if payload is None:
                    payload = {
                        "content": str(row[2]),
                        "metadata": _decode_json_object(row[3]) or {},
                    }
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "message",
                    str(row[1]),
                    str(row[0]),
                    payload,
                    cipher=new_cipher,
                )
                connection.execute(
                    """
                    UPDATE agent_messages
                    SET content = '', metadata_json = '{}',
                        payload_ciphertext = ?, payload_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (ciphertext, nonce, version, key_id, int(row[0]), str(row[1])),
                )

            run_rows = connection.execute(
                """
                SELECT run_id, owner_id, message, approval_json, response_json,
                       payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_runs
                """
            ).fetchall()
            for row in run_rows:
                payload = self._decrypt_payload(
                    "run",
                    str(row[1]),
                    str(row[0]),
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                )
                if payload is None:
                    payload = {
                        "message": str(row[2]),
                        "approval": _decode_json_object(row[3]),
                        "response": _decode_json_object(row[4]),
                    }
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "run",
                    str(row[1]),
                    str(row[0]),
                    payload,
                    cipher=new_cipher,
                )
                connection.execute(
                    """
                    UPDATE agent_runs
                    SET message = '', approval_json = NULL, response_json = NULL,
                        payload_ciphertext = ?, payload_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE run_id = ? AND owner_id = ?
                    """,
                    (ciphertext, nonce, version, key_id, str(row[0]), str(row[1])),
                )

            summary_rows = connection.execute(
                """
                SELECT owner_id, thread_id, summary, open_loops_json,
                       decisions_json, completed_actions_json,
                       active_assumptions_json, artifact_refs_json,
                       blockers_json, next_goal, payload_ciphertext,
                       payload_nonce, encryption_version, key_id,
                       provenance_json
                FROM agent_session_summaries
                """
            ).fetchall()
            for row in summary_rows:
                payload = self._decrypt_payload(
                    "summary",
                    str(row[0]),
                    str(row[1]),
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                )
                if payload is None:
                    payload = {
                        "summary": str(row[2]),
                        "open_loops": _decode_string_list(row[3]),
                        "decisions": _decode_string_list(row[4]),
                        "completed_actions": _decode_string_list(row[5]),
                        "active_assumptions": _decode_string_list(row[6]),
                        "artifact_refs": _decode_string_list(row[7]),
                        "blockers": _decode_string_list(row[8]),
                        "next_goal": str(row[9]) if row[9] else None,
                        "provenance": _decode_summary_provenance(row[14]),
                    }
                ciphertext, nonce, version, key_id = self._encrypt_payload(
                    "summary",
                    str(row[0]),
                    str(row[1]),
                    payload,
                    cipher=new_cipher,
                )
                connection.execute(
                    """
                    UPDATE agent_session_summaries
                    SET summary = '', open_loops_json = '[]',
                        decisions_json = '[]', completed_actions_json = '[]',
                        active_assumptions_json = '[]', artifact_refs_json = '[]',
                        blockers_json = '[]', next_goal = NULL,
                        provenance_json = '{}',
                        payload_ciphertext = ?, payload_nonce = ?,
                        encryption_version = ?, key_id = ?
                    WHERE owner_id = ? AND thread_id = ?
                    """,
                    (
                        ciphertext,
                        nonce,
                        version,
                        key_id,
                        str(row[0]),
                        str(row[1]),
                    ),
                )

        self._ciphers[new_cipher.key_id] = new_cipher
        self._cipher = new_cipher
        return new_cipher.key_id

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int | None = 50,
    ) -> list[StoredAgentRun]:
        safe_limit = max(1, min(limit, 1000)) if limit is not None else None
        limit_clause = "LIMIT ?" if safe_limit is not None else ""
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    f"""
                    SELECT run_id, owner_id, thread_id, channel, message,
                           status, approval_json, response_json,
                           created_at, updated_at, payload_ciphertext,
                           payload_nonce, encryption_version, key_id,
                           checkpoint_thread_id, project_id
                    FROM agent_runs
                    ORDER BY updated_at DESC
                    {limit_clause}
                    """,
                    (safe_limit,) if safe_limit is not None else (),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""
                    SELECT run_id, owner_id, thread_id, channel, message,
                           status, approval_json, response_json,
                           created_at, updated_at, payload_ciphertext,
                           payload_nonce, encryption_version, key_id,
                           checkpoint_thread_id, project_id
                    FROM agent_runs
                    WHERE status = ?
                    ORDER BY updated_at DESC
                    {limit_clause}
                    """,
                    (
                        (status, safe_limit)
                        if safe_limit is not None
                        else (status,)
                    ),
                ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def begin_resume(self, *, run_id: str, owner_id: str | None) -> bool:
        with self._lock, self._connect() as connection:
            if owner_id is None:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND status = 'waiting_approval'
                    """,
                    (time.time(), run_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND owner_id = ?
                      AND status = 'waiting_approval'
                    """,
                    (time.time(), run_id, owner_id),
                )
            return cursor.rowcount == 1

    def begin_continue(self, *, run_id: str, owner_id: str | None) -> bool:
        with self._lock, self._connect() as connection:
            if owner_id is None:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND status = 'recoverable'
                    """,
                    (time.time(), run_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'resuming', updated_at = ?
                    WHERE run_id = ? AND owner_id = ?
                      AND status = 'recoverable'
                    """,
                    (time.time(), run_id, owner_id),
                )
            return cursor.rowcount == 1

    def begin_abandon(self, *, run_id: str, owner_id: str | None) -> bool:
        """Atomically claim an interrupted run for explicit abandonment."""

        with self._lock, self._connect() as connection:
            if owner_id is None:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', updated_at = ?
                    WHERE run_id = ?
                      AND status IN ('recoverable', 'needs_attention')
                    """,
                    (time.time(), run_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = 'failed', updated_at = ?
                    WHERE run_id = ? AND owner_id = ?
                      AND status IN ('recoverable', 'needs_attention')
                    """,
                    (time.time(), run_id, owner_id),
                )
            return cursor.rowcount == 1

    def link_verified_identity(
        self,
        *,
        alias_owner_id: str,
        subject_id: str,
        verification_method: str,
    ) -> None:
        """Store an explicitly verified identity binding without auto-merging data."""

        safe_alias = alias_owner_id.strip()[:200]
        safe_subject = subject_id.strip()[:200]
        safe_method = verification_method.strip()[:80]
        if not safe_alias or not safe_subject or not safe_method:
            raise ValueError("身份映射必须包含别名、主体和验证方式。")
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_identity_links (
                    alias_owner_id, subject_id, verification_method,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alias_owner_id) DO UPDATE SET
                    subject_id = excluded.subject_id,
                    verification_method = excluded.verification_method,
                    updated_at = excluded.updated_at
                """,
                (safe_alias, safe_subject, safe_method, timestamp, timestamp),
            )

    def resolve_verified_subject_id(self, owner_id: str) -> str:
        """Resolve a verified binding; callers choose when cross-channel use is safe."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT subject_id FROM agent_identity_links
                WHERE alias_owner_id = ?
                """,
                (owner_id,),
            ).fetchone()
        return str(row[0]) if row is not None else owner_id

    def shadow_gate_status(self, kind: str) -> dict[str, Any]:
        if kind not in {"summary", "retrieval"}:
            raise ValueError("shadow kind 必须是 summary 或 retrieval。")
        minimum_samples = _bounded_memory_int(
            os.getenv("AGENT_CONTEXT_SHADOW_MIN_SAMPLES"),
            default=20,
            lower=1,
            upper=10_000,
        )
        minimum_pass_rate = _bounded_memory_float(
            os.getenv("AGENT_CONTEXT_SHADOW_MIN_PASS_RATE"),
            default=0.95,
            lower=0.0,
            upper=1.0,
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(passed), 0)
                FROM agent_context_shadow_comparisons
                WHERE kind = ?
                """,
                (kind,),
            ).fetchone()
        samples = int(row[0]) if row is not None else 0
        passed = int(row[1]) if row is not None else 0
        pass_rate = passed / samples if samples else 0.0
        return {
            "kind": kind,
            "samples": samples,
            "passed": passed,
            "pass_rate": pass_rate,
            "minimum_samples": minimum_samples,
            "minimum_pass_rate": minimum_pass_rate,
            "ready": (
                samples >= minimum_samples
                and pass_rate >= minimum_pass_rate
            ),
        }

    def _record_shadow_comparison(
        self,
        *,
        owner_id: str,
        thread_id: str | None,
        kind: str,
        input_value: str,
        baseline_id: str,
        candidate_id: str,
        passed: bool,
        metrics: dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_context_shadow_comparisons (
                    comparison_id, owner_id, thread_id, kind, input_hash,
                    baseline_id, candidate_id, passed, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    owner_id,
                    thread_id,
                    kind,
                    sha256(input_value.encode("utf-8")).hexdigest(),
                    baseline_id,
                    candidate_id,
                    1 if passed else 0,
                    json.dumps(metrics, separators=(",", ":"), sort_keys=True),
                    time.time(),
                ),
            )

    def remember_many(
        self,
        *,
        owner_id: str,
        content: str,
        source: str,
        memory_type: MemoryType | None = None,
        scope: MemoryScope = "user",
        scope_id: str | None = None,
        source_message_id: int | None = None,
        source_run_id: str | None = None,
        confidence: float = 1.0,
        importance: float = 0.65,
        sensitivity: MemorySensitivity | None = None,
        retrieval_policy: MemoryRetrievalPolicy | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        status: MemoryStatus = "active",
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """Persist stable facts as independently retrievable memory units.

        Splitting is conservative: when every clause cannot be recognized as a
        self-contained profile, preference, task, or procedure statement, the
        original text remains a single memory so no meaning is silently lost.
        """

        atoms = _split_atomic_memories(content)
        if any(_contains_forbidden_secret(atom) for atom in atoms):
            raise MemoryPolicyError(
                "检测到密钥、密码或验证码特征，已拒绝写入长期记忆。"
            )
        group_id = (
            sha256(
                "\x1f".join(
                    (
                        owner_id,
                        source,
                        str(source_message_id or ""),
                        str(source_run_id or ""),
                        content.strip(),
                    )
                ).encode("utf-8")
            ).hexdigest()
            if len(atoms) > 1
            else None
        )
        result: list[str] = []
        with self._lock, self._connect() as connection:
            for index, atom in enumerate(atoms):
                atom_metadata = dict(metadata or {})
                if group_id is not None:
                    atom_metadata.update(
                        {
                            "atomic_group_id": group_id,
                            "atomic_index": index,
                            "atomic_count": len(atoms),
                            "original_content_hash": sha256(
                                content.strip().encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                result.append(
                    self.remember(
                        owner_id=owner_id,
                        content=atom,
                        source=source,
                        memory_type=(
                            memory_type
                            if len(atoms) == 1
                            else _infer_memory_type(atom)
                        ),
                        scope=scope,
                        scope_id=scope_id,
                        source_message_id=source_message_id,
                        source_run_id=(source_run_id if len(atoms) == 1 else None),
                        confidence=confidence,
                        importance=importance,
                        sensitivity=sensitivity,
                        retrieval_policy=retrieval_policy,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        status=status,
                        metadata=atom_metadata,
                        _connection=connection,
                    )
                )
        return result

    def remember(
        self,
        *,
        owner_id: str,
        content: str,
        source: str,
        memory_type: MemoryType | None = None,
        scope: MemoryScope = "user",
        scope_id: str | None = None,
        source_message_id: int | None = None,
        source_run_id: str | None = None,
        confidence: float = 1.0,
        importance: float = 0.65,
        sensitivity: MemorySensitivity | None = None,
        retrieval_policy: MemoryRetrievalPolicy | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        status: MemoryStatus = "active",
        metadata: dict[str, Any] | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        if _connection is None:
            with self._lock, self._connect() as connection:
                return self.remember(
                    owner_id=owner_id,
                    content=content,
                    source=source,
                    memory_type=memory_type,
                    scope=scope,
                    scope_id=scope_id,
                    source_message_id=source_message_id,
                    source_run_id=source_run_id,
                    confidence=confidence,
                    importance=importance,
                    sensitivity=sensitivity,
                    retrieval_policy=retrieval_policy,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    status=status,
                    metadata=metadata,
                    _connection=connection,
                )
        safe_content = content.strip()[:2000]
        if not safe_content:
            raise ValueError("记忆内容不能为空。")
        if _contains_forbidden_secret(safe_content):
            raise MemoryPolicyError(
                "检测到密钥、密码或验证码特征，已拒绝写入长期记忆。"
            )
        safe_type = memory_type or _infer_memory_type(safe_content)
        if safe_type not in _MEMORY_TYPES:
            raise ValueError("记忆类型无效。")
        if scope not in _MEMORY_SCOPES:
            raise ValueError("记忆作用域无效。")
        if status not in _MEMORY_STATUSES:
            raise ValueError("记忆状态无效。")
        safe_scope = scope
        safe_scope_id = (
            (scope_id or "").strip()[:160] or None
            if safe_scope != "user"
            else None
        )
        if safe_scope != "user" and safe_scope_id is None:
            raise ValueError("非用户级记忆必须提供 scope_id。")
        safe_metadata_object = dict(metadata or {})
        claim = _normalize_memory_claim(
            safe_content,
            safe_type,
            safe_metadata_object,
        )
        claim_key = _memory_claim_storage_key(claim, self._cipher)
        claim_polarity = (
            str(claim.get("polarity", "affirmed"))
            if claim is not None
            else "affirmed"
        )
        safe_sensitivity = sensitivity or _infer_sensitivity(safe_content)
        if safe_sensitivity not in _MEMORY_SENSITIVITIES:
            raise ValueError("记忆敏感度无效。")
        safe_retrieval_policy = retrieval_policy or (
            "always" if safe_sensitivity == "normal" else "explicit_only"
        )
        if safe_retrieval_policy not in _MEMORY_RETRIEVAL_POLICIES:
            raise ValueError("记忆召回策略无效。")
        normalized = re.sub(r"\s+", " ", safe_content).casefold()
        if safe_type == "episode" and source_run_id:
            normalized = f"{normalized}\u241f{source_run_id}"
        elif safe_scope != "user":
            normalized = (
                f"{normalized}\u241f{safe_scope}\u241f{safe_scope_id or ''}"
            )
        topic_key = _memory_topic_key(safe_content, safe_type)
        memory_id = str(uuid4())
        stored_content = safe_content
        stored_normalized = normalized
        content_ciphertext: bytes | None = None
        content_nonce: bytes | None = None
        encryption_version = 0
        key_id: str | None = None
        if self._cipher is not None:
            content_ciphertext, content_nonce = self._cipher.encrypt(
                safe_content,
                aad=memory_aad(owner_id, memory_id),
            )
            stored_content = ""
            stored_normalized = self._cipher.blind_index(normalized)
            encryption_version = self._cipher.version
            key_id = self._cipher.key_id
        timestamp = time.time()
        safe_valid_from = timestamp if valid_from is None else float(valid_from)
        safe_valid_to = float(valid_to) if valid_to is not None else None
        if safe_valid_to is not None and safe_valid_to <= safe_valid_from:
            raise ValueError("记忆失效时间必须晚于生效时间。")
        safe_confidence = _clamp_score(confidence)
        safe_importance = _clamp_score(importance)
        safe_metadata = json.dumps(
            safe_metadata_object,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        stored_metadata = safe_metadata
        metadata_ciphertext: bytes | None = None
        metadata_nonce: bytes | None = None
        if self._cipher is not None:
            metadata_ciphertext, metadata_nonce = self._cipher.encrypt(
                safe_metadata,
                aad=memory_aad(owner_id, memory_id, "metadata"),
            )
            stored_metadata = "{}"
        with nullcontext(_connection) as connection:
            assert connection is not None
            if source_run_id:
                source_row = connection.execute(
                    """
                    SELECT id FROM agent_memories
                    WHERE owner_id = ? AND source_run_id = ?
                    LIMIT 1
                    """,
                    (owner_id, source_run_id),
                ).fetchone()
                if source_row is not None:
                    existing_id = str(source_row[0])
                    self._record_memory_evidence(
                        connection,
                        memory_id=existing_id,
                        owner_id=owner_id,
                        source=source,
                        excerpt=safe_content,
                        source_message_id=source_message_id,
                        source_run_id=source_run_id,
                        confidence=safe_confidence,
                        observed_at=safe_valid_from,
                    )
                    return existing_id
            existing = connection.execute(
                """
                SELECT id FROM agent_memories
                WHERE owner_id = ? AND normalized_content = ?
                """,
                (owner_id, stored_normalized),
            ).fetchone()
            if existing is not None:
                memory_id = str(existing[0])
                if self._cipher is not None:
                    content_ciphertext, content_nonce = self._cipher.encrypt(
                        safe_content,
                        aad=memory_aad(owner_id, memory_id),
                    )
                    metadata_ciphertext, metadata_nonce = self._cipher.encrypt(
                        safe_metadata,
                        aad=memory_aad(owner_id, memory_id, "metadata"),
                    )

            effective_status: MemoryStatus = status
            effective_valid_to = safe_valid_to
            supersedes_id: str | None = None
            if claim_key and status == "active":
                previous = connection.execute(
                    """
                    SELECT id, valid_from, content, content_ciphertext,
                           content_nonce, encryption_version
                    FROM agent_memories
                    WHERE owner_id = ? AND memory_type = ?
                      AND scope = ? AND COALESCE(scope_id, '') = COALESCE(?, '')
                      AND claim_key = ? AND status = 'active' AND id <> ?
                    ORDER BY valid_from DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (
                        owner_id,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        claim_key,
                        memory_id,
                    ),
                ).fetchone()
                if previous is not None:
                    previous_id = str(previous[0])
                    previous_valid_from = float(previous[1])
                    previous_content = str(previous[2])
                    if int(previous[5] or 0) > 0:
                        if self._cipher is None:
                            raise MemoryEncryptionError(
                                "旧版长期记忆已加密，但当前未启用解密。"
                            )
                        previous_content = self._cipher.decrypt(
                            bytes(previous[3]),
                            bytes(previous[4]),
                            aad=memory_aad(owner_id, previous_id),
                        )
                    if safe_valid_from >= previous_valid_from:
                        supersedes_id = previous_id
                        connection.execute(
                            """
                            UPDATE agent_memories
                            SET status = 'superseded',
                                valid_to = CASE
                                    WHEN valid_to IS NULL OR valid_to > ? THEN ?
                                    ELSE valid_to
                                END,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                safe_valid_from,
                                safe_valid_from,
                                timestamp,
                                previous_id,
                            ),
                        )
                        self._delete_fts(connection, previous_id)
                        connection.execute(
                            "DELETE FROM agent_memory_embeddings WHERE memory_id = ?",
                            (previous_id,),
                        )
                        self._record_memory_event(
                            connection,
                            memory_id=previous_id,
                            owner_id=owner_id,
                            event_type="superseded",
                            content=previous_content,
                            metadata={"superseded_by": memory_id},
                        )
                    else:
                        effective_status = "superseded"
                        if (
                            effective_valid_to is None
                            or effective_valid_to > previous_valid_from
                        ):
                            effective_valid_to = previous_valid_from
            if existing is not None:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET content = ?, normalized_content = ?,
                        content_ciphertext = ?, content_nonce = ?,
                        encryption_version = ?, key_id = ?,
                        memory_type = ?, scope = ?, scope_id = ?,
                        topic_key = ?, source = ?, source_message_id = ?,
                        source_run_id = COALESCE(?, source_run_id),
                        confidence = ?, importance = ?, sensitivity = ?,
                        retrieval_policy = ?,
                        valid_from = ?, valid_to = ?, status = ?,
                        supersedes_id = ?, metadata_json = ?,
                        metadata_ciphertext = ?, metadata_nonce = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        stored_content,
                        stored_normalized,
                        content_ciphertext,
                        content_nonce,
                        encryption_version,
                        key_id,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        topic_key,
                        source[:160],
                        source_message_id,
                        source_run_id,
                        safe_confidence,
                        safe_importance,
                        safe_sensitivity,
                        safe_retrieval_policy,
                        safe_valid_from,
                        effective_valid_to,
                        effective_status,
                        supersedes_id,
                        stored_metadata,
                        metadata_ciphertext,
                        metadata_nonce,
                        timestamp,
                        memory_id,
                    ),
                )
                event_type = "reinforced"
            else:
                connection.execute(
                    """
                    INSERT INTO agent_memories (
                        id, owner_id, content, normalized_content,
                        source, created_at, updated_at, memory_type,
                        scope, scope_id, topic_key, source_message_id,
                        source_run_id, confidence, importance, sensitivity,
                        retrieval_policy,
                        valid_from, valid_to, status, supersedes_id,
                        metadata_json, last_accessed_at, access_count,
                        utility_score, content_ciphertext, content_nonce,
                        encryption_version, key_id,
                        metadata_ciphertext, metadata_nonce
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, NULL, 0, 0.5, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        memory_id,
                        owner_id,
                        stored_content,
                        stored_normalized,
                        source[:120],
                        timestamp,
                        timestamp,
                        safe_type,
                        safe_scope,
                        safe_scope_id,
                        topic_key,
                        source_message_id,
                        source_run_id,
                        safe_confidence,
                        safe_importance,
                        safe_sensitivity,
                        safe_retrieval_policy,
                        safe_valid_from,
                        effective_valid_to,
                        effective_status,
                        supersedes_id,
                        stored_metadata,
                        content_ciphertext,
                        content_nonce,
                        encryption_version,
                        key_id,
                        metadata_ciphertext,
                        metadata_nonce,
                    ),
                )
                event_type = "created"
            connection.execute(
                """
                UPDATE agent_memories
                SET claim_key = ?, claim_polarity = ?
                WHERE id = ?
                """,
                (claim_key, claim_polarity, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memory_embeddings WHERE memory_id = ?",
                (memory_id,),
            )
            if effective_status == "active":
                self._upsert_fts(
                    connection,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    content=safe_content,
                    topic_key=topic_key,
                )
            else:
                self._delete_fts(connection, memory_id)
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                owner_id=owner_id,
                event_type=event_type,
                content=safe_content,
                metadata={
                    "source": source[:160],
                    "type": safe_type,
                    "claim_key": claim_key,
                    "claim_polarity": claim_polarity,
                },
            )
            self._record_memory_evidence(
                connection,
                memory_id=memory_id,
                owner_id=owner_id,
                source=source,
                excerpt=safe_content,
                source_message_id=source_message_id,
                source_run_id=source_run_id,
                confidence=safe_confidence,
                observed_at=safe_valid_from,
            )
        return memory_id

    def list_memories(self, owner_id: str, limit: int = 50) -> list[str]:
        return [
            item.content
            for item in self.list_memory_records(
                owner_id=owner_id,
                limit=limit,
            )
        ]

    def list_memory_records(
        self,
        *,
        owner_id: str,
        memory_type: MemoryType | None = None,
        scope: MemoryScope | None = None,
        status: MemoryStatus | None = "active",
        query: str | None = None,
        limit: int | None = 100,
    ) -> list[MemoryRecord]:
        clauses = ["owner_id = ?"]
        parameters: list[object] = [owner_id]
        post_filter_query: str | None = None
        if memory_type is not None:
            clauses.append("memory_type = ?")
            parameters.append(memory_type)
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if query and query.strip():
            if self._cipher is not None:
                post_filter_query = query.strip()[:160].casefold()
            else:
                clauses.append("content LIKE ? ESCAPE '\\'")
                escaped = (
                    query.strip()[:160]
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                parameters.append(f"%{escaped}%")
        limit_clause = ""
        if limit is not None and post_filter_query is None:
            parameters.append(max(1, min(limit, 500)))
            limit_clause = "LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY importance DESC, updated_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
        records = [self._memory_from_row(row) for row in rows]
        if post_filter_query is not None:
            records = [
                record
                for record in records
                if post_filter_query in record.content.casefold()
            ]
            if limit is not None:
                records = records[: max(1, min(limit, 500))]
        return self._attach_evidence_summaries(records)

    def get_memory(self, *, owner_id: str, memory_id: str) -> MemoryRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories
                WHERE owner_id = ? AND id = ?
                """,
                (owner_id, memory_id),
            ).fetchone()
        if row is None:
            return None
        return self._attach_evidence_summaries(
            [self._memory_from_row(row)]
        )[0]

    def update_memory(
        self,
        *,
        owner_id: str,
        memory_id: str,
        updates: dict[str, Any],
    ) -> MemoryRecord:
        current = self.get_memory(owner_id=owner_id, memory_id=memory_id)
        if current is None:
            raise ValueError("找不到这条记忆。")
        allowed = {
            "content",
            "memory_type",
            "scope",
            "scope_id",
            "confidence",
            "importance",
            "sensitivity",
            "retrieval_policy",
            "valid_from",
            "valid_to",
            "status",
            "metadata",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不支持更新字段：{sorted(unknown)[0]}")
        for field in {
            "content",
            "memory_type",
            "scope",
            "confidence",
            "importance",
            "sensitivity",
            "retrieval_policy",
            "valid_from",
            "status",
            "metadata",
        }:
            if field in updates and updates[field] is None:
                raise ValueError(f"{field} 不能设为空。")
        content = str(updates.get("content", current.content)).strip()[:2000]
        if not content:
            raise ValueError("记忆内容不能为空。")
        if _contains_forbidden_secret(content):
            raise MemoryPolicyError(
                "检测到密钥、密码或验证码特征，已拒绝写入长期记忆。"
            )
        memory_type = updates.get("memory_type", current.memory_type)
        scope = updates.get("scope", current.scope)
        scope_id = updates.get("scope_id", current.scope_id)
        if memory_type not in _MEMORY_TYPES or scope not in _MEMORY_SCOPES:
            raise ValueError("记忆类型或作用域无效。")
        if scope == "user":
            scope_id = None
        elif not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("非用户级记忆必须提供 scope_id。")
        sensitivity = updates.get("sensitivity", current.sensitivity)
        retrieval_policy = updates.get(
            "retrieval_policy",
            (
                "explicit_only"
                if "sensitivity" in updates
                and sensitivity != "normal"
                and current.retrieval_policy == "always"
                else current.retrieval_policy
            ),
        )
        status = updates.get("status", current.status)
        if sensitivity not in _MEMORY_SENSITIVITIES or status not in _MEMORY_STATUSES:
            raise ValueError("记忆敏感度或状态无效。")
        if retrieval_policy not in _MEMORY_RETRIEVAL_POLICIES:
            raise ValueError("记忆召回策略无效。")
        metadata = updates.get("metadata", current.metadata)
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须是对象。")
        next_valid_from = float(updates.get("valid_from", current.valid_from))
        next_valid_to = (
            (
                float(updates["valid_to"])
                if updates.get("valid_to") is not None
                else None
            )
            if "valid_to" in updates
            else current.valid_to
        )
        if next_valid_to is not None and next_valid_to <= next_valid_from:
            raise ValueError("记忆失效时间必须晚于生效时间。")
        normalized_content = re.sub(r"\s+", " ", content).casefold()
        stored_content = content
        stored_normalized = normalized_content
        content_ciphertext: bytes | None = None
        content_nonce: bytes | None = None
        encryption_version = 0
        key_id: str | None = None
        if self._cipher is not None:
            content_ciphertext, content_nonce = self._cipher.encrypt(
                content,
                aad=memory_aad(owner_id, memory_id),
            )
            stored_content = ""
            stored_normalized = self._cipher.blind_index(normalized_content)
            encryption_version = self._cipher.version
            key_id = self._cipher.key_id
        metadata_json = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        )
        stored_metadata = metadata_json
        metadata_ciphertext: bytes | None = None
        metadata_nonce: bytes | None = None
        if self._cipher is not None:
            metadata_ciphertext, metadata_nonce = self._cipher.encrypt(
                metadata_json,
                aad=memory_aad(owner_id, memory_id, "metadata"),
            )
            stored_metadata = "{}"
        update_timestamp = time.time()
        values = (
            stored_content,
            stored_normalized,
            content_ciphertext,
            content_nonce,
            encryption_version,
            key_id,
            memory_type,
            scope,
            str(scope_id).strip()[:160] if scope_id else None,
            _memory_topic_key(content, memory_type),
            _clamp_score(updates.get("confidence", current.confidence)),
            _clamp_score(updates.get("importance", current.importance)),
            sensitivity,
            retrieval_policy,
            next_valid_from,
            next_valid_to,
            status,
            stored_metadata,
            metadata_ciphertext,
            metadata_nonce,
            update_timestamp,
            owner_id,
            memory_id,
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET content = ?, normalized_content = ?,
                        content_ciphertext = ?, content_nonce = ?,
                        encryption_version = ?, key_id = ?, memory_type = ?,
                        scope = ?, scope_id = ?, topic_key = ?, confidence = ?,
                        importance = ?, sensitivity = ?, retrieval_policy = ?,
                        valid_from = ?,
                        valid_to = ?, status = ?, metadata_json = ?,
                        metadata_ciphertext = ?, metadata_nonce = ?, updated_at = ?
                    WHERE owner_id = ? AND id = ?
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("已经存在内容相同的记忆。") from exc
            connection.execute(
                "DELETE FROM agent_memory_embeddings WHERE memory_id = ?",
                (memory_id,),
            )
            if status == "active":
                self._upsert_fts(
                    connection,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    content=content,
                    topic_key=_memory_topic_key(content, memory_type),
                )
            else:
                self._delete_fts(connection, memory_id)
            self._record_memory_event(
                connection,
                memory_id=memory_id,
                owner_id=owner_id,
                event_type="updated",
                content=content,
                metadata={"fields": sorted(updates)},
            )
            if "content" in updates:
                self._record_memory_evidence(
                    connection,
                    memory_id=memory_id,
                    owner_id=owner_id,
                    source="memory:update",
                    excerpt=content,
                    source_message_id=None,
                    source_run_id=None,
                    confidence=_clamp_score(
                        updates.get("confidence", current.confidence)
                    ),
                    observed_at=update_timestamp,
                    source_type="revision",
                )
        updated = self.get_memory(owner_id=owner_id, memory_id=memory_id)
        assert updated is not None
        return updated

    def delete_memory(self, *, owner_id: str, memory_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM agent_memories WHERE owner_id = ? AND id = ?",
                (owner_id, memory_id),
            ).fetchone()
            if row is None:
                return False
            self._delete_fts(connection, memory_id)
            connection.execute(
                "DELETE FROM agent_memory_embeddings WHERE memory_id = ?",
                (memory_id,),
            )
            connection.execute(
                "DELETE FROM agent_memory_usage WHERE owner_id = ? AND memory_id = ?",
                (owner_id, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memory_events WHERE owner_id = ? AND memory_id = ?",
                (owner_id, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memory_evidence WHERE owner_id = ? AND memory_id = ?",
                (owner_id, memory_id),
            )
            connection.execute(
                "DELETE FROM agent_memories WHERE owner_id = ? AND id = ?",
                (owner_id, memory_id),
            )
            return True

    def search_memories(
        self,
        *,
        owner_id: str,
        query: str,
        thread_id: str | None = None,
        channel: str | None = None,
        project_id: str | None = None,
        limit: int = 8,
        track_access: bool = False,
    ) -> list[MemoryRecord]:
        now = time.time()
        query_terms = _memory_terms(query)
        query_hints = _memory_query_hints(query)
        recent_candidates = self.list_memory_records(
            owner_id=owner_id,
            status="active",
            limit=500,
        )
        candidate_map = {item.id: item for item in recent_candidates}
        hinted_types = {
            hint.removeprefix("type:")
            for hint in query_hints
            if hint.startswith("type:")
        }
        if any(hint.startswith("profile:") for hint in query_hints):
            hinted_types.add("profile")
        for memory_type in hinted_types:
            if memory_type not in _MEMORY_TYPES:
                continue
            for item in self.list_memory_records(
                owner_id=owner_id,
                memory_type=memory_type,  # type: ignore[arg-type]
                status="active",
                limit=200,
            ):
                candidate_map[item.id] = item
        candidates = list(candidate_map.values())
        candidates = [
            item
            for item in candidates
            if item.valid_from <= now
            and (item.valid_to is None or item.valid_to > now)
            and (
                item.scope == "user"
                or (item.scope == "thread" and item.scope_id == thread_id)
                or (item.scope == "channel" and item.scope_id == channel)
                or (item.scope == "project" and item.scope_id == project_id)
            )
        ]
        fts_ids = self._fts_candidate_ids(owner_id=owner_id, query=query)
        eligible = [
            item
            for item in candidates
            if _retrieval_policy_allows(item, query_hints)
        ]
        semantic_scores, semantic_vectors = self._semantic_scores(
            owner_id=owner_id,
            query=query,
            candidates=eligible,
        )
        if (
            query.strip()
            and not query_terms
            and not query_hints
            and not semantic_scores
        ):
            return []

        lexical_scores: dict[str, float] = {}
        intent_scores: dict[str, float] = {}
        prior_scores: dict[str, float] = {}
        allowed_ids: set[str] = set()
        for item in eligible:
            item_terms = _memory_terms(item.content + " " + (item.topic_key or ""))
            overlap = len(query_terms & item_terms) / max(1, len(query_terms))
            substring = (
                1.0
                if query.strip()
                and query.strip().casefold() in item.content.casefold()
                else 0.0
            )
            fts_score = 1.0 if item.id in fts_ids else 0.0
            intent_score = _memory_intent_score(item, query_hints)
            lexical_score = (
                0.70 * overlap + 0.20 * substring + 0.10 * fts_score
            )
            effective_type = (
                _infer_memory_type(item.content)
                if item.memory_type == "fact"
                else item.memory_type
            )
            if (
                hinted_types
                and effective_type not in hinted_types
                and substring == 0
                and overlap < 0.5
            ):
                continue
            age_days = max(0.0, (now - item.updated_at) / 86_400)
            recency = math.exp(-age_days / 90.0)
            scope_bonus = 1.0 if item.scope == "thread" else 0.75
            if item.memory_type in {"profile", "preference", "procedure"}:
                scope_bonus = max(scope_bonus, 0.9)
            evidence_support = 1.0 - math.exp(-item.evidence_count / 2.0)
            prior_scores[item.id] = (
                0.29 * recency
                + 0.22 * item.importance
                + 0.17 * item.confidence
                + 0.14 * item.utility_score
                + 0.08 * scope_bonus
                + 0.10 * evidence_support
            )
            if lexical_score > 0:
                lexical_scores[item.id] = lexical_score
            if intent_score > 0:
                intent_scores[item.id] = intent_score
            if not query.strip():
                allowed_ids.add(item.id)
            elif (
                lexical_score > 0
                or intent_score > 0
                or semantic_scores.get(item.id, -1.0) >= self._semantic_threshold
            ):
                allowed_ids.add(item.id)

        if not allowed_ids:
            return []

        route_rankings: list[tuple[float, list[str]]] = []
        if intent_scores:
            route_rankings.append(
                (1.35, _rank_score_map(intent_scores, allowed_ids))
            )
        if lexical_scores:
            route_rankings.append(
                (1.0, _rank_score_map(lexical_scores, allowed_ids))
            )
        semantic_route = {
            memory_id: score
            for memory_id, score in semantic_scores.items()
            if memory_id in allowed_ids and score >= self._semantic_threshold
        }
        if semantic_route:
            route_rankings.append(
                (1.1, _rank_score_map(semantic_route, allowed_ids))
            )
        if not query.strip() or not route_rankings:
            route_rankings.append(
                (0.8, _rank_score_map(prior_scores, allowed_ids))
            )

        rrf_scores: dict[str, float] = {memory_id: 0.0 for memory_id in allowed_ids}
        maximum_rrf = sum(
            weight / (self._rrf_k + 1) for weight, _ in route_rankings
        )
        for weight, ranking in route_rankings:
            for rank, memory_id in enumerate(ranking, start=1):
                rrf_scores[memory_id] += weight / (self._rrf_k + rank)

        scored: list[MemoryRecord] = []
        for item in eligible:
            if item.id not in allowed_ids:
                continue
            fused = rrf_scores[item.id] / max(maximum_rrf, 1e-9)
            relevance = 0.78 * fused + 0.22 * prior_scores[item.id]
            scored.append(
                MemoryRecord(
                    **{
                        **item.__dict__,
                        "relevance_score": round(relevance, 6),
                    }
                )
            )
        selected = _mmr_select_memories(
            scored,
            semantic_vectors=semantic_vectors,
            limit=max(1, min(limit, 20)),
            diversity_lambda=self._mmr_lambda,
        )
        safe_limit = max(1, min(limit, 20))
        legacy_allowed = (
            allowed_ids
            if not query.strip()
            else set(lexical_scores) | set(intent_scores)
        )
        legacy_ranked = sorted(
            (item for item in eligible if item.id in legacy_allowed),
            key=lambda item: (
                intent_scores.get(item.id, 0.0),
                lexical_scores.get(item.id, 0.0),
                prior_scores.get(item.id, 0.0),
                item.updated_at,
            ),
            reverse=True,
        )[:safe_limit]
        legacy_selected = [
            MemoryRecord(
                **{
                    **item.__dict__,
                    "relevance_score": round(
                        max(
                            intent_scores.get(item.id, 0.0),
                            lexical_scores.get(item.id, 0.0),
                        ),
                        6,
                    ),
                }
            )
            for item in legacy_ranked
        ]
        retrieval_mode = os.getenv(
            "AGENT_MEMORY_RETRIEVAL_MODE",
            "auto",
        ).strip().lower()
        if retrieval_mode not in {"legacy", "shadow", "auto", "hybrid"}:
            retrieval_mode = "auto"
        if retrieval_mode in {"shadow", "auto"}:
            baseline_ids = [item.id for item in legacy_selected]
            candidate_ids = [item.id for item in selected]
            required = set(baseline_ids[: min(3, len(baseline_ids))])
            passed = required.issubset(set(candidate_ids))
            self._record_shadow_comparison(
                owner_id=owner_id,
                thread_id=thread_id,
                kind="retrieval",
                input_value=query,
                baseline_id="lexical-intent-v1",
                candidate_id="hybrid-rrf-mmr-v2",
                passed=passed,
                metrics={
                    "baseline_count": len(baseline_ids),
                    "candidate_count": len(candidate_ids),
                    "top3_baseline_recalled": len(required & set(candidate_ids)),
                    "top3_baseline_required": len(required),
                },
            )
            if (
                retrieval_mode == "shadow"
                or not self.shadow_gate_status("retrieval")["ready"]
            ):
                selected = legacy_selected
        elif retrieval_mode == "legacy":
            selected = legacy_selected
        elif (
            _env_truthy("AGENT_MEMORY_RETRIEVAL_REQUIRE_SHADOW_PASS")
            and not self.shadow_gate_status("retrieval")["ready"]
        ):
            selected = legacy_selected
        if track_access and selected:
            with self._lock, self._connect() as connection:
                for item in selected:
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET last_accessed_at = ?, access_count = access_count + 1
                        WHERE owner_id = ? AND id = ?
                        """,
                        (now, owner_id, item.id),
                    )
        return selected

    def record_memory_usage(
        self,
        *,
        owner_id: str,
        run_id: str,
        memories: list[MemoryRecord],
    ) -> None:
        if not memories:
            return
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            for rank, item in enumerate(memories, start=1):
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_memory_usage (
                        memory_id, owner_id, run_id, rank, relevance_score,
                        outcome, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        item.id,
                        owner_id,
                        run_id,
                        rank,
                        item.relevance_score,
                        timestamp,
                        timestamp,
                    ),
                )
                if cursor.rowcount:
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET last_accessed_at = ?, access_count = access_count + 1
                        WHERE owner_id = ? AND id = ?
                        """,
                        (timestamp, owner_id, item.id),
                    )

    def complete_memory_usage(self, *, run_id: str, outcome: str) -> None:
        delta = 0.04 if outcome == "completed" else -0.08
        timestamp = time.time()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_id, owner_id FROM agent_memory_usage
                WHERE run_id = ? AND outcome IS NULL
                """,
                (run_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE agent_memory_usage
                SET outcome = ?, updated_at = ?
                WHERE run_id = ? AND outcome IS NULL
                """,
                (outcome, timestamp, run_id),
            )
            for memory_id, owner_id in rows:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET utility_score = MIN(1.0, MAX(0.0, utility_score + ?))
                    WHERE id = ? AND owner_id = ?
                    """,
                    (delta, str(memory_id), str(owner_id)),
                )

    def record_episode(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        task: str,
        answer: str,
        status: str,
        tool_names: list[str],
    ) -> str | None:
        if not tool_names and status == "completed":
            return None
        tools = "、".join(dict.fromkeys(tool_names)) or "未调用工具"
        outcome = "成功" if status == "completed" else "失败"
        content = (
            f"任务：{task.strip()[:500]}\n"
            f"执行：{tools}\n"
            f"结果（{outcome}）：{answer.strip()[:700]}"
        )
        try:
            return self.remember(
                owner_id=owner_id,
                content=content,
                source=f"run:{run_id}",
                memory_type="episode",
                scope="user",
                source_run_id=run_id,
                confidence=1.0,
                importance=0.62 if status == "failed" else 0.55,
                metadata={
                    "thread_id": thread_id,
                    "status": status,
                    "tool_names": list(dict.fromkeys(tool_names)),
                },
            )
        except MemoryPolicyError:
            return None

    def get_session_summary(
        self,
        *,
        owner_id: str,
        thread_id: str,
    ) -> SessionMemorySummary | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, open_loops_json, decisions_json,
                       completed_actions_json, active_assumptions_json,
                       artifact_refs_json, blockers_json, next_goal,
                       last_message_id, updated_at, payload_ciphertext,
                       payload_nonce, encryption_version, key_id,
                       summary_provider, summary_schema_version,
                       fallback_reason, covered_from_message_id,
                       provenance_json
                FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).fetchone()
        if row is None:
            return None
        payload = self._decrypt_payload(
            "summary",
            owner_id,
            thread_id,
            row[10],
            row[11],
            row[12],
            row[13],
        )
        if payload is not None:
            return SessionMemorySummary(
                summary=str(payload.get("summary", "")),
                open_loops=_coerce_string_list(payload.get("open_loops")),
                decisions=_coerce_string_list(payload.get("decisions")),
                completed_actions=_coerce_string_list(
                    payload.get("completed_actions")
                ),
                active_assumptions=_coerce_string_list(
                    payload.get("active_assumptions")
                ),
                artifact_refs=_coerce_string_list(payload.get("artifact_refs")),
                blockers=_coerce_string_list(payload.get("blockers")),
                next_goal=(
                    str(payload.get("next_goal"))
                    if payload.get("next_goal")
                    else None
                ),
                last_message_id=int(row[8]),
                updated_at=float(row[9]),
                provider_id=str(row[14]),
                schema_version=str(row[15]),
                fallback_reason=str(row[16]) if row[16] else None,
                covered_from_message_id=(
                    int(row[17]) if row[17] is not None else None
                ),
                provenance=_coerce_summary_provenance(payload.get("provenance")),
            )
        return SessionMemorySummary(
            summary=str(row[0]),
            open_loops=_decode_string_list(row[1]),
            decisions=_decode_string_list(row[2]),
            completed_actions=_decode_string_list(row[3]),
            active_assumptions=_decode_string_list(row[4]),
            artifact_refs=_decode_string_list(row[5]),
            blockers=_decode_string_list(row[6]),
            next_goal=str(row[7]) if row[7] else None,
            last_message_id=int(row[8]),
            updated_at=float(row[9]),
            provider_id=str(row[14]),
            schema_version=str(row[15]),
            fallback_reason=str(row[16]) if row[16] else None,
            covered_from_message_id=(
                int(row[17]) if row[17] is not None else None
            ),
            provenance=_decode_summary_provenance(row[18]),
        )

    def verify_session_summary_provenance(
        self,
        *,
        owner_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        summary = self.get_session_summary(
            owner_id=owner_id,
            thread_id=thread_id,
        )
        if summary is None:
            return {
                "valid": True,
                "checked_items": 0,
                "checked_sources": 0,
                "untraced_items": [],
                "missing_message_ids": [],
                "hash_mismatches": [],
            }
        provenance = _coerce_summary_provenance(summary.provenance)
        untraced = [
            {"field": item["field"], "text": item["text"]}
            for item in provenance
            if not item.get("sources")
        ]
        expected: dict[int, str] = {}
        for item in provenance:
            for source in item.get("sources", []):
                expected[int(source["message_id"])] = str(
                    source["content_sha256"]
                )
        actual: dict[int, str] = {}
        if expected:
            placeholders = ",".join("?" for _ in expected)
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT id, role, content, payload_ciphertext, payload_nonce,
                           encryption_version, key_id
                    FROM agent_messages
                    WHERE owner_id = ? AND thread_id = ?
                      AND id IN ({placeholders})
                    """,
                    (owner_id, thread_id, *sorted(expected)),
                ).fetchall()
            for message_id, role, content in self._decode_summary_source_rows(
                owner_id,
                rows,
            ):
                actual[message_id] = _summary_event_hash(
                    message_id,
                    role,
                    content,
                )
        missing = sorted(set(expected) - set(actual))
        mismatches = sorted(
            message_id
            for message_id, expected_hash in expected.items()
            if message_id in actual and actual[message_id] != expected_hash
        )
        return {
            "valid": not untraced and not missing and not mismatches,
            "checked_items": len(provenance),
            "checked_sources": len(expected),
            "untraced_items": untraced,
            "missing_message_ids": missing,
            "hash_mismatches": mismatches,
        }

    def forget_all(self, owner_id: str) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM agent_memories WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
            for row in rows:
                self._delete_fts(connection, str(row[0]))
            connection.execute(
                "DELETE FROM agent_memory_embeddings WHERE owner_id = ?",
                (owner_id,),
            )
            connection.execute(
                "DELETE FROM agent_memory_usage WHERE owner_id = ?",
                (owner_id,),
            )
            connection.execute(
                "DELETE FROM agent_memory_events WHERE owner_id = ?",
                (owner_id,),
            )
            connection.execute(
                "DELETE FROM agent_memory_evidence WHERE owner_id = ?",
                (owner_id,),
            )
            cursor = connection.execute(
                "DELETE FROM agent_memories WHERE owner_id = ?",
                (owner_id,),
            )
            return cursor.rowcount

    def clear_thread(self, *, owner_id: str, thread_id: str) -> int:
        self.ensure_thread(owner_id=owner_id, thread_id=thread_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            cursor = connection.execute(
                """
                DELETE FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            )
            return cursor.rowcount

    def _refresh_session_summary(
        self,
        *,
        owner_id: str,
        thread_id: str,
        raw_history_token_budget: int | None = None,
        summary_token_budget: int | None = None,
    ) -> bool:
        """Compact older turns into validated structured state.

        A configured provider is the primary path. The deterministic extractor
        remains an observable fallback only, and provider inference never holds
        the SQLite write lock.
        """

        recent_message_limit = _bounded_memory_int(
            os.getenv("AGENT_RECENT_HISTORY_MESSAGES"),
            default=20,
            lower=1,
            upper=20,
        )
        raw_token_budget = (
            _bounded_memory_int(
                os.getenv("AGENT_RECENT_HISTORY_TOKENS"),
                default=32_768,
                lower=128,
                upper=32_768,
            )
            if raw_history_token_budget is None
            else min(max(int(raw_history_token_budget), 128), 32_768)
        )
        safe_summary_budget = (
            _bounded_memory_int(
                os.getenv("AGENT_SESSION_SUMMARY_TOKENS"),
                default=8_192,
                lower=128,
                upper=8_192,
            )
            if summary_token_budget is None
            else min(max(int(summary_token_budget), 128), 8_192)
        )

        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT summary, open_loops_json, decisions_json,
                       completed_actions_json, active_assumptions_json,
                       artifact_refs_json, blockers_json, next_goal,
                       last_message_id, payload_ciphertext, payload_nonce,
                       encryption_version, key_id, summary_provider,
                       summary_schema_version, fallback_reason,
                       covered_from_message_id, provenance_json
                FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).fetchone()
            previous_last_id = int(existing[8]) if existing is not None else 0
            rows = connection.execute(
                """
                SELECT id, role, content, payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND id > ?
                ORDER BY id ASC
                """,
                (owner_id, thread_id, previous_last_id),
            ).fetchall()
        if not rows:
            return False

        decoded_rows = self._decode_summary_source_rows(owner_id, rows)
        retained_count = 0
        retained_tokens = 0
        for _, _, raw_content in reversed(decoded_rows):
            cost = self._token_counter.count(raw_content) + 8
            if retained_count == 0:
                retained_count = 1
                retained_tokens = cost
                continue
            if (
                retained_count >= recent_message_limit
                or retained_tokens + cost > raw_token_budget
            ):
                break
            retained_count += 1
            retained_tokens += cost

        compact_rows = decoded_rows[:-retained_count]
        if not compact_rows:
            return False
        maximum_source_messages = _bounded_memory_int(
            os.getenv("AGENT_SESSION_COMPACTION_MESSAGES"),
            default=240,
            lower=20,
            upper=500,
        )
        compact_rows = compact_rows[:maximum_source_messages]
        cutoff_id = compact_rows[-1][0]
        source_fingerprint = _summary_source_fingerprint(compact_rows)
        existing_payload = (
            self._decrypt_payload(
                "summary",
                owner_id,
                thread_id,
                existing[9],
                existing[10],
                existing[11],
                existing[12],
            )
            if existing is not None
            else None
        )
        previous = _summary_state_from_storage(existing, existing_payload)

        provider_id = "fallback:extractive-v2"
        fallback_reason: str | None = "provider_unavailable"
        candidate_state: dict[str, Any] | None = None
        candidate_provider_id = "provider:unavailable"
        provider = self._summary_provider
        if provider is not None:
            candidate_provider_id = str(provider.summary_provider_id)[:200]
            try:
                candidate = provider.summarize_session(
                    previous=_summary_provider_state(previous),
                    messages=[
                        {"message_id": item_id, "role": role, "content": content}
                        for item_id, role, content in compact_rows
                    ],
                    token_budget=safe_summary_budget,
                )
                candidate_state = _merge_summary_invariants(
                    previous,
                    _validate_summary_state(candidate),
                    compact_rows,
                    token_counter=self._token_counter,
                )
                fallback_reason = None
            except Exception as exc:
                fallback_reason = type(exc).__name__[:120]
                LOGGER.warning(
                    "Structured session summarization failed; using the "
                    "deterministic fallback (%s: %s).",
                    type(exc).__name__,
                    str(exc),
                )
        summary_mode = os.getenv(
            "AGENT_SESSION_SUMMARY_MODE",
            "auto",
        ).strip().lower()
        if summary_mode not in {"legacy", "shadow", "auto", "structured"}:
            summary_mode = "auto"
        baseline_state: dict[str, Any] | None = None
        if summary_mode in {"legacy", "shadow", "auto"} or candidate_state is None:
            baseline_state = _extractive_summary_fallback(
                previous,
                compact_rows,
                token_budget=safe_summary_budget,
                token_counter=self._token_counter,
            )
        if summary_mode in {"shadow", "auto"}:
            assert baseline_state is not None
            passed, metrics = _compare_summary_states(
                baseline_state,
                candidate_state,
            )
            self._record_shadow_comparison(
                owner_id=owner_id,
                thread_id=thread_id,
                kind="summary",
                input_value=source_fingerprint,
                baseline_id="fallback:extractive-v2",
                candidate_id=candidate_provider_id,
                passed=passed,
                metrics=metrics,
            )
            if (
                summary_mode == "auto"
                and candidate_state is not None
                and self.shadow_gate_status("summary")["ready"]
            ):
                state = candidate_state
                provider_id = candidate_provider_id
                fallback_reason = None
            else:
                state = baseline_state
                provider_id = (
                    "shadow-baseline:extractive-v2"
                    if summary_mode == "shadow"
                    else "gated-baseline:extractive-v2"
                )
                fallback_reason = (
                    "shadow_candidate_observed"
                    if summary_mode == "shadow"
                    else "shadow_gate_not_passed"
                )
        elif summary_mode == "legacy":
            assert baseline_state is not None
            state = baseline_state
            provider_id = "fallback:extractive-v2"
            fallback_reason = "legacy_mode"
        elif (
            _env_truthy("AGENT_SESSION_SUMMARY_REQUIRE_SHADOW_PASS")
            and not self.shadow_gate_status("summary")["ready"]
        ):
            state = baseline_state or _extractive_summary_fallback(
                previous,
                compact_rows,
                token_budget=safe_summary_budget,
                token_counter=self._token_counter,
            )
            provider_id = "gated-baseline:extractive-v2"
            fallback_reason = "shadow_gate_not_passed"
        elif candidate_state is not None:
            state = candidate_state
            provider_id = candidate_provider_id
            fallback_reason = None
        else:
            assert baseline_state is not None
            state = baseline_state
        state = _fit_summary_state_to_budget(
            state,
            safe_summary_budget,
            token_counter=self._token_counter,
        )
        state["provenance"] = _attach_summary_provenance(
            state,
            previous=previous,
            compact_rows=compact_rows,
        )

        summary_text = str(state["summary"])
        open_loops = list(state["open_loops"])
        decisions = list(state["decisions"])
        completed_actions = list(state["completed_actions"])
        active_assumptions = list(state["active_assumptions"])
        artifact_refs = list(state["artifact_refs"])
        blockers = list(state["blockers"])
        next_goal = state["next_goal"]
        timestamp = time.time()
        stored_summary = summary_text
        stored_open_loops = json.dumps(open_loops, ensure_ascii=False)
        stored_decisions = json.dumps(decisions, ensure_ascii=False)
        stored_completed_actions = json.dumps(completed_actions, ensure_ascii=False)
        stored_active_assumptions = json.dumps(active_assumptions, ensure_ascii=False)
        stored_artifact_refs = json.dumps(artifact_refs, ensure_ascii=False)
        stored_blockers = json.dumps(blockers, ensure_ascii=False)
        stored_next_goal = next_goal
        stored_provenance = json.dumps(
            state["provenance"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload_ciphertext = payload_nonce = None
        encryption_version = 0
        key_id = None
        if self._cipher is not None:
            (
                payload_ciphertext,
                payload_nonce,
                encryption_version,
                key_id,
            ) = self._encrypt_payload(
                "summary",
                owner_id,
                thread_id,
                state,
            )
            stored_summary = ""
            stored_open_loops = "[]"
            stored_decisions = "[]"
            stored_completed_actions = "[]"
            stored_active_assumptions = "[]"
            stored_artifact_refs = "[]"
            stored_blockers = "[]"
            stored_next_goal = None
            stored_provenance = "{}"

        covered_from_message_id = (
            int(existing[16])
            if existing is not None and existing[16] is not None
            else compact_rows[0][0]
        )
        with self._lock, self._connect() as connection:
            current_summary = connection.execute(
                """
                SELECT last_message_id FROM agent_session_summaries
                WHERE owner_id = ? AND thread_id = ?
                """,
                (owner_id, thread_id),
            ).fetchone()
            if previous_last_id == 0:
                if current_summary is not None and int(current_summary[0]) != 0:
                    return False
            elif (
                current_summary is None
                or int(current_summary[0]) != previous_last_id
            ):
                return False
            current_rows = connection.execute(
                """
                SELECT id, role, content, payload_ciphertext, payload_nonce,
                       encryption_version, key_id
                FROM agent_messages
                WHERE owner_id = ? AND thread_id = ?
                  AND id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (owner_id, thread_id, previous_last_id, cutoff_id),
            ).fetchall()
            if _summary_source_fingerprint(
                self._decode_summary_source_rows(owner_id, current_rows)
            ) != source_fingerprint:
                return False
            cursor = connection.execute(
                """
                INSERT INTO agent_session_summaries (
                    owner_id, thread_id, summary, open_loops_json,
                    decisions_json, completed_actions_json,
                    active_assumptions_json, artifact_refs_json, blockers_json,
                    next_goal, last_message_id, updated_at,
                    payload_ciphertext, payload_nonce, encryption_version, key_id,
                    summary_provider, summary_schema_version, fallback_reason,
                    covered_from_message_id, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, thread_id) DO UPDATE SET
                    summary = excluded.summary,
                    open_loops_json = excluded.open_loops_json,
                    decisions_json = excluded.decisions_json,
                    completed_actions_json = excluded.completed_actions_json,
                    active_assumptions_json = excluded.active_assumptions_json,
                    artifact_refs_json = excluded.artifact_refs_json,
                    blockers_json = excluded.blockers_json,
                    next_goal = excluded.next_goal,
                    last_message_id = excluded.last_message_id,
                    updated_at = excluded.updated_at,
                    payload_ciphertext = excluded.payload_ciphertext,
                    payload_nonce = excluded.payload_nonce,
                    encryption_version = excluded.encryption_version,
                    key_id = excluded.key_id,
                    summary_provider = excluded.summary_provider,
                    summary_schema_version = excluded.summary_schema_version,
                    fallback_reason = excluded.fallback_reason,
                    covered_from_message_id = excluded.covered_from_message_id,
                    provenance_json = excluded.provenance_json
                WHERE agent_session_summaries.last_message_id = ?
                """,
                (
                    owner_id,
                    thread_id,
                    stored_summary,
                    stored_open_loops,
                    stored_decisions,
                    stored_completed_actions,
                    stored_active_assumptions,
                    stored_artifact_refs,
                    stored_blockers,
                    stored_next_goal,
                    cutoff_id,
                    timestamp,
                    payload_ciphertext,
                    payload_nonce,
                    encryption_version,
                    key_id,
                    provider_id,
                    SESSION_SUMMARY_SCHEMA_VERSION,
                    fallback_reason,
                    covered_from_message_id,
                    stored_provenance,
                    previous_last_id,
                ),
            )
            return cursor.rowcount > 0

    def _decode_summary_source_rows(
        self,
        owner_id: str,
        rows: list[sqlite3.Row] | list[tuple[Any, ...]],
    ) -> list[tuple[int, str, str]]:
        decoded: list[tuple[int, str, str]] = []
        for row in rows:
            payload = self._decrypt_payload(
                "message",
                owner_id,
                str(row[0]),
                row[3],
                row[4],
                row[5],
                row[6],
            )
            decoded.append(
                (
                    int(row[0]),
                    str(row[1]),
                    str(
                        payload.get("content", "")
                        if payload is not None
                        else row[2]
                    ),
                )
            )
        return decoded

    def _record_memory_event(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        owner_id: str,
        event_type: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO agent_memory_events (
                event_id, memory_id, owner_id, event_type,
                content_hash, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                memory_id,
                owner_id,
                event_type[:40],
                sha256(content.encode("utf-8")).hexdigest(),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            ),
        )

    def _record_memory_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        owner_id: str,
        source: str,
        excerpt: str,
        source_message_id: int | None,
        source_run_id: str | None,
        confidence: float,
        observed_at: float,
        source_type: str | None = None,
    ) -> str:
        safe_excerpt = excerpt.strip()[:2000]
        safe_source = source.strip()[:160] or "unknown"
        content_hash = sha256(safe_excerpt.encode("utf-8")).hexdigest()
        evidence_id = sha256(
            "\x1f".join(
                (
                    owner_id,
                    memory_id,
                    safe_source,
                    str(source_message_id or ""),
                    str(source_run_id or ""),
                    content_hash,
                )
            ).encode("utf-8")
        ).hexdigest()
        stored_excerpt = safe_excerpt
        payload_ciphertext = payload_nonce = None
        encryption_version = 0
        key_id = None
        if self._cipher is not None:
            (
                payload_ciphertext,
                payload_nonce,
                encryption_version,
                key_id,
            ) = self._encrypt_payload(
                "evidence",
                owner_id,
                evidence_id,
                {"excerpt": safe_excerpt},
            )
            stored_excerpt = ""
        connection.execute(
            """
            INSERT OR IGNORE INTO agent_memory_evidence (
                evidence_id, memory_id, owner_id, source_type, source,
                excerpt, content_hash, source_message_id, source_run_id,
                confidence, observed_at, created_at, payload_ciphertext,
                payload_nonce, encryption_version, key_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                memory_id,
                owner_id,
                source_type or _memory_evidence_source_type(
                    source=safe_source,
                    source_message_id=source_message_id,
                    source_run_id=source_run_id,
                ),
                safe_source,
                stored_excerpt,
                content_hash,
                source_message_id,
                source_run_id,
                _clamp_score(confidence),
                float(observed_at),
                time.time(),
                payload_ciphertext,
                payload_nonce,
                encryption_version,
                key_id,
            ),
        )
        return evidence_id

    def list_memory_evidence(
        self,
        *,
        owner_id: str,
        memory_id: str,
        limit: int = 50,
    ) -> list[MemoryEvidence]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, memory_id, owner_id, source_type, source,
                       excerpt, content_hash, source_message_id, source_run_id,
                       confidence, observed_at, created_at, payload_ciphertext,
                       payload_nonce, encryption_version, key_id
                FROM agent_memory_evidence
                WHERE owner_id = ? AND memory_id = ?
                ORDER BY observed_at DESC, created_at DESC
                LIMIT ?
                """,
                (owner_id, memory_id, max(1, min(limit, 200))),
            ).fetchall()
        evidence: list[MemoryEvidence] = []
        for row in rows:
            payload = self._decrypt_payload(
                "evidence",
                owner_id,
                str(row[0]),
                row[12],
                row[13],
                row[14],
                row[15],
            )
            evidence.append(
                MemoryEvidence(
                    evidence_id=str(row[0]),
                    memory_id=str(row[1]),
                    owner_id=str(row[2]),
                    source_type=str(row[3]),
                    source=str(row[4]),
                    excerpt=(
                        str(payload.get("excerpt", ""))
                        if payload is not None
                        else str(row[5])
                    ),
                    content_hash=str(row[6]),
                    source_message_id=(
                        int(row[7]) if row[7] is not None else None
                    ),
                    source_run_id=str(row[8]) if row[8] is not None else None,
                    confidence=float(row[9]),
                    observed_at=float(row[10]),
                    created_at=float(row[11]),
                )
            )
        return evidence

    def _attach_evidence_summaries(
        self,
        records: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        if not records:
            return records
        memory_ids = [record.id for record in records]
        placeholders = ",".join("?" for _ in memory_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT memory_id, COUNT(*), GROUP_CONCAT(source, char(30))
                FROM agent_memory_evidence
                WHERE memory_id IN ({placeholders})
                GROUP BY memory_id
                """,
                memory_ids,
            ).fetchall()
        summaries = {
            str(row[0]): (
                int(row[1]),
                tuple(
                    dict.fromkeys(
                        item for item in str(row[2] or "").split(chr(30)) if item
                    )
                )[:8],
            )
            for row in rows
        }
        return [
            MemoryRecord(
                **{
                    **record.__dict__,
                    "evidence_count": summaries.get(record.id, (0, ()))[0],
                    "evidence_refs": summaries.get(record.id, (0, ()))[1],
                }
            )
            for record in records
        ]

    def _backfill_legacy_memory_evidence(self) -> None:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories AS memory
                WHERE NOT EXISTS (
                    SELECT 1 FROM agent_memory_evidence AS evidence
                    WHERE evidence.memory_id = memory.id
                      AND evidence.owner_id = memory.owner_id
                )
                """
            ).fetchall()
            for row in rows:
                record = self._memory_from_row(row)
                self._record_memory_evidence(
                    connection,
                    memory_id=record.id,
                    owner_id=record.owner_id,
                    source=record.source,
                    excerpt=record.content,
                    source_message_id=record.source_message_id,
                    source_run_id=record.source_run_id,
                    confidence=record.confidence,
                    observed_at=record.valid_from,
                    source_type="legacy_claim",
                )

    def _backfill_memory_claim_keys(self) -> None:
        """Add deterministic claim identities to legacy typed memories."""

        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEMORY_SELECT_COLUMNS}
                FROM agent_memories
                WHERE claim_key IS NULL
                """
            ).fetchall()
            for row in rows:
                record = self._memory_from_row(row)
                claim = _normalize_memory_claim(
                    record.content,
                    record.memory_type,
                    record.metadata,
                )
                claim_key = _memory_claim_storage_key(claim, self._cipher)
                if claim_key is None:
                    continue
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET claim_key = ?, claim_polarity = ?
                    WHERE id = ? AND owner_id = ?
                    """,
                    (
                        claim_key,
                        str(claim.get("polarity", "affirmed")),
                        record.id,
                        record.owner_id,
                    ),
                )

    def _upsert_fts(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        owner_id: str,
        content: str,
        topic_key: str | None,
    ) -> None:
        if not self._fts_available:
            return
        self._delete_fts(connection, memory_id)
        connection.execute(
            """
            INSERT INTO agent_memory_fts (
                memory_id, owner_id, content, topic_key
            ) VALUES (?, ?, ?, ?)
            """,
            (memory_id, owner_id, content, topic_key or ""),
        )

    def _delete_fts(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> None:
        if self._fts_available:
            connection.execute(
                "DELETE FROM agent_memory_fts WHERE memory_id = ?",
                (memory_id,),
            )

    def _fts_candidate_ids(self, *, owner_id: str, query: str) -> set[str]:
        if not self._fts_available:
            return set()
        terms = sorted(_memory_terms(query), key=len, reverse=True)[:12]
        if not terms:
            return set()
        match_query = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT memory_id FROM agent_memory_fts
                    WHERE owner_id = ? AND agent_memory_fts MATCH ?
                    LIMIT 100
                    """,
                    (owner_id, match_query),
                ).fetchall()
            return {str(row[0]) for row in rows}
        except sqlite3.OperationalError:
            return set()

    def _memory_from_row(self, row: sqlite3.Row | tuple[Any, ...]) -> MemoryRecord:
        metadata_raw = str(row[23] or "{}")
        encryption_version = int(row[26] or 0)
        if encryption_version > 0 and row[28] is not None and row[29] is not None:
            metadata_raw = self._cipher_for(row[27]).decrypt(
                bytes(row[28]),
                bytes(row[29]),
                aad=memory_aad(str(row[1]), str(row[0]), "metadata"),
            )
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        content = str(row[5])
        if encryption_version > 0:
            if row[24] is None or row[25] is None:
                raise MemoryEncryptionError("长期记忆密文缺少 nonce 或密文数据。")
            content = self._cipher_for(row[27]).decrypt(
                bytes(row[24]),
                bytes(row[25]),
                aad=memory_aad(str(row[1]), str(row[0])),
            )
        return MemoryRecord(
            id=str(row[0]),
            owner_id=str(row[1]),
            memory_type=str(row[2]),  # type: ignore[arg-type]
            scope=str(row[3]),  # type: ignore[arg-type]
            scope_id=str(row[4]) if row[4] is not None else None,
            content=content,
            topic_key=str(row[6]) if row[6] is not None else None,
            source=str(row[7]),
            source_message_id=int(row[8]) if row[8] is not None else None,
            source_run_id=str(row[9]) if row[9] is not None else None,
            confidence=float(row[10]),
            importance=float(row[11]),
            sensitivity=str(row[12]),  # type: ignore[arg-type]
            retrieval_policy=str(row[13]),  # type: ignore[arg-type]
            valid_from=float(row[14]),
            valid_to=float(row[15]) if row[15] is not None else None,
            status=str(row[16]),  # type: ignore[arg-type]
            supersedes_id=str(row[17]) if row[17] is not None else None,
            created_at=float(row[18]),
            updated_at=float(row[19]),
            last_accessed_at=float(row[20]) if row[20] is not None else None,
            access_count=int(row[21]),
            utility_score=float(row[22]),
            metadata=metadata,
        )

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted_plaintext_rows = 0
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_threads (
                    thread_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'unknown',
                    project_id TEXT,
                    title TEXT NOT NULL DEFAULT '新对话',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    title_ciphertext BLOB,
                    title_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    payload_ciphertext BLOB,
                    payload_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    FOREIGN KEY(thread_id) REFERENCES agent_threads(thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memories (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    scope TEXT NOT NULL DEFAULT 'user',
                    scope_id TEXT,
                    topic_key TEXT,
                    source_message_id INTEGER,
                    source_run_id TEXT,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    importance REAL NOT NULL DEFAULT 0.65,
                    sensitivity TEXT NOT NULL DEFAULT 'normal',
                    retrieval_policy TEXT NOT NULL DEFAULT 'always',
                    valid_from REAL NOT NULL DEFAULT 0,
                    valid_to REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    supersedes_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    last_accessed_at REAL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    utility_score REAL NOT NULL DEFAULT 0.5,
                    content_ciphertext BLOB,
                    content_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    metadata_ciphertext BLOB,
                    metadata_nonce BLOB,
                    claim_key TEXT,
                    claim_polarity TEXT NOT NULL DEFAULT 'affirmed',
                    UNIQUE(owner_id, normalized_content)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    project_id TEXT,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approval_json TEXT,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload_ciphertext BLOB,
                    payload_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    checkpoint_thread_id TEXT,
                    FOREIGN KEY(thread_id) REFERENCES agent_threads(thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_embeddings (
                    memory_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    dimension INTEGER NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    vector_ciphertext BLOB NOT NULL,
                    vector_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES agent_memories(id)
                );

                CREATE TABLE IF NOT EXISTS agent_session_summaries (
                    owner_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    open_loops_json TEXT NOT NULL DEFAULT '[]',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    completed_actions_json TEXT NOT NULL DEFAULT '[]',
                    active_assumptions_json TEXT NOT NULL DEFAULT '[]',
                    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                    blockers_json TEXT NOT NULL DEFAULT '[]',
                    next_goal TEXT,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    payload_ciphertext BLOB,
                    payload_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    summary_provider TEXT NOT NULL DEFAULT 'fallback:extractive-v1',
                    summary_schema_version TEXT NOT NULL DEFAULT 'session-summary-v3',
                    fallback_reason TEXT,
                    covered_from_message_id INTEGER,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(owner_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_events (
                    event_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_memory_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_message_id INTEGER,
                    source_run_id TEXT,
                    confidence REAL NOT NULL,
                    observed_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    payload_ciphertext BLOB,
                    payload_nonce BLOB,
                    encryption_version INTEGER NOT NULL DEFAULT 0,
                    key_id TEXT,
                    FOREIGN KEY(memory_id) REFERENCES agent_memories(id)
                );

                CREATE TABLE IF NOT EXISTS agent_memory_usage (
                    memory_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    relevance_score REAL NOT NULL,
                    outcome TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(memory_id, run_id)
                );

                CREATE TABLE IF NOT EXISTS agent_identity_links (
                    alias_owner_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    verification_method TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_context_shadow_comparisons (
                    comparison_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    thread_id TEXT,
                    kind TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    baseline_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS agent_messages_thread_idx
                ON agent_messages(owner_id, thread_id, id DESC);

                CREATE INDEX IF NOT EXISTS agent_memories_owner_idx
                ON agent_memories(owner_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_memory_embeddings_owner_idx
                ON agent_memory_embeddings(owner_id, model_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_runs_status_idx
                ON agent_runs(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_runs_thread_idx
                ON agent_runs(thread_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS agent_context_shadow_kind_idx
                ON agent_context_shadow_comparisons(kind, created_at DESC);
                """
            )
            thread_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_threads)"
                ).fetchall()
            }
            if "channel" not in thread_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_threads
                    ADD COLUMN channel TEXT NOT NULL DEFAULT 'unknown'
                    """
                )
            if "title" not in thread_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_threads
                    ADD COLUMN title TEXT NOT NULL DEFAULT '新对话'
                    """
                )
            thread_migrations = {
                "project_id": "TEXT",
                "title_ciphertext": "BLOB",
                "title_nonce": "BLOB",
                "encryption_version": "INTEGER NOT NULL DEFAULT 0",
                "key_id": "TEXT",
            }
            for name, declaration in thread_migrations.items():
                if name not in thread_columns:
                    connection.execute(
                        f"ALTER TABLE agent_threads ADD COLUMN {name} {declaration}"
                    )
            message_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_messages)"
                ).fetchall()
            }
            if "run_id" not in message_columns:
                connection.execute(
                    "ALTER TABLE agent_messages ADD COLUMN run_id TEXT"
                )
            if "metadata_json" not in message_columns:
                connection.execute(
                    """
                    ALTER TABLE agent_messages
                    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'
                    """
                )
            message_migrations = {
                "payload_ciphertext": "BLOB",
                "payload_nonce": "BLOB",
                "encryption_version": "INTEGER NOT NULL DEFAULT 0",
                "key_id": "TEXT",
            }
            for name, declaration in message_migrations.items():
                if name not in message_columns:
                    connection.execute(
                        f"ALTER TABLE agent_messages ADD COLUMN {name} {declaration}"
                    )
            run_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_runs)"
                ).fetchall()
            }
            run_migrations = {
                "project_id": "TEXT",
                "payload_ciphertext": "BLOB",
                "payload_nonce": "BLOB",
                "encryption_version": "INTEGER NOT NULL DEFAULT 0",
                "key_id": "TEXT",
                "checkpoint_thread_id": "TEXT",
            }
            for name, declaration in run_migrations.items():
                if name not in run_columns:
                    connection.execute(
                        f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}"
                    )
            summary_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_session_summaries)"
                ).fetchall()
            }
            summary_migrations = {
                "completed_actions_json": "TEXT NOT NULL DEFAULT '[]'",
                "active_assumptions_json": "TEXT NOT NULL DEFAULT '[]'",
                "artifact_refs_json": "TEXT NOT NULL DEFAULT '[]'",
                "blockers_json": "TEXT NOT NULL DEFAULT '[]'",
                "next_goal": "TEXT",
                "payload_ciphertext": "BLOB",
                "payload_nonce": "BLOB",
                "encryption_version": "INTEGER NOT NULL DEFAULT 0",
                "key_id": "TEXT",
                "summary_provider": (
                    "TEXT NOT NULL DEFAULT 'legacy:extractive-v1'"
                ),
                "summary_schema_version": (
                    "TEXT NOT NULL DEFAULT 'session-summary-v2'"
                ),
                "fallback_reason": "TEXT",
                "covered_from_message_id": "INTEGER",
                "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, declaration in summary_migrations.items():
                if name not in summary_columns:
                    connection.execute(
                        f"ALTER TABLE agent_session_summaries ADD COLUMN {name} {declaration}"
                    )
            # Derive legacy titles while message content is still plaintext.
            connection.execute(
                """
                UPDATE agent_threads
                SET title = COALESCE(
                    (
                        SELECT substr(
                            replace(replace(content, char(10), ' '), char(13), ' '),
                            1,
                            24
                        )
                        FROM agent_messages
                        WHERE agent_messages.thread_id = agent_threads.thread_id
                          AND agent_messages.owner_id = agent_threads.owner_id
                          AND role = 'user'
                        ORDER BY id ASC
                        LIMIT 1
                    ),
                    '新对话'
                )
                WHERE title = '新对话'
                """
            )
            memory_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(agent_memories)"
                ).fetchall()
            }
            memory_migrations = {
                "memory_type": "TEXT NOT NULL DEFAULT 'fact'",
                "scope": "TEXT NOT NULL DEFAULT 'user'",
                "scope_id": "TEXT",
                "topic_key": "TEXT",
                "source_message_id": "INTEGER",
                "source_run_id": "TEXT",
                "confidence": "REAL NOT NULL DEFAULT 1.0",
                "importance": "REAL NOT NULL DEFAULT 0.65",
                "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
                "retrieval_policy": "TEXT NOT NULL DEFAULT 'always'",
                "valid_from": "REAL NOT NULL DEFAULT 0",
                "valid_to": "REAL",
                "status": "TEXT NOT NULL DEFAULT 'active'",
                "supersedes_id": "TEXT",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "last_accessed_at": "REAL",
                "access_count": "INTEGER NOT NULL DEFAULT 0",
                "utility_score": "REAL NOT NULL DEFAULT 0.5",
                "content_ciphertext": "BLOB",
                "content_nonce": "BLOB",
                "encryption_version": "INTEGER NOT NULL DEFAULT 0",
                "key_id": "TEXT",
                "metadata_ciphertext": "BLOB",
                "metadata_nonce": "BLOB",
                "claim_key": "TEXT",
                "claim_polarity": "TEXT NOT NULL DEFAULT 'affirmed'",
            }
            for name, declaration in memory_migrations.items():
                if name not in memory_columns:
                    connection.execute(
                        f"ALTER TABLE agent_memories ADD COLUMN {name} {declaration}"
                    )
            if "retrieval_policy" not in memory_columns:
                connection.execute(
                    """
                    UPDATE agent_memories
                    SET retrieval_policy = 'explicit_only'
                    WHERE sensitivity IN ('private', 'sensitive')
                    """
                )
            connection.execute(
                """
                UPDATE agent_memories
                SET valid_from = created_at
                WHERE valid_from = 0
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS agent_memories_claim_idx
                ON agent_memories(
                    owner_id, memory_type, scope, scope_id,
                    claim_key, status, valid_from DESC
                )
                """
            )
            if self._cipher is not None:
                thread_rows = connection.execute(
                    """
                    SELECT thread_id, owner_id, title
                    FROM agent_threads
                    WHERE encryption_version = 0
                       OR title_ciphertext IS NULL
                       OR title_nonce IS NULL
                    """
                ).fetchall()
                for thread_id, owner_id, title in thread_rows:
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "thread",
                        str(owner_id),
                        str(thread_id),
                        {"title": str(title or "新对话")},
                    )
                    connection.execute(
                        """
                        UPDATE agent_threads
                        SET title = '', title_ciphertext = ?, title_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE thread_id = ? AND owner_id = ?
                        """,
                        (
                            ciphertext,
                            nonce,
                            version,
                            key_id,
                            str(thread_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1

                message_rows = connection.execute(
                    """
                    SELECT id, owner_id, content, metadata_json
                    FROM agent_messages
                    WHERE encryption_version = 0
                       OR payload_ciphertext IS NULL
                       OR payload_nonce IS NULL
                    """
                ).fetchall()
                for message_id, owner_id, content, metadata_json in message_rows:
                    try:
                        metadata = json.loads(str(metadata_json or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        metadata = {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "message",
                        str(owner_id),
                        str(message_id),
                        {"content": str(content), "metadata": metadata},
                    )
                    connection.execute(
                        """
                        UPDATE agent_messages
                        SET content = '', metadata_json = '{}',
                            payload_ciphertext = ?, payload_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE id = ? AND owner_id = ?
                        """,
                        (
                            ciphertext,
                            nonce,
                            version,
                            key_id,
                            int(message_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1

                run_rows = connection.execute(
                    """
                    SELECT run_id, owner_id, message, approval_json, response_json
                    FROM agent_runs
                    WHERE encryption_version = 0
                       OR payload_ciphertext IS NULL
                       OR payload_nonce IS NULL
                    """
                ).fetchall()
                for (
                    run_id,
                    owner_id,
                    message,
                    approval_json,
                    response_json,
                ) in run_rows:
                    approval = _decode_json_object(approval_json)
                    response = _decode_json_object(response_json)
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "run",
                        str(owner_id),
                        str(run_id),
                        {
                            "message": str(message),
                            "approval": approval,
                            "response": response,
                        },
                    )
                    connection.execute(
                        """
                        UPDATE agent_runs
                        SET message = '', approval_json = NULL,
                            response_json = NULL, payload_ciphertext = ?,
                            payload_nonce = ?, encryption_version = ?, key_id = ?
                        WHERE run_id = ? AND owner_id = ?
                        """,
                        (
                            ciphertext,
                            nonce,
                            version,
                            key_id,
                            str(run_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1

                summary_rows = connection.execute(
                    """
                    SELECT owner_id, thread_id, summary, open_loops_json,
                           decisions_json, completed_actions_json,
                           active_assumptions_json, artifact_refs_json,
                           blockers_json, next_goal
                    FROM agent_session_summaries
                    WHERE encryption_version = 0
                       OR payload_ciphertext IS NULL
                       OR payload_nonce IS NULL
                    """
                ).fetchall()
                for row in summary_rows:
                    owner_id = str(row[0])
                    thread_id = str(row[1])
                    ciphertext, nonce, version, key_id = self._encrypt_payload(
                        "summary",
                        owner_id,
                        thread_id,
                        {
                            "summary": str(row[2]),
                            "open_loops": _decode_string_list(row[3]),
                            "decisions": _decode_string_list(row[4]),
                            "completed_actions": _decode_string_list(row[5]),
                            "active_assumptions": _decode_string_list(row[6]),
                            "artifact_refs": _decode_string_list(row[7]),
                            "blockers": _decode_string_list(row[8]),
                            "next_goal": str(row[9]) if row[9] else None,
                        },
                    )
                    connection.execute(
                        """
                        UPDATE agent_session_summaries
                        SET summary = '', open_loops_json = '[]',
                            decisions_json = '[]', completed_actions_json = '[]',
                            active_assumptions_json = '[]', artifact_refs_json = '[]',
                            blockers_json = '[]', next_goal = NULL,
                            payload_ciphertext = ?, payload_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE owner_id = ? AND thread_id = ?
                        """,
                        (
                            ciphertext,
                            nonce,
                            version,
                            key_id,
                            owner_id,
                            thread_id,
                        ),
                    )
                    encrypted_plaintext_rows += 1
            legacy_rows = connection.execute(
                """
                SELECT id, content, memory_type, topic_key
                FROM agent_memories
                WHERE encryption_version = 0
                """
            ).fetchall()
            for memory_id, content, memory_type, topic_key in legacy_rows:
                effective_type = str(memory_type)
                if effective_type == "fact":
                    effective_type = _infer_memory_type(str(content))
                effective_topic = _memory_topic_key(
                    str(content),
                    effective_type,
                )
                if (
                    effective_type != str(memory_type)
                    or effective_topic != (
                        str(topic_key) if topic_key is not None else None
                    )
                ):
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET memory_type = ?, topic_key = ?
                        WHERE id = ?
                        """,
                        (effective_type, effective_topic, str(memory_id)),
                    )
            if self._cipher is not None:
                plaintext_rows = connection.execute(
                    """
                    SELECT id, owner_id, content, normalized_content, metadata_json
                    FROM agent_memories
                    WHERE encryption_version = 0
                       OR content_ciphertext IS NULL
                       OR content_nonce IS NULL
                    """
                ).fetchall()
                for (
                    memory_id,
                    owner_id,
                    content,
                    normalized_content,
                    metadata_json,
                ) in plaintext_rows:
                    plaintext = str(content)
                    if not plaintext:
                        raise MemoryEncryptionError(
                            "发现缺少密文且正文为空的长期记忆，已停止迁移。"
                        )
                    ciphertext, nonce = self._cipher.encrypt(
                        plaintext,
                        aad=memory_aad(str(owner_id), str(memory_id)),
                    )
                    normalized_value = str(normalized_content) or re.sub(
                        r"\s+", " ", plaintext
                    ).casefold()
                    metadata_ciphertext, metadata_nonce = self._cipher.encrypt(
                        str(metadata_json or "{}"),
                        aad=memory_aad(str(owner_id), str(memory_id), "metadata"),
                    )
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET content = '', normalized_content = ?,
                            content_ciphertext = ?, content_nonce = ?,
                            encryption_version = ?, key_id = ?,
                            metadata_json = '{}', metadata_ciphertext = ?,
                            metadata_nonce = ?
                        WHERE id = ? AND owner_id = ?
                        """,
                        (
                            self._cipher.blind_index(normalized_value),
                            ciphertext,
                            nonce,
                            self._cipher.version,
                            self._cipher.key_id,
                            metadata_ciphertext,
                            metadata_nonce,
                            str(memory_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1
                metadata_rows = connection.execute(
                    """
                    SELECT id, owner_id, metadata_json
                    FROM agent_memories
                    WHERE encryption_version > 0
                      AND (metadata_ciphertext IS NULL OR metadata_nonce IS NULL)
                    """
                ).fetchall()
                for memory_id, owner_id, metadata_json in metadata_rows:
                    metadata_ciphertext, metadata_nonce = self._cipher.encrypt(
                        str(metadata_json or "{}"),
                        aad=memory_aad(str(owner_id), str(memory_id), "metadata"),
                    )
                    connection.execute(
                        """
                        UPDATE agent_memories
                        SET metadata_json = '{}', metadata_ciphertext = ?,
                            metadata_nonce = ?, key_id = ?
                        WHERE id = ? AND owner_id = ?
                        """,
                        (
                            metadata_ciphertext,
                            metadata_nonce,
                            self._cipher.key_id,
                            str(memory_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1
                plaintext_embedding_rows = connection.execute(
                    """
                    SELECT memory_id, owner_id, model_id, vector_ciphertext
                    FROM agent_memory_embeddings
                    WHERE encryption_version = 0
                    """
                ).fetchall()
                for (
                    memory_id,
                    owner_id,
                    model_id,
                    raw_vector,
                ) in plaintext_embedding_rows:
                    ciphertext, nonce = self._cipher.encrypt_bytes(
                        bytes(raw_vector),
                        aad=record_aad(
                            "embedding",
                            str(owner_id),
                            str(memory_id),
                            "vector",
                        ),
                    )
                    memory_row = connection.execute(
                        f"""
                        SELECT {_MEMORY_SELECT_COLUMNS}
                        FROM agent_memories
                        WHERE id = ? AND owner_id = ?
                        """,
                        (str(memory_id), str(owner_id)),
                    ).fetchone()
                    fingerprint = (
                        self._embedding_fingerprint(
                            self._memory_from_row(memory_row).content,
                            model_id=str(model_id),
                        )
                        if memory_row is not None
                        else ""
                    )
                    connection.execute(
                        """
                        UPDATE agent_memory_embeddings
                        SET content_fingerprint = ?, vector_ciphertext = ?, vector_nonce = ?,
                            encryption_version = ?, key_id = ?
                        WHERE memory_id = ? AND owner_id = ?
                        """,
                        (
                            fingerprint,
                            ciphertext,
                            nonce,
                            self._cipher.version,
                            self._cipher.key_id,
                            str(memory_id),
                            str(owner_id),
                        ),
                    )
                    encrypted_plaintext_rows += 1
                probe = connection.execute(
                    """
                    SELECT id, owner_id, content_ciphertext, content_nonce, key_id
                    FROM agent_memories
                    WHERE encryption_version > 0
                    LIMIT 1
                    """
                ).fetchone()
                if probe is not None:
                    self._cipher_for(probe[4]).decrypt(
                        bytes(probe[2]),
                        bytes(probe[3]),
                        aad=memory_aad(str(probe[1]), str(probe[0])),
                    )
                embedding_probe = connection.execute(
                    """
                    SELECT memory_id, owner_id, vector_ciphertext,
                           vector_nonce, key_id
                    FROM agent_memory_embeddings
                    WHERE encryption_version > 0 LIMIT 1
                    """
                ).fetchone()
                if embedding_probe is not None:
                    self._cipher_for(embedding_probe[4]).decrypt_bytes(
                        bytes(embedding_probe[2]),
                        bytes(embedding_probe[3]),
                        aad=record_aad(
                            "embedding",
                            str(embedding_probe[1]),
                            str(embedding_probe[0]),
                            "vector",
                        ),
                    )
                encrypted_probes = (
                    (
                        "thread",
                        connection.execute(
                            """
                            SELECT thread_id, owner_id, title_ciphertext,
                                   title_nonce, key_id
                            FROM agent_threads WHERE encryption_version > 0 LIMIT 1
                            """
                        ).fetchone(),
                    ),
                    (
                        "message",
                        connection.execute(
                            """
                            SELECT id, owner_id, payload_ciphertext,
                                   payload_nonce, key_id
                            FROM agent_messages WHERE encryption_version > 0 LIMIT 1
                            """
                        ).fetchone(),
                    ),
                    (
                        "run",
                        connection.execute(
                            """
                            SELECT run_id, owner_id, payload_ciphertext,
                                   payload_nonce, key_id
                            FROM agent_runs WHERE encryption_version > 0 LIMIT 1
                            """
                        ).fetchone(),
                    ),
                    (
                        "summary",
                        connection.execute(
                            """
                            SELECT thread_id, owner_id, payload_ciphertext,
                                   payload_nonce, key_id
                            FROM agent_session_summaries
                            WHERE encryption_version > 0 LIMIT 1
                            """
                        ).fetchone(),
                    ),
                )
                for record_type, encrypted_probe in encrypted_probes:
                    if encrypted_probe is None:
                        continue
                    self._cipher_for(encrypted_probe[4]).decrypt(
                        bytes(encrypted_probe[2]),
                        bytes(encrypted_probe[3]),
                        aad=record_aad(
                            record_type,
                            str(encrypted_probe[1]),
                            str(encrypted_probe[0]),
                        ),
                    )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS agent_memories_lookup_idx
                ON agent_memories(
                    owner_id, status, memory_type, scope, scope_id, updated_at DESC
                );

                CREATE INDEX IF NOT EXISTS agent_memories_topic_idx
                ON agent_memories(owner_id, topic_key, status);

                CREATE UNIQUE INDEX IF NOT EXISTS agent_memories_source_run_idx
                ON agent_memories(owner_id, source_run_id)
                WHERE source_run_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS agent_memory_usage_run_idx
                ON agent_memory_usage(run_id, outcome);

                CREATE INDEX IF NOT EXISTS agent_memory_events_item_idx
                ON agent_memory_events(owner_id, memory_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS agent_memory_evidence_item_idx
                ON agent_memory_evidence(owner_id, memory_id, observed_at DESC);
                """
            )
            if self._cipher is not None:
                connection.execute("DROP TABLE IF EXISTS agent_memory_fts")
                self._fts_available = False
            else:
                encrypted_row = connection.execute(
                    """
                    SELECT 1 FROM (
                        SELECT encryption_version FROM agent_memories
                        UNION ALL
                        SELECT encryption_version FROM agent_threads
                        UNION ALL
                        SELECT encryption_version FROM agent_messages
                        UNION ALL
                        SELECT encryption_version FROM agent_runs
                        UNION ALL
                        SELECT encryption_version FROM agent_session_summaries
                        UNION ALL
                        SELECT encryption_version FROM agent_memory_embeddings
                    )
                    WHERE encryption_version > 0
                    LIMIT 1
                    """
                ).fetchone()
                if encrypted_row is not None:
                    raise MemoryEncryptionError(
                        "数据库包含已加密上下文记忆，不能关闭记忆加密。"
                    )
                try:
                    connection.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts
                        USING fts5(
                            memory_id UNINDEXED,
                            owner_id UNINDEXED,
                            content,
                            topic_key,
                            tokenize='unicode61 remove_diacritics 2'
                        )
                        """
                    )
                    self._fts_available = True
                    connection.execute("DELETE FROM agent_memory_fts")
                    connection.execute(
                        """
                        INSERT INTO agent_memory_fts (
                            memory_id, owner_id, content, topic_key
                        )
                        SELECT id, owner_id, content, COALESCE(topic_key, '')
                        FROM agent_memories
                        WHERE status = 'active'
                        """
                    )
                except sqlite3.OperationalError:
                    self._fts_available = False
            connection.execute(
                """
                UPDATE agent_threads
                SET channel = CASE
                    WHEN thread_id LIKE 'web:%' THEN 'web'
                    WHEN thread_id LIKE 'feishu:%' THEN 'feishu'
                    ELSE channel
                END
                WHERE channel = 'unknown'
                """
            )
            connection.execute(
                """
                UPDATE agent_threads
                SET title = COALESCE(
                    (
                        SELECT substr(
                            replace(replace(content, char(10), ' '), char(13), ' '),
                            1,
                            24
                        )
                        FROM agent_messages
                        WHERE agent_messages.thread_id = agent_threads.thread_id
                          AND agent_messages.owner_id = agent_threads.owner_id
                          AND role = 'user'
                        ORDER BY id ASC
                        LIMIT 1
                    ),
                    '新对话'
                )
                WHERE title = '新对话'
                """
            )
        os.chmod(self.path, 0o600)
        if encrypted_plaintext_rows:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            factory=_ClosingConnection,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    def _run_from_row(
        self,
        row: sqlite3.Row | tuple[Any, ...],
    ) -> StoredAgentRun:
        payload = self._decrypt_payload(
            "run",
            str(row[1]),
            str(row[0]),
            row[10],
            row[11],
            row[12],
            row[13],
        )
        return StoredAgentRun(
            run_id=str(row[0]),
            owner_id=str(row[1]),
            thread_id=str(row[2]),
            channel=str(row[3]),
            project_id=(
                str(row[15]) if len(row) > 15 and row[15] is not None else None
            ),
            message=(
                str(payload.get("message", ""))
                if payload is not None
                else str(row[4])
            ),
            status=str(row[5]),
            approval=(
                payload.get("approval")
                if payload is not None
                and isinstance(payload.get("approval"), dict)
                else _decode_json_object(row[6])
                if payload is None
                else None
            ),
            response=(
                payload.get("response")
                if payload is not None
                and isinstance(payload.get("response"), dict)
                else _decode_json_object(row[7])
                if payload is None
                else None
            ),
            created_at=float(row[8]),
            updated_at=float(row[9]),
            checkpoint_thread_id=(
                str(row[14]) if len(row) > 14 and row[14] is not None else None
            ),
        )


_MEMORY_TYPES = {
    "profile",
    "preference",
    "fact",
    "task_state",
    "episode",
    "procedure",
    "asset_relation",
}
_MEMORY_SCOPES = {"user", "channel", "thread", "project"}
_MEMORY_STATUSES = {"candidate", "active", "superseded", "archived"}
_MEMORY_SENSITIVITIES = {"normal", "private", "sensitive"}
_MEMORY_RETRIEVAL_POLICIES = {"always", "explicit_only", "never"}
_MEMORY_SELECT_COLUMNS = """
    id, owner_id, memory_type, scope, scope_id, content, topic_key,
    source, source_message_id, source_run_id, confidence, importance,
    sensitivity, retrieval_policy, valid_from, valid_to, status, supersedes_id,
    created_at, updated_at, last_accessed_at, access_count,
    utility_score, metadata_json, content_ciphertext, content_nonce,
    encryption_version, key_id, metadata_ciphertext, metadata_nonce
"""


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.5
    return min(max(score, 0.0), 1.0)


def _memory_evidence_source_type(
    *,
    source: str,
    source_message_id: int | None,
    source_run_id: str | None,
) -> str:
    if source_message_id is not None:
        return "message"
    if source_run_id is not None or source.startswith("run:"):
        return "run"
    if source in {"web:memory-center", "explicit_memory"}:
        return "explicit"
    return "derived"


def _infer_memory_type(content: str) -> MemoryType:
    if re.search(r"偏好|喜欢|希望|优先|默认|习惯", content):
        return "preference"
    if re.search(r"待办|下一步|截止|任务状态|进行中|已完成", content):
        return "task_state"
    if re.search(r"以后每次|操作流程|先.+再|遇到.+就", content):
        return "procedure"
    if _profile_topic_key(content) is not None or re.search(
        r"(?:^|[，。；;\s])我(?:是|叫|住在|居住在)",
        content,
    ):
        return "profile"
    return "fact"


def _infer_sensitivity(content: str) -> MemorySensitivity:
    if re.search(r"身份证|银行卡|病历|家庭住址|手机号|邮箱", content):
        return "sensitive"
    if re.search(r"私人|隐私|住址|生日|家庭|健康", content):
        return "private"
    return "normal"


def _split_atomic_memories(content: str) -> list[str]:
    original = re.sub(r"\s+", " ", content).strip()[:2000]
    if not original:
        return [original]
    raw_parts = [
        re.sub(r"^(?:并且|而且|另外|以及|还有)\s*", "", part).strip()
        for part in re.split(r"[，,；;。\n]+", original)
        if part.strip()
    ]
    if len(raw_parts) < 2:
        return [original]

    atoms: list[str] = []
    for part in raw_parts:
        if re.match(r"^(?:我|我的)", part):
            atom = part
        elif re.match(r"^(?:叫|名叫)", part):
            atom = f"我{part}"
        elif re.match(r"^(?:住在|居住在|位于)", part):
            atom = f"我{part}"
        elif re.match(r"^(?:喜欢|偏好|希望|习惯|默认)", part):
            atom = f"我{part}"
        elif re.match(r"^(?:名字|姓名|年龄|生日|职业|职位|工作|公司|学校|专业)", part):
            atom = f"我的{part}"
        elif re.match(r"^(?:待办|下一步|任务状态|以后每次|遇到)", part):
            atom = part
        else:
            return [original]
        if _infer_memory_type(atom) == "fact":
            return [original]
        atoms.append(atom[:2000])
    return _dedupe_strings(atoms) if len(atoms) > 1 else [original]


def _contains_forbidden_secret(content: str) -> bool:
    if re.search(r"\bsk-[A-Za-z0-9_-]{12,}\b", content):
        return True
    if re.search(
        r"(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
        r"password|密码|密钥|验证码)\s*[:：=]\s*[^\s，。,;；]{4,}",
        content,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _memory_topic_key(content: str, memory_type: str) -> str | None:
    if memory_type != "profile":
        return None
    return _profile_topic_key(content)


def _normalize_memory_claim(
    content: str,
    memory_type: str,
    metadata: dict[str, Any],
) -> dict[str, str] | None:
    """Normalize mutable memories without using body similarity as identity."""

    explicit = metadata.get("claim") or metadata.get("_claim")
    if isinstance(explicit, dict):
        entity_key = str(explicit.get("entity_key", "")).strip()[:160]
        predicate = str(explicit.get("predicate", "")).strip()[:120]
        value = str(explicit.get("value", content)).strip()[:500]
        polarity = str(explicit.get("polarity", "affirmed")).strip().lower()
        if entity_key and predicate and polarity in {"affirmed", "negated"}:
            return {
                "schema_version": "memory-claim-v1",
                "entity_key": entity_key,
                "predicate": predicate,
                "value": value,
                "polarity": polarity,
            }

    if memory_type == "profile":
        topic = _profile_topic_key(content)
        if topic is None:
            return None
        return {
            "schema_version": "memory-claim-v1",
            "entity_key": "self",
            "predicate": topic,
            "value": content.strip()[:500],
            "polarity": (
                "negated" if _contains_claim_negation(content) else "affirmed"
            ),
        }

    if memory_type == "preference":
        return _normalize_preference_claim(content)

    if memory_type == "task_state":
        task_name = str(metadata.get("task_name", "")).strip()[:120]
        if not task_name:
            match = re.search(
                r"(?:任务(?:名)?\s*[：:为]?\s*)([^，,。；;]{1,80})",
                content,
            )
            if match is not None:
                task_name = match.group(1).strip()
            else:
                match = re.search(r"([^，,。；;]{1,50})任务", content)
                if match is not None:
                    task_name = match.group(1).strip()
        task_name = re.sub(
            r"(?:状态|进度|已经|已|为|是)\s*.*$",
            "",
            task_name,
        ).strip(" ：:，,")[:120]
        if not task_name:
            return None
        return {
            "schema_version": "memory-claim-v1",
            "entity_key": f"task:{task_name.casefold()}",
            "predicate": "task:state",
            "value": content.strip()[:500],
            "polarity": (
                "negated"
                if re.search(r"(?:取消|删除|不再执行|停止任务)", content)
                else "affirmed"
            ),
        }
    return None


def _normalize_preference_claim(content: str) -> dict[str, str] | None:
    replacement = re.search(
        r"(?:改用|改成|改为|换成|以后(?:改)?用)\s*([^，,。；;]{1,100})",
        content,
    )
    if replacement is not None:
        fragment = replacement.group(1).strip()
        polarity = "affirmed"
    else:
        match = re.search(
            r"(?:不再|不|不要|取消)?\s*(?:偏好|喜欢|希望|默认|习惯(?:使用)?|采用|使用)"
            r"\s*([^，,。；;]{1,120})",
            content,
        )
        if match is None:
            return None
        fragment = match.group(1).strip()
        polarity = "negated" if _contains_claim_negation(content) else "affirmed"

    dimensions = (
        ("主题", "theme"),
        ("颜色", "color"),
        ("风格", "style"),
        ("语言", "language"),
        ("格式", "format"),
        ("回答", "response"),
        ("输出", "output"),
        ("通知", "notification"),
        ("模型", "model"),
        ("工具", "tool"),
        ("称呼", "address"),
    )
    dimension = next(
        (canonical for marker, canonical in dimensions if marker in fragment),
        None,
    )
    if dimension is None:
        dimension = next(
            (canonical for marker, canonical in dimensions if marker in content),
            "general",
        )
    value = fragment
    for marker, canonical in dimensions:
        if canonical == dimension:
            value = value.replace(marker, "").strip() or fragment
            break
    return {
        "schema_version": "memory-claim-v1",
        "entity_key": "self",
        "predicate": f"preference:{dimension}",
        "value": value[:500],
        "polarity": polarity,
    }


def _contains_claim_negation(content: str) -> bool:
    return bool(
        re.search(
            r"(?:不再|不要|取消|并不|不喜欢|不偏好|停止使用)",
            content,
        )
    )


def _memory_claim_storage_key(
    claim: dict[str, str] | None,
    cipher: MemoryCipher | None,
) -> str | None:
    if claim is None:
        return None
    canonical = json.dumps(
        [claim["entity_key"], claim["predicate"]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return cipher.blind_index(canonical) if cipher is not None else canonical


def _profile_topic_key(content: str) -> str | None:
    normalized = re.sub(r"\s+", "", content).casefold()
    if re.search(
        r"(?:我叫|我的(?:姓名|名字)(?:是|叫|为|改为|改成))"
        r"[^，。；;]{1,60}",
        normalized,
    ):
        return "profile:name"
    location = re.search(
        r"(?:我)?(?:现在)?(?:住在|居住在|所在地是|位置是|"
        r"家庭住址是|地址是)([^，。；;]{1,60})",
        normalized,
    )
    if location:
        return "profile:location"
    if re.search(r"(?:我的)?(?:生日|出生日期)(?:是|为)", normalized):
        return "profile:birthday"
    if re.search(r"(?:我的)?年龄(?:是|为)|我(?:今年)?\d{1,3}岁", normalized):
        return "profile:age"
    if re.search(
        r"(?:我的)?(?:职业|工作|职位|公司|学校|专业)(?:是|为|在)|"
        r"我是(?:一名|一个)?[^，。；;]{1,30}(?:师|员|家|生|者)",
        normalized,
    ):
        return "profile:occupation"
    if re.search(r"联系方式|手机号|电话|邮箱|微信号", normalized):
        return "profile:contact"
    attribute = re.search(
        r"我的([^，。；;]{1,20}?)(?:是|叫|为|改为|改成)([^，。；;]{1,60})",
        normalized,
    )
    if attribute:
        return f"profile:{attribute.group(1)[:20]}"
    if re.search(r"(?:^|[，。；;])我是[^，。；;]{1,60}", normalized):
        return "profile:identity"
    return None


def _memory_terms(value: str) -> set[str]:
    lowered = value.casefold().replace("_", " ")
    terms = set(re.findall(r"[a-z0-9][a-z0-9-]{1,}", lowered))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(chunk) <= 8:
            terms.add(chunk)
        terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    synonym_groups = {
        "preference": {"偏好", "喜欢", "习惯", "希望", "优先", "默认"},
        "location": {"住在", "居住", "地址", "位置", "所在地"},
        "display": {"界面", "主题", "颜色", "外观"},
        "image": {"图片", "照片", "图像"},
        "workflow": {"流程", "步骤", "操作", "方法"},
        "failure": {"失败", "错误", "异常", "问题"},
    }
    for canonical, synonyms in synonym_groups.items():
        if any(term in lowered for term in synonyms):
            terms.add(canonical)
    stop_terms = {
        "我的",
        "我是",
        "我在",
        "什么",
        "怎么",
        "是否",
        "知道",
        "记得",
        "一下",
        "用户",
        "请问",
        "可以",
    }
    return {term for term in terms if term and term not in stop_terms}


def _rank_score_map(
    scores: dict[str, float],
    allowed_ids: set[str],
) -> list[str]:
    return [
        memory_id
        for memory_id, _ in sorted(
            (
                (memory_id, score)
                for memory_id, score in scores.items()
                if memory_id in allowed_ids
            ),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
    ]


def _memory_pair_similarity(
    left: MemoryRecord,
    right: MemoryRecord,
    semantic_vectors: dict[str, list[float]],
) -> float:
    left_vector = semantic_vectors.get(left.id)
    right_vector = semantic_vectors.get(right.id)
    if left_vector is not None and right_vector is not None:
        return max(0.0, cosine_similarity(left_vector, right_vector))
    left_terms = _memory_terms(left.content)
    right_terms = _memory_terms(right.content)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))


def _mmr_select_memories(
    scored: list[MemoryRecord],
    *,
    semantic_vectors: dict[str, list[float]],
    limit: int,
    diversity_lambda: float,
) -> list[MemoryRecord]:
    ordered = sorted(
        scored,
        key=lambda item: (item.relevance_score, item.updated_at, item.id),
        reverse=True,
    )
    unique: list[MemoryRecord] = []
    seen: set[str] = set()
    for item in ordered:
        normalized = re.sub(r"\W+", "", item.content).casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(item)

    selected: list[MemoryRecord] = []
    remaining = list(unique)
    while remaining and len(selected) < limit:
        if not selected:
            selected.append(remaining.pop(0))
            continue
        best_index = max(
            range(len(remaining)),
            key=lambda index: (
                diversity_lambda * remaining[index].relevance_score
                - (1.0 - diversity_lambda)
                * max(
                    _memory_pair_similarity(
                        remaining[index],
                        chosen,
                        semantic_vectors,
                    )
                    for chosen in selected
                ),
                remaining[index].relevance_score,
                remaining[index].updated_at,
            ),
        )
        selected.append(remaining.pop(best_index))
    return selected


def _memory_query_hints(query: str) -> set[str]:
    normalized = re.sub(r"\s+", "", query).casefold()
    if not normalized:
        return set()
    hints: set[str] = set()
    if re.fullmatch(r"(?:你好|您好|嗨|哈喽|hello|hi)[!！。,.，]?", normalized):
        hints.add("profile:bootstrap")
    if re.search(
        r"(?:记得|保存)(?:了|的)?(?:我)?(?:什么|哪些|多少)|"
        r"(?:查看|列出|展示|告诉我).*(?:记忆|已保存)|"
        r"我的(?:长期)?记忆",
        normalized,
    ):
        hints.add("memory:all")
    if re.search(
        r"我是谁|还记得我(?:吗|么|不)|认识我(?:吗|么)|"
        r"了解我(?:吗|么|多少)|个人(?:信息|资料)|关于我",
        normalized,
    ):
        hints.add("profile:broad")
    if re.search(r"名字|姓名|怎么称呼|叫什么|叫啥|称谓", normalized):
        hints.add("profile:name")
    if re.search(r"住哪|住在|居住|地址|位置|所在地|哪座城市", normalized):
        hints.add("profile:location")
    if re.search(r"年龄|多大|几岁", normalized):
        hints.add("profile:age")
    if re.search(r"生日|出生日期|哪天出生", normalized):
        hints.add("profile:birthday")
    if re.search(r"职业|工作|职位|公司|学校|专业|做什么的", normalized):
        hints.add("profile:occupation")
    if re.search(r"联系方式|手机号|电话|邮箱|微信号", normalized):
        hints.add("profile:contact")
    if re.search(r"偏好|喜欢|习惯|默认|更想要|倾向", normalized):
        hints.add("type:preference")
    if re.search(r"待办|任务状态|进度|下一步|还要做", normalized):
        hints.add("type:task_state")
    if re.search(r"操作流程|处理流程|以后怎么|通常怎么|步骤", normalized):
        hints.add("type:procedure")
    if re.search(r"(?:上次|之前|过去).*(?:任务|执行|失败|成功|做过)", normalized):
        hints.add("type:episode")
    return hints


def _memory_intent_score(item: MemoryRecord, hints: set[str]) -> float:
    if not hints:
        return 0.0
    if "memory:all" in hints:
        return 1.0

    effective_type = item.memory_type
    if effective_type == "fact":
        effective_type = _infer_memory_type(item.content)
    topic = item.topic_key or _memory_topic_key(item.content, effective_type)
    if "profile:bootstrap" in hints:
        if (
            topic in {"profile:name", "profile:identity"}
            and item.sensitivity == "normal"
        ):
            return 0.75
        return 0.0
    profile_slots = {
        hint
        for hint in hints
        if hint.startswith("profile:")
        and hint not in {"profile:broad", "profile:bootstrap"}
    }
    if profile_slots:
        if topic in profile_slots:
            return 1.0
        if topic == "profile:identity" and "profile:name" in profile_slots:
            return 0.8
        return 0.0
    if "profile:broad" in hints and effective_type == "profile":
        return 0.0 if item.sensitivity == "sensitive" else 0.9
    if f"type:{effective_type}" in hints:
        return 0.9
    return 0.0


def _retrieval_policy_allows(item: MemoryRecord, hints: set[str]) -> bool:
    if item.retrieval_policy == "never":
        return False
    if item.retrieval_policy == "always":
        return True
    if "memory:all" in hints:
        return True
    effective_type = item.memory_type
    if effective_type == "fact":
        effective_type = _infer_memory_type(item.content)
    topic = item.topic_key or _memory_topic_key(item.content, effective_type)
    if topic and topic in hints:
        return True
    return f"type:{effective_type}" in hints and effective_type != "profile"


def _bounded_memory_int(
    raw: Any,
    *,
    default: int,
    lower: int,
    upper: int,
) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(max(value, lower), upper)


def _bounded_memory_float(
    raw: Any,
    *,
    default: float,
    lower: float,
    upper: float,
) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return min(max(value, lower), upper)


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _compare_summary_states(
    baseline: dict[str, Any],
    candidate: dict[str, Any] | None,
) -> tuple[bool, dict[str, Any]]:
    if candidate is None:
        return False, {"candidate_available": False, "critical_recall": 0.0}
    critical_fields = ("open_loops", "decisions", "blockers")
    total = 0
    recalled = 0
    for field in critical_fields:
        candidate_items = [str(item) for item in candidate.get(field, [])]
        for baseline_item in baseline.get(field, []):
            total += 1
            baseline_terms = _memory_terms(str(baseline_item))
            if any(
                (
                    str(baseline_item) in item
                    or item in str(baseline_item)
                    or len(baseline_terms & _memory_terms(item))
                    / max(1, len(baseline_terms))
                    >= 0.5
                )
                for item in candidate_items
            ):
                recalled += 1
    if baseline.get("next_goal"):
        total += 1
        baseline_terms = _memory_terms(str(baseline["next_goal"]))
        candidate_goal = str(candidate.get("next_goal") or "")
        if (
            str(baseline["next_goal"]) in candidate_goal
            or candidate_goal in str(baseline["next_goal"])
            or len(baseline_terms & _memory_terms(candidate_goal))
            / max(1, len(baseline_terms))
            >= 0.5
        ):
            recalled += 1
    recall = recalled / total if total else 1.0
    return recall >= 0.95, {
        "candidate_available": True,
        "critical_items": total,
        "critical_recalled": recalled,
        "critical_recall": round(recall, 6),
    }


_SESSION_SUMMARY_FIELDS = {
    "summary",
    "open_loops",
    "decisions",
    "completed_actions",
    "active_assumptions",
    "artifact_refs",
    "blockers",
    "next_goal",
}
_SESSION_SUMMARY_PROVENANCE_FIELD = "provenance"
_SESSION_SUMMARY_LIST_FIELDS = (
    "open_loops",
    "decisions",
    "completed_actions",
    "active_assumptions",
    "artifact_refs",
    "blockers",
)


def _summary_state_from_storage(
    existing: sqlite3.Row | tuple[Any, ...] | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if payload is not None:
        source = payload
        return {
            "summary": str(source.get("summary", "")),
            **{
                field: _coerce_string_list(source.get(field))
                for field in _SESSION_SUMMARY_LIST_FIELDS
            },
            "next_goal": (
                str(source.get("next_goal")) if source.get("next_goal") else None
            ),
            "provenance": _coerce_summary_provenance(
                source.get("provenance")
            ),
        }
    if existing is None:
        return {
            "summary": "",
            **{field: [] for field in _SESSION_SUMMARY_LIST_FIELDS},
            "next_goal": None,
            "provenance": [],
        }
    return {
        "summary": str(existing[0]),
        "open_loops": _decode_string_list(existing[1]),
        "decisions": _decode_string_list(existing[2]),
        "completed_actions": _decode_string_list(existing[3]),
        "active_assumptions": _decode_string_list(existing[4]),
        "artifact_refs": _decode_string_list(existing[5]),
        "blockers": _decode_string_list(existing[6]),
        "next_goal": str(existing[7]) if existing[7] else None,
        "provenance": (
            _decode_summary_provenance(existing[17])
            if len(existing) > 17
            else []
        ),
    }


def _validate_summary_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("结构化摘要字段不完整或包含未知字段。")
    keys = frozenset(raw)
    if keys not in {
        frozenset(_SESSION_SUMMARY_FIELDS),
        frozenset(_SESSION_SUMMARY_FIELDS | {_SESSION_SUMMARY_PROVENANCE_FIELD}),
    }:
        raise ValueError("结构化摘要字段不完整或包含未知字段。")
    if not isinstance(raw["summary"], str):
        raise ValueError("结构化摘要的 summary 必须是字符串。")
    normalized: dict[str, Any] = {"summary": raw["summary"].strip()}
    for field in _SESSION_SUMMARY_LIST_FIELDS:
        value = raw[field]
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
            or len(value) > 256
        ):
            raise ValueError(f"结构化摘要的 {field} 字段无效。")
        normalized[field] = _dedupe_strings(
            [item.strip() for item in value if item.strip()]
        )[-24:]
    next_goal = raw["next_goal"]
    if next_goal is not None and not isinstance(next_goal, str):
        raise ValueError("结构化摘要的 next_goal 必须是字符串或 null。")
    normalized["next_goal"] = next_goal.strip() if next_goal else None
    normalized["provenance"] = _coerce_summary_provenance(
        raw.get("provenance")
    )
    return normalized


def _coerce_summary_provenance(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    allowed_fields = _SESSION_SUMMARY_FIELDS
    normalized: list[dict[str, Any]] = []
    for item in raw[:128]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field", "")).strip()
        text = str(item.get("text", "")).strip()
        if field not in allowed_fields or not text:
            continue
        raw_sources = item.get("sources")
        sources: list[dict[str, Any]] = []
        if isinstance(raw_sources, list):
            for source in raw_sources[:32]:
                if not isinstance(source, dict):
                    continue
                try:
                    message_id = int(source.get("message_id"))
                except (TypeError, ValueError):
                    continue
                content_sha256 = str(source.get("content_sha256", "")).strip()
                role = str(source.get("role", "")).strip()
                if message_id > 0 and len(content_sha256) == 64:
                    sources.append(
                        {
                            "message_id": message_id,
                            "role": role,
                            "content_sha256": content_sha256,
                        }
                    )
        raw_ids = item.get("source_message_ids")
        source_message_ids: list[int] = []
        if isinstance(raw_ids, list):
            for value in raw_ids[:32]:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id > 0 and message_id not in source_message_ids:
                    source_message_ids.append(message_id)
        normalized_item: dict[str, Any] = {
            "field": field,
            "text": text,
        }
        if sources:
            normalized_item["sources"] = sources
        if source_message_ids:
            normalized_item["source_message_ids"] = source_message_ids
        normalized.append(normalized_item)
    return normalized


def _summary_provider_state(previous: dict[str, Any]) -> dict[str, Any]:
    """Remove storage-only hashes while retaining source IDs for model citations."""

    compact = {
        key: value
        for key, value in previous.items()
        if key != "provenance"
    }
    compact["provenance"] = [
        {
            "field": item["field"],
            "text": item["text"],
            "source_message_ids": [
                int(source["message_id"])
                for source in item.get("sources", [])
                if isinstance(source, dict) and source.get("message_id")
            ],
        }
        for item in _coerce_summary_provenance(previous.get("provenance"))
    ]
    return compact


def _merge_summary_invariants(
    previous: dict[str, Any],
    candidate: dict[str, Any],
    compact_rows: list[tuple[int, str, str]],
    *,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    """Prevent a model refresh from silently dropping unresolved state."""

    merged = dict(candidate)
    guards = _extract_critical_state_guards(
        compact_rows,
        token_counter=token_counter,
    )
    previous_lines = str(previous.get("summary", "")).splitlines()
    candidate_lines = str(candidate.get("summary", "")).splitlines()
    merged["summary"] = "\n".join(
        _dedupe_strings([*previous_lines, *candidate_lines])
    )
    new_contents = [content for _, _, content in compact_rows]
    for field in (
        "decisions",
        "active_assumptions",
        "artifact_refs",
        "completed_actions",
    ):
        merged[field] = _dedupe_strings(
            [
                *[str(item) for item in previous.get(field, [])],
                *guards.get(field, []),
                *[str(item) for item in candidate.get(field, [])],
            ]
        )
    for field in ("open_loops", "blockers"):
        still_active = [
            str(item)
            for item in previous.get(field, [])
            if not _state_item_explicitly_resolved(str(item), new_contents)
        ]
        merged[field] = _dedupe_strings(
            [
                *still_active,
                *guards.get(field, []),
                *[str(item) for item in candidate.get(field, [])],
            ]
        )
    merged["next_goal"] = (
        candidate.get("next_goal")
        or guards.get("next_goal")
        or previous.get("next_goal")
    )
    merged["provenance"] = [
        *_coerce_summary_provenance(previous.get("provenance")),
        *_coerce_summary_provenance(candidate.get("provenance")),
    ]
    return merged


def _extract_critical_state_guards(
    compact_rows: list[tuple[int, str, str]],
    *,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    """Extract only explicit critical state as a guard against model omission."""

    guarded: dict[str, Any] = {
        "decisions": [],
        "open_loops": [],
        "blockers": [],
        "next_goal": None,
    }
    for _, _, raw_content in compact_rows:
        content = re.sub(r"\s+", " ", raw_content).strip()
        if not content or re.search(r"保持不变|不新增(?:事实|决定|待办)|无变化", content):
            continue
        clipped = _clip_memory_text(content, 96, token_counter=token_counter)
        if re.search(r"决定|确认|选择|采用|改为|同意", content):
            guarded["decisions"].append(clipped)
        if re.search(r"待办|下一步|还需|尚未|未完成|稍后|需要继续", content):
            guarded["open_loops"].append(clipped)
        if re.search(r"阻塞|受阻|失败|错误|无法|等待|缺少", content):
            guarded["blockers"].append(clipped)
        if re.search(r"下一步|接下来|之后需要|继续(?:处理|完成|实现|修复)", content):
            guarded["next_goal"] = clipped
    for field in ("decisions", "open_loops", "blockers"):
        guarded[field] = _dedupe_strings(guarded[field])
    return guarded


def _state_item_explicitly_resolved(
    item: str,
    new_contents: list[str],
) -> bool:
    item_terms = _memory_terms(item)
    for content in new_contents:
        if not re.search(
            r"已完成|已经完成|已解决|不再需要|已取消|取消该|已关闭",
            content,
        ):
            continue
        overlap = item_terms & _memory_terms(content)
        if len(overlap) >= 3 and len(overlap) / max(1, len(item_terms)) >= 0.6:
            return True
    return False


def _decode_summary_provenance(raw: Any) -> list[dict[str, Any]]:
    try:
        decoded = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return _coerce_summary_provenance(decoded)


def _summary_event_hash(message_id: int, role: str, content: str) -> str:
    return sha256(
        f"{message_id}\0{role}\0{content}".encode("utf-8")
    ).hexdigest()


def _attach_summary_provenance(
    state: dict[str, Any],
    *,
    previous: dict[str, Any],
    compact_rows: list[tuple[int, str, str]],
) -> list[dict[str, Any]]:
    current_sources = {
        message_id: {
            "message_id": message_id,
            "role": role,
            "content_sha256": _summary_event_hash(message_id, role, content),
        }
        for message_id, role, content in compact_rows
    }
    current_content = {
        message_id: content for message_id, _, content in compact_rows
    }
    previous_items = _coerce_summary_provenance(previous.get("provenance"))
    previous_lookup = {
        (str(item.get("field")), str(item.get("text"))): list(
            item.get("sources", [])
        )
        for item in previous_items
        if item.get("sources")
    }
    available_sources = dict(current_sources)
    for item in previous_items:
        for source in item.get("sources", []):
            if isinstance(source, dict) and source.get("message_id"):
                available_sources[int(source["message_id"])] = source

    requested = _coerce_summary_provenance(state.get("provenance"))
    requested_lookup = {
        (str(item.get("field")), str(item.get("text"))): item
        for item in requested
    }

    targets: list[tuple[str, str]] = []
    if str(state.get("summary", "")).strip():
        targets.append(("summary", str(state["summary"]).strip()))
    for field in _SESSION_SUMMARY_LIST_FIELDS:
        targets.extend((field, str(text)) for text in state.get(field, []))
    if state.get("next_goal"):
        targets.append(("next_goal", str(state["next_goal"])))

    result: list[dict[str, Any]] = []
    for field, text in targets:
        source_map: dict[int, dict[str, Any]] = {}
        requested_item = requested_lookup.get((field, text))
        if requested_item is not None:
            for message_id in requested_item.get("source_message_ids", []):
                source = available_sources.get(int(message_id))
                if source is not None:
                    source_map[int(message_id)] = source
            for source in requested_item.get("sources", []):
                if not isinstance(source, dict) or not source.get("message_id"):
                    continue
                message_id = int(source["message_id"])
                verified = available_sources.get(message_id)
                if verified is not None:
                    source_map[message_id] = verified
        if not source_map:
            for source in previous_lookup.get((field, text), []):
                if isinstance(source, dict) and source.get("message_id"):
                    source_map[int(source["message_id"])] = source
        if not source_map and current_content:
            target_terms = _memory_terms(text)
            scored = []
            for message_id, content in current_content.items():
                content_terms = _memory_terms(content)
                overlap = len(target_terms & content_terms)
                contains = text in content or content in text
                scored.append((1 if contains else 0, overlap, message_id))
            best_contains, best_overlap, _ = max(scored)
            selected_ids = [
                message_id
                for contains, overlap, message_id in scored
                if (contains, overlap) == (best_contains, best_overlap)
                and (contains or overlap > 0)
            ]
            if not selected_ids:
                selected_ids = list(current_sources)[-8:]
            for message_id in selected_ids[:8]:
                source_map[message_id] = current_sources[message_id]
        result.append(
            {
                "field": field,
                "text": text,
                "sources": [source_map[key] for key in sorted(source_map)],
            }
        )
    return result


def _extractive_summary_fallback(
    previous: dict[str, Any],
    compact_rows: list[tuple[int, str, str]],
    *,
    token_budget: int,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    """Deterministic emergency fallback; never the configured primary path."""

    previous_summary = str(previous["summary"]).strip()
    new_lines: list[str] = []
    open_loops = list(previous["open_loops"])
    decisions = list(previous["decisions"])
    completed_actions = list(previous["completed_actions"])
    active_assumptions = list(previous["active_assumptions"])
    artifact_refs = list(previous["artifact_refs"])
    blockers = list(previous["blockers"])
    next_goal = previous["next_goal"]
    for _, role, raw_content in compact_rows:
        content = re.sub(r"\s+", " ", raw_content).strip()
        if not content:
            continue
        clipped = _clip_memory_text(
            content,
            96,
            token_counter=token_counter,
        )
        label = "用户" if role == "user" else "助手"
        new_lines.append(f"[{label}] {clipped}")
        if re.search(r"决定|确认|选择|采用|改为|同意|已完成", content):
            decisions.append(clipped)
        if re.search(r"待办|下一步|还需|尚未|未完成|稍后|需要继续", content):
            open_loops.append(clipped)
        if re.search(r"已完成|完成了|已经完成|成功|已保存|已创建|已更新|已发送", content):
            completed_actions.append(clipped)
            open_loops = _remove_resolved_state(open_loops, content)
            blockers = _remove_resolved_state(blockers, content)
        if re.search(r"假设|前提|默认认为|暂定|先按", content):
            active_assumptions.append(clipped)
        if re.search(r"阻塞|受阻|失败|错误|无法|等待|缺少|尚未", content):
            blockers.append(clipped)
        artifact_refs.extend(_extract_artifact_refs(content))
        if re.search(r"下一步|接下来|之后需要|继续(?:处理|完成|实现|修复)", content):
            next_goal = clipped

    narrative_budget = max(16, min(2_048, token_budget // 3))
    protected_previous = _clip_memory_text(
        previous_summary,
        narrative_budget,
        token_counter=token_counter,
    )
    remaining_narrative = max(
        0,
        narrative_budget - token_counter.count(protected_previous) - 1,
    )
    recent_narrative, _ = clip_text_to_tokens(
        "\n".join(new_lines),
        remaining_narrative,
        token_counter=token_counter,
        preserve_tail=True,
    )
    return {
        "summary": "\n".join(
            item for item in (protected_previous, recent_narrative) if item
        ),
        "open_loops": _dedupe_strings(open_loops)[-8:],
        "decisions": _dedupe_strings(decisions)[-8:],
        "completed_actions": _dedupe_strings(completed_actions)[-8:],
        "active_assumptions": _dedupe_strings(active_assumptions)[-8:],
        "artifact_refs": _dedupe_strings(artifact_refs)[-12:],
        "blockers": _dedupe_strings(blockers)[-8:],
        "next_goal": next_goal,
        "provenance": list(previous.get("provenance", [])),
    }


def _fit_summary_state_to_budget(
    raw: dict[str, Any],
    token_budget: int,
    *,
    token_counter: TokenCounter,
) -> dict[str, Any]:
    state = _validate_summary_state(raw)
    requested_provenance = list(state.pop("provenance", []))
    state["summary"] = _clip_memory_text(
        state["summary"],
        max(16, min(2_048, token_budget // 3)),
        token_counter=token_counter,
    )
    for field in _SESSION_SUMMARY_LIST_FIELDS:
        limit = 12 if field == "artifact_refs" else 8
        state[field] = [
            _clip_memory_text(item, 72, token_counter=token_counter)
            for item in _dedupe_strings(state[field])[-limit:]
        ]
    if state["next_goal"]:
        state["next_goal"] = _clip_memory_text(
            str(state["next_goal"]),
            96,
            token_counter=token_counter,
        )

    def state_cost() -> int:
        return token_counter.count(
            json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        )

    drop_order = (
        "completed_actions",
        "artifact_refs",
        "active_assumptions",
        "decisions",
        "blockers",
        "open_loops",
    )
    while state_cost() > token_budget:
        target = next((field for field in drop_order if state[field]), None)
        if target is None:
            break
        state[target].pop(0)
    if state_cost() > token_budget and state["summary"]:
        without_summary = dict(state)
        without_summary["summary"] = ""
        remaining = max(1, token_budget - token_counter.count(
            json.dumps(without_summary, ensure_ascii=False, separators=(",", ":"))
        ))
        state["summary"] = _clip_memory_text(
            state["summary"],
            remaining,
            token_counter=token_counter,
        )
    if state_cost() > token_budget:
        state["next_goal"] = None
    state["provenance"] = requested_provenance
    return state


def _summary_source_fingerprint(
    rows: list[tuple[int, str, str]],
) -> str:
    serialized = json.dumps(
        rows,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _clip_memory_text(
    value: str,
    token_budget: int,
    *,
    token_counter: TokenCounter,
) -> str:
    clipped, _ = clip_text_to_tokens(
        value,
        token_budget,
        token_counter=token_counter,
    )
    return clipped


def _fit_summary_lines(
    lines: list[str],
    token_budget: int,
    *,
    token_counter: TokenCounter,
) -> list[str]:
    selected: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = token_counter.count(line) + 1
        if used + cost <= token_budget:
            selected.append(line)
            used += cost
            continue
        if not selected:
            selected.append(
                _clip_memory_text(
                    line,
                    max(1, token_budget - 1),
                    token_counter=token_counter,
                )
            )
        break
    selected.reverse()
    dropped = len(lines) - len(selected)
    marker = f"[摘要] 更早的 {dropped} 条会话要点已被滚动压缩。"
    if (
        dropped > 0
        and token_counter.count("\n".join([marker, *selected])) <= token_budget
    ):
        selected.insert(0, marker)
    return selected


def _extract_artifact_refs(content: str) -> list[str]:
    patterns = (
        r"https?://[^\s，。；;]+",
        r"\b(?:run|asset|source[_-]?image|file)[_-]?id\s*[:：=]\s*[A-Za-z0-9._:-]+",
        r"\b(?:run|asset|image|file)-[A-Za-z0-9._:-]{3,}\b",
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(match.group(0)[:240] for match in re.finditer(pattern, content, re.I))
    return _dedupe_strings(found)


def _remove_resolved_state(items: list[str], completion: str) -> list[str]:
    completion_terms = _memory_terms(completion)
    if not completion_terms:
        return items
    retained: list[str] = []
    for item in items:
        item_terms = _memory_terms(item)
        overlap = len(item_terms & completion_terms) / max(1, len(item_terms))
        if overlap < 0.45:
            retained.append(item)
    return retained


def _decode_string_list(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _decode_json_object(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _coerce_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item).strip()]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip().casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result
