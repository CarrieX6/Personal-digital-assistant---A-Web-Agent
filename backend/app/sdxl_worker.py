from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from .assets import AssetError
from .sdxl_style_provider import NativeSDXLStyleProvider
from .style_transfer import StyleParameters


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run(request_path: Path) -> int:
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    provider = NativeSDXLStyleProvider(
        model_root=Path(payload["model_root"]),
        manifest_path=Path(payload["manifest_path"]),
        gate_path=Path(payload["gate_path"]),
        lock_path=Path(payload["lock_path"]),
        lcm_preview_enabled=bool(payload["lcm_preview_enabled"]),
        verify_hashes=bool(payload["verify_hashes"]),
        embedding_cache_entries=int(payload["embedding_cache_entries"]),
        unload_after_generation=False,
        accelerator=str(payload.get("accelerator") or "auto"),
    )
    try:
        with Image.open(payload["content_path"]) as source:
            content = source.convert("RGB")
        styles: list[Image.Image] = []
        for style_path in payload["style_paths"]:
            with Image.open(style_path) as style:
                styles.append(style.convert("RGB"))
        result = provider.stylize(
            content,
            styles,
            StyleParameters(**payload["parameters"]).validated(),
            lambda value, stage, message: emit(
                {
                    "type": "progress",
                    "value": value,
                    "stage": stage,
                    "message": message,
                }
            ),
        )
        result.image.save(payload["result_path"], "PNG")
        runtime = result.metadata.setdefault("runtime", {})
        runtime["model_load_seconds"] = provider._load_seconds
        Path(payload["metadata_path"]).write_text(
            json.dumps(result.metadata, ensure_ascii=False),
            encoding="utf-8",
        )
        emit({"type": "result"})
        return 0
    except AssetError as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    except Exception:
        logging.exception("Unhandled isolated SDXL worker failure")
        emit({"type": "error", "message": "隔离 SDXL 进程生成失败。"})
        return 1
    finally:
        provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one isolated SDXL style job")
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    return run(arguments.request)


if __name__ == "__main__":
    raise SystemExit(main())
