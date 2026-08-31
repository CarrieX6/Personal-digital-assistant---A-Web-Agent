from __future__ import annotations

import json
from pathlib import Path

from scripts.start_public_viewer import _clear_state, _enabled, _write_state


def test_public_viewer_env_flags_are_explicit() -> None:
    assert _enabled("true") is True
    assert _enabled("ON") is True
    assert _enabled("false") is False
    assert _enabled(None) is False


def test_public_viewer_state_is_atomic_and_url_scoped(tmp_path: Path) -> None:
    state_path = tmp_path / "viewer-public-url.json"
    _write_state(
        state_path,
        {
            "url": "https://current.trycloudflare.com",
            "created_at": 1,
            "status": "connected",
        },
    )
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "connected"

    _clear_state(state_path, "https://other.trycloudflare.com")
    assert state_path.is_file()

    _clear_state(state_path, "https://current.trycloudflare.com")
    assert not state_path.exists()
