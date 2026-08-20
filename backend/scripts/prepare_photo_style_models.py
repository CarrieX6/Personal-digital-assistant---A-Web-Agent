from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.style_model_manifest import (  # noqa: E402
    MODEL_MARKER_NAME,
    ModelArtifact,
    ModelManifest,
    load_model_manifest,
    resolve_component_dir,
    sha256_file,
    verify_prepared_models,
    verify_provider_gate,
)


DEFAULT_MANIFEST = PROJECT_ROOT / "backend" / "config" / "photo-style-models.json"
DEFAULT_GATE = PROJECT_ROOT / "backend" / "config" / "photo-style-provider-gate.json"
DEFAULT_ROOT = PROJECT_ROOT / "backend" / "models" / "photo-style"


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def print_plan(manifest: ModelManifest, root: Path) -> None:
    print(f"Manifest: {manifest.version} sha256={manifest.fingerprint}")
    print(
        f"Reference: {manifest.source_repository} @ {manifest.source_commit}"
    )
    print(f"Destination root: {root.resolve()}")
    for component, artifact in manifest.components():
        print(
            f"- {component}: {artifact.id} @ {artifact.revision}\n"
            f"  license={artifact.license} review={artifact.license_url}\n"
            f"  download={_human_bytes(artifact.estimated_download_bytes)} "
            f"files={len(artifact.required_files)} "
            f"destination={resolve_component_dir(root, artifact)}"
        )
    print(f"Selected download total: {_human_bytes(manifest.active_download_bytes)}")
    print("Runtime downloads: disabled; ControlNet: excluded from the 8 GB path")


def _existing_ancestor(path: Path) -> Path:
    candidate = path.resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _assert_disk_capacity(manifest: ModelManifest, root: Path) -> None:
    free = shutil.disk_usage(_existing_ancestor(root)).free
    reserve = max(2 * 1024**3, int(manifest.active_download_bytes * 0.10))
    required = manifest.active_download_bytes + reserve
    if free < required:
        raise RuntimeError(
            f"insufficient disk space: need {_human_bytes(required)}, "
            f"have {_human_bytes(free)}"
        )


def _verify_required_files(artifact: ModelArtifact, destination: Path) -> None:
    for required in artifact.required_files:
        file_path = destination / Path(required.path)
        if not file_path.is_file():
            raise RuntimeError(f"download did not produce required file: {file_path}")
        if file_path.stat().st_size != required.size_bytes:
            raise RuntimeError(f"downloaded file has unexpected size: {file_path}")
        if required.sha256 and sha256_file(file_path) != required.sha256:
            raise RuntimeError(f"downloaded file failed SHA-256: {file_path}")


def _write_marker(
    manifest: ModelManifest,
    component: str,
    artifact: ModelArtifact,
    destination: Path,
) -> None:
    marker = {
        "version": "personal_agent_prepared_model_v1",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "id": artifact.id,
        "revision": artifact.revision,
        "license": artifact.license,
        "license_url": artifact.license_url,
        "license_acceptance_attested": True,
        "manifest_fingerprint": manifest.fingerprint,
    }
    (destination / MODEL_MARKER_NAME).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _download_with_retries(
    snapshot_download: Any,
    *,
    artifact: ModelArtifact,
    destination: Path,
    cache_dir: Path,
    attempts: int = 5,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            snapshot_download(
                repo_id=artifact.id,
                revision=artifact.revision,
                local_dir=destination,
                allow_patterns=list(artifact.allow_patterns),
                cache_dir=cache_dir,
            )
            return
        except Exception as exc:
            if attempt == attempts:
                raise
            delay = min(20, 2**attempt)
            print(
                f"Download {attempt}/{attempts} failed for {artifact.id}: "
                f"{type(exc).__name__}; retrying in {delay}s"
            )
            time.sleep(delay)


def download_models(manifest: ModelManifest, root: Path) -> None:
    cache_root = root.resolve() / ".cache" / "huggingface"
    hub_cache = cache_root / "hub"
    xet_cache = cache_root / "xet"
    hub_cache.mkdir(parents=True, exist_ok=True)
    xet_cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_root)
    os.environ["HF_XET_CACHE"] = str(xet_cache)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "Install backend/requirements-gpu.txt before downloading models"
        ) from exc
    _assert_disk_capacity(manifest, root)
    for component, artifact in manifest.components():
        destination = resolve_component_dir(root, artifact)
        destination.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {component}: {artifact.id} @ {artifact.revision}")
        _download_with_retries(
            snapshot_download,
            artifact=artifact,
            destination=destination,
            cache_dir=hub_cache,
        )
        _verify_required_files(artifact, destination)
        _write_marker(manifest, component, artifact, destination)
        print(f"Verified {component}: {destination}")


def _read_marker(path: Path) -> dict[str, Any]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("prepared marker must be an object")
    return payload


def write_lock(manifest: ModelManifest, root: Path, output: Path) -> None:
    resolved_root = root.resolve()
    files: list[dict[str, Any]] = []
    components: dict[str, dict[str, Any]] = {}
    for component, artifact in manifest.components():
        directory = resolve_component_dir(root, artifact)
        _verify_required_files(artifact, directory)
        marker = _read_marker(directory / MODEL_MARKER_NAME)
        if marker.get("license_acceptance_attested") is not True:
            raise RuntimeError(f"license acceptance missing for {component}")
        if marker.get("manifest_fingerprint") != manifest.fingerprint:
            raise RuntimeError(f"prepared marker is stale for {component}")
        components[component] = {
            "id": artifact.id,
            "revision": artifact.revision,
            "license": artifact.license,
            "license_url": artifact.license_url,
            "license_acceptance_attested": True,
        }
        for required in artifact.required_files:
            path = directory / Path(required.path)
            files.append(
                {
                    "component": component,
                    "path": path.relative_to(resolved_root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    payload = {
        "version": "personal_agent_model_lock_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_fingerprint": manifest.fingerprint,
        "source_model_manifest_fingerprint": (
            manifest.source_model_manifest_fingerprint
        ),
        "components": components,
        "files": files,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Plan, explicitly download, license-attest and verify pinned "
            "photo-style models."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--accept-model-licenses", action="store_true")
    parser.add_argument("--verify-checksums", action="store_true")
    parser.add_argument("--lock-output", type=Path)
    args = parser.parse_args()

    manifest = load_model_manifest(args.manifest)
    gate_errors = verify_provider_gate(manifest, args.gate)
    if gate_errors:
        raise SystemExit("\n".join(gate_errors))
    lock_output = args.lock_output or args.root / "model-lock.json"
    print_plan(manifest, args.root)
    if args.plan and not args.download:
        return
    if args.download:
        if not args.accept_model_licenses:
            raise SystemExit(
                "Review every printed license URL, then rerun with "
                "--accept-model-licenses to record explicit acceptance."
            )
        download_models(manifest, args.root)

    write_lock(manifest, args.root, lock_output)
    errors = verify_prepared_models(
        manifest,
        args.root,
        lock_path=lock_output,
        verify_hashes=args.verify_checksums or args.download,
    )
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Prepared model lock: {lock_output}")


if __name__ == "__main__":
    main()
