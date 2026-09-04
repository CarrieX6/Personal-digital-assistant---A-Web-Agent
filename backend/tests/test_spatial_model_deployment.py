from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "backend/scripts/prepare_spatial_models.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_spatial_models", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
preparation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparation)


def test_verify_lock_accepts_pinned_files(tmp_path: Path) -> None:
    root = tmp_path / "spatial"
    model = root / "depth"
    model.mkdir(parents=True)
    config = model / "config.json"
    config.write_text('{"model":"test"}', encoding="utf-8")
    lock = root / "spatial-model-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "pda_spatial_model_lock_v1",
                "runtime_downloads_allowed": False,
                "models": [
                    {
                        "local_dir": "depth",
                        "files": [
                            {
                                "path": "config.json",
                                "size": config.stat().st_size,
                                "sha256": preparation.sha256_file(config),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = preparation.verify_lock(root, lock)

    assert payload["runtime_downloads_allowed"] is False


def test_verify_lock_rejects_modified_model_file(tmp_path: Path) -> None:
    root = tmp_path / "spatial"
    model = root / "birefnet"
    model.mkdir(parents=True)
    weights = model / "model.safetensors"
    weights.write_bytes(b"original")
    lock = root / "spatial-model-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "pda_spatial_model_lock_v1",
                "models": [
                    {
                        "local_dir": "birefnet",
                        "files": [
                            {
                                "path": weights.name,
                                "size": weights.stat().st_size,
                                "sha256": preparation.sha256_file(weights),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    weights.write_bytes(b"modified")

    with pytest.raises(preparation.ModelPreparationError, match="哈希不一致"):
        preparation.verify_lock(root, lock)
