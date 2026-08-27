from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
from pathlib import Path

import keyring
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from keyring.errors import KeyringError


class MemoryEncryptionError(RuntimeError):
    """Raised when persisted memory cannot be encrypted or decrypted safely."""


class MemoryCipher:
    version = 1

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("记忆加密密钥必须是 32 字节。")
        self._key = bytes(key)
        self._aes = AESGCM(self._key)
        self.key_id = hashlib.sha256(self._key).hexdigest()[:16]

    def encrypt(self, plaintext: str, *, aad: bytes) -> tuple[bytes, bytes]:
        return self.encrypt_bytes(plaintext.encode("utf-8"), aad=aad)

    def encrypt_bytes(self, plaintext: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        ciphertext = self._aes.encrypt(nonce, plaintext, aad)
        return ciphertext, nonce

    def decrypt(self, ciphertext: bytes, nonce: bytes, *, aad: bytes) -> str:
        try:
            return self.decrypt_bytes(ciphertext, nonce, aad=aad).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryEncryptionError("上下文记忆解密后不是有效 UTF-8。") from exc

    def decrypt_bytes(
        self,
        ciphertext: bytes,
        nonce: bytes,
        *,
        aad: bytes,
    ) -> bytes:
        try:
            return self._aes.decrypt(nonce, ciphertext, aad)
        except (InvalidTag, ValueError) as exc:
            raise MemoryEncryptionError(
                "上下文记忆无法解密，可能使用了错误密钥或数据已损坏。"
            ) from exc

    def blind_index(self, normalized_content: str) -> str:
        return hmac.new(
            self._key,
            normalized_content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


_KEY_LOCK = threading.Lock()
_KEY_CACHE: dict[str, bytes] = {}


def load_memory_key(database_path: Path) -> bytes:
    """Load a workspace memory key, preferring the operating-system keyring."""

    explicit = os.getenv("AGENT_MEMORY_ENCRYPTION_KEY", "").strip()
    if explicit:
        try:
            key = base64.urlsafe_b64decode(explicit.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise MemoryEncryptionError(
                "AGENT_MEMORY_ENCRYPTION_KEY 不是有效的 URL-safe Base64。"
            ) from exc
        if len(key) != 32:
            raise MemoryEncryptionError(
                "AGENT_MEMORY_ENCRYPTION_KEY 解码后必须是 32 字节。"
            )
        return key

    workspace = str(Path(__file__).resolve().parents[2])
    workspace_id = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:12]
    service = f"agent-lab-{workspace_id}"
    account = "memory-encryption-v1"
    cache_key = f"{service}:{account}"
    fallback_cache_key = f"file:{database_path.resolve()}"
    with _KEY_LOCK:
        cached = _KEY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        fallback_cached = _KEY_CACHE.get(fallback_cache_key)
        if fallback_cached is not None:
            return fallback_cached
        try:
            encoded = keyring.get_password(service, account)
        except (KeyringError, RuntimeError):
            encoded = None
        if encoded:
            try:
                key = base64.urlsafe_b64decode(encoded.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise MemoryEncryptionError("系统钥匙串中的记忆密钥已损坏。") from exc
            if len(key) != 32:
                raise MemoryEncryptionError("系统钥匙串中的记忆密钥长度无效。")
            _KEY_CACHE[cache_key] = key
            return key

        if _memory_key_path(database_path).exists():
            candidate = _load_file_fallback_key(database_path)
            encoded_candidate = base64.urlsafe_b64encode(candidate).decode("ascii")
            try:
                keyring.set_password(service, account, encoded_candidate)
            except (KeyringError, RuntimeError):
                cache_key = fallback_cache_key
            _KEY_CACHE[cache_key] = candidate
            return candidate

        candidate = os.urandom(32)
        encoded_candidate = base64.urlsafe_b64encode(candidate).decode("ascii")
        try:
            keyring.set_password(service, account, encoded_candidate)
        except (KeyringError, RuntimeError):
            candidate = _load_file_fallback_key(database_path)
            cache_key = fallback_cache_key
        _KEY_CACHE[cache_key] = candidate
        return candidate


def _load_file_fallback_key(database_path: Path) -> bytes:
    key_path = _memory_key_path(database_path)
    if key_path.exists():
        try:
            key = base64.urlsafe_b64decode(key_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise MemoryEncryptionError("本机记忆密钥文件已损坏。") from exc
        if len(key) != 32:
            raise MemoryEncryptionError("本机记忆密钥文件长度无效。")
        return key

    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    temporary = key_path.with_suffix(key_path.suffix + ".tmp")
    temporary.write_bytes(base64.urlsafe_b64encode(key))
    os.chmod(temporary, 0o600)
    try:
        temporary.replace(key_path)
    finally:
        temporary.unlink(missing_ok=True)
    return key


def _memory_key_path(database_path: Path) -> Path:
    return database_path.with_suffix(database_path.suffix + ".memory-key")


def record_aad(
    record_type: str,
    owner_id: str,
    record_id: str,
    field: str = "payload",
) -> bytes:
    """Bind ciphertext to its table, owner, record and logical field."""

    return (
        f"agent-context-v1\x1f{record_type}\x1f{owner_id}\x1f{record_id}\x1f{field}"
    ).encode("utf-8")


def memory_aad(owner_id: str, memory_id: str, field: str = "content") -> bytes:
    # Keep the original AAD stable so databases encrypted before the generic
    # record envelope was introduced remain decryptable.
    return f"agent-memory-v1\x1f{owner_id}\x1f{memory_id}\x1f{field}".encode(
        "utf-8"
    )
