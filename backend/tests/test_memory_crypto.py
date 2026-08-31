from __future__ import annotations

import base64
from pathlib import Path

from backend.app import memory_crypto


def _fake_keyring(monkeypatch) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        memory_crypto.keyring,
        "get_password",
        lambda service, account: values.get((service, account)),
    )
    monkeypatch.setattr(
        memory_crypto.keyring,
        "set_password",
        lambda service, account, value: values.__setitem__(
            (service, account), value
        ),
    )
    return values


def test_memory_key_survives_database_and_checkout_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    values = _fake_keyring(monkeypatch)
    monkeypatch.delenv("AGENT_MEMORY_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_WORKSPACE_ID", "move-safe-test")
    memory_crypto._KEY_CACHE.clear()

    first = memory_crypto.load_memory_key(tmp_path / "old" / "memory.sqlite3")
    memory_crypto._KEY_CACHE.clear()
    second = memory_crypto.load_memory_key(tmp_path / "new" / "memory.sqlite3")

    assert second == first
    assert len(values) == 1
    assert next(iter(values))[0] == memory_crypto._memory_keyring_service()


def test_legacy_path_key_is_copied_to_stable_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    values = _fake_keyring(monkeypatch)
    monkeypatch.delenv("AGENT_MEMORY_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("AGENT_WORKSPACE_ID", "legacy-migration-test")
    memory_crypto._KEY_CACHE.clear()
    account = "memory-encryption-v1"
    legacy_key = b"l" * 32
    values[(memory_crypto._legacy_memory_keyring_service(), account)] = (
        base64.urlsafe_b64encode(legacy_key).decode("ascii")
    )

    loaded = memory_crypto.load_memory_key(tmp_path / "memory.sqlite3")

    assert loaded == legacy_key
    stable = values[(memory_crypto._memory_keyring_service(), account)]
    assert base64.urlsafe_b64decode(stable.encode("ascii")) == legacy_key
