from __future__ import annotations

import stat
from pathlib import Path

import pytest

from backend.app.identity import IdentityBindingError, IdentityBindingRegistry


def test_feishu_identity_gets_stable_isolated_workspace(tmp_path: Path) -> None:
    registry = IdentityBindingRegistry(tmp_path / "identity.sqlite3")

    first = registry.ensure_feishu_binding(
        app_id="cli_app",
        open_id="ou_user_a",
    )
    repeated = registry.ensure_feishu_binding(
        app_id="cli_app",
        open_id="ou_user_a",
    )
    second = registry.ensure_feishu_binding(
        app_id="cli_app",
        open_id="ou_user_b",
    )

    assert repeated == first
    assert first.workspace_id != second.workspace_id
    assert first.owner_key == "feishu:cli_app:ou_user_a"
    assert second.owner_key == "feishu:cli_app:ou_user_b"
    assert first.device_id == second.device_id
    assert stat.S_IMODE(registry.path.stat().st_mode) == 0o600


def test_binding_status_blocks_resolution_and_workspace_can_be_renamed(
    tmp_path: Path,
) -> None:
    registry = IdentityBindingRegistry(tmp_path / "identity.sqlite3")
    binding = registry.ensure_feishu_binding(
        app_id="cli_app",
        open_id="ou_user",
    )

    renamed = registry.rename_workspace(binding.id, "卓钒的个人助手")
    suspended = registry.set_binding_status(binding.id, "suspended")

    assert renamed.workspace_name == "卓钒的个人助手"
    assert suspended.status == "suspended"
    with pytest.raises(IdentityBindingError, match="已停用"):
        registry.resolve_feishu_owner_key(
            app_id="cli_app",
            open_id="ou_user",
        )

    active = registry.set_binding_status(binding.id, "active")
    assert active.status == "active"
    assert registry.resolve_feishu_owner_key(
        app_id="cli_app",
        open_id="ou_user",
    ) == "feishu:cli_app:ou_user"
