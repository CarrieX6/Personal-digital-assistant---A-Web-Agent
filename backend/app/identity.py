from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class IdentityBindingError(ValueError):
    """A safe identity, workspace, or device binding error."""


@dataclass(frozen=True)
class IdentityBinding:
    id: str
    provider: str
    app_id: str
    external_id: str
    workspace_id: str
    workspace_name: str
    owner_key: str
    device_id: str
    device_name: str
    status: str
    created_at: float
    updated_at: float


class IdentityBindingRegistry:
    """Maps external chat identities to isolated workspaces and Agent nodes.

    The current product has one local Agent node. Keeping the device boundary in
    the schema lets a future control plane route the same identity to a different
    computer without changing memory or asset ownership again.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def ensure_feishu_binding(
        self,
        *,
        app_id: str,
        open_id: str,
        workspace_name: str | None = None,
    ) -> IdentityBinding:
        safe_app_id = self._required(app_id, "飞书 App ID")
        safe_open_id = self._required(open_id, "飞书 Open ID")
        with self._lock, self._connect() as connection:
            existing = self._select_identity(
                connection,
                provider="feishu",
                app_id=safe_app_id,
                external_id=safe_open_id,
            )
            if existing is not None:
                return self._binding_from_row(existing)

            now = time.time()
            workspace_id = str(uuid4())
            binding_id = str(uuid4())
            device_id, device_name = self._ensure_local_device(connection, now)
            owner_key = self.legacy_feishu_owner_key(safe_app_id, safe_open_id)
            display_name = self._workspace_name(
                workspace_name,
                safe_open_id,
            )
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, name, owner_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (workspace_id, display_name, owner_key, now, now),
            )
            connection.execute(
                """
                INSERT INTO external_identity_bindings (
                    id, provider, app_id, external_id, workspace_id,
                    device_id, status, created_at, updated_at
                ) VALUES (?, 'feishu', ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    binding_id,
                    safe_app_id,
                    safe_open_id,
                    workspace_id,
                    device_id,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                action="binding.created",
                binding_id=binding_id,
                detail="auto-provisioned local Feishu workspace",
                timestamp=now,
            )
            created = self._select_identity(
                connection,
                provider="feishu",
                app_id=safe_app_id,
                external_id=safe_open_id,
            )
            assert created is not None
            return self._binding_from_row(created)

    def resolve_feishu_owner_key(self, *, app_id: str, open_id: str) -> str:
        binding = self.ensure_feishu_binding(app_id=app_id, open_id=open_id)
        if binding.status != "active":
            raise IdentityBindingError("该飞书账号的个人工作区已停用，请联系本机管理员。")
        return binding.owner_key

    def list_bindings(self, limit: int = 200) -> list[IdentityBinding]:
        safe_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                self._binding_select_sql()
                + " ORDER BY identity.updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._binding_from_row(row) for row in rows]

    def set_binding_status(self, binding_id: str, status: str) -> IdentityBinding:
        if status not in {"active", "suspended", "revoked"}:
            raise IdentityBindingError("绑定状态无效。")
        with self._lock, self._connect() as connection:
            now = time.time()
            cursor = connection.execute(
                """
                UPDATE external_identity_bindings
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, binding_id),
            )
            if cursor.rowcount != 1:
                raise IdentityBindingError("找不到这个账号绑定。")
            self._audit(
                connection,
                action=f"binding.{status}",
                binding_id=binding_id,
                detail="changed from local Root console",
                timestamp=now,
            )
            row = connection.execute(
                self._binding_select_sql() + " WHERE identity.id = ?",
                (binding_id,),
            ).fetchone()
            assert row is not None
            return self._binding_from_row(row)

    def rename_workspace(self, binding_id: str, name: str) -> IdentityBinding:
        safe_name = " ".join(name.strip().split())[:80]
        if not safe_name:
            raise IdentityBindingError("工作区名称不能为空。")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT workspace_id FROM external_identity_bindings WHERE id = ?",
                (binding_id,),
            ).fetchone()
            if row is None:
                raise IdentityBindingError("找不到这个账号绑定。")
            now = time.time()
            connection.execute(
                "UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?",
                (safe_name, now, str(row[0])),
            )
            connection.execute(
                "UPDATE external_identity_bindings SET updated_at = ? WHERE id = ?",
                (now, binding_id),
            )
            self._audit(
                connection,
                action="workspace.renamed",
                binding_id=binding_id,
                detail=safe_name,
                timestamp=now,
            )
            updated = connection.execute(
                self._binding_select_sql() + " WHERE identity.id = ?",
                (binding_id,),
            ).fetchone()
            assert updated is not None
            return self._binding_from_row(updated)

    @staticmethod
    def legacy_feishu_owner_key(app_id: str, open_id: str) -> str:
        return f"feishu:{app_id}:{open_id}"

    @staticmethod
    def _required(value: str, label: str) -> str:
        safe = value.strip()
        if (
            not safe
            or len(safe) > 160
            or re.fullmatch(r"[A-Za-z0-9._-]+", safe) is None
        ):
            raise IdentityBindingError(f"{label}无效。")
        return safe

    @staticmethod
    def _workspace_name(value: str | None, open_id: str) -> str:
        if value:
            safe = " ".join(value.strip().split())[:80]
            if safe:
                return safe
        suffix = open_id[-6:] if len(open_id) > 6 else open_id
        return f"飞书个人工作区 · {suffix}"

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS registry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS external_identity_bindings (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(provider, app_id, external_id),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
                    FOREIGN KEY(device_id) REFERENCES devices(id)
                );

                CREATE TABLE IF NOT EXISTS identity_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    binding_id TEXT,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS identity_binding_workspace_idx
                ON external_identity_bindings(workspace_id, status);
                """
            )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_local_device(
        self,
        connection: sqlite3.Connection,
        timestamp: float,
    ) -> tuple[str, str]:
        row = connection.execute(
            "SELECT value FROM registry_meta WHERE key = 'local_device_id'"
        ).fetchone()
        if row is not None:
            device_id = str(row[0])
            device = connection.execute(
                "SELECT name FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if device is not None:
                return device_id, str(device[0])
        device_id = str(uuid4())
        device_name = "当前本机 Agent"
        connection.execute(
            """
            INSERT INTO devices (
                id, name, kind, status, created_at, updated_at
            ) VALUES (?, ?, 'local', 'online', ?, ?)
            """,
            (device_id, device_name, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO registry_meta (key, value) VALUES ('local_device_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (device_id,),
        )
        return device_id, device_name

    @staticmethod
    def _binding_select_sql() -> str:
        return """
            SELECT identity.id, identity.provider, identity.app_id,
                   identity.external_id, workspace.id, workspace.name,
                   workspace.owner_key, device.id, device.name,
                   identity.status, identity.created_at, identity.updated_at
            FROM external_identity_bindings AS identity
            JOIN workspaces AS workspace ON workspace.id = identity.workspace_id
            JOIN devices AS device ON device.id = identity.device_id
        """

    def _select_identity(
        self,
        connection: sqlite3.Connection,
        *,
        provider: str,
        app_id: str,
        external_id: str,
    ) -> sqlite3.Row | tuple | None:
        return connection.execute(
            self._binding_select_sql()
            + """
              WHERE identity.provider = ?
                AND identity.app_id = ?
                AND identity.external_id = ?
            """,
            (provider, app_id, external_id),
        ).fetchone()

    @staticmethod
    def _binding_from_row(row: sqlite3.Row | tuple) -> IdentityBinding:
        return IdentityBinding(
            id=str(row[0]),
            provider=str(row[1]),
            app_id=str(row[2]),
            external_id=str(row[3]),
            workspace_id=str(row[4]),
            workspace_name=str(row[5]),
            owner_key=str(row[6]),
            device_id=str(row[7]),
            device_name=str(row[8]),
            status=str(row[9]),
            created_at=float(row[10]),
            updated_at=float(row[11]),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        action: str,
        binding_id: str,
        detail: str,
        timestamp: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO identity_audit_log (
                action, binding_id, detail, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (action, binding_id, detail[:500], timestamp),
        )
