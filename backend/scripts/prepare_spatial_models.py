from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPOSITORY_ROOT / "backend/models/spatial"
DEFAULT_LOCK = DEFAULT_ROOT / "spatial-model-lock.json"
MODELS = (
    {
        "name": "depth_anything_v2_small",
        "repo_id": "depth-anything/Depth-Anything-V2-Small-hf",
        "local_dir": "depth-anything-v2-small",
    },
    {
        "name": "birefnet",
        "repo_id": "ZhengPeng7/BiRefNet",
        "local_dir": "birefnet",
    },
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ModelPreparationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and ".cache" not in path.relative_to(root).parts
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )


def resolve_revision(repo_id: str, requested: str) -> str:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise ModelPreparationError("请先安装 backend/requirements.txt。") from exc
    info = HfApi().model_info(repo_id=repo_id, revision=requested)
    revision = str(info.sha or "").lower()
    if not COMMIT_SHA.fullmatch(revision):
        raise ModelPreparationError(f"{repo_id} 未返回固定提交 SHA。")
    return revision


def download_models(root: Path, requested_revision: str) -> dict[str, Any]:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise ModelPreparationError("请先安装 backend/requirements.txt。") from exc
    records: list[dict[str, Any]] = []
    for model in MODELS:
        repo_id = str(model["repo_id"])
        revision = resolve_revision(repo_id, requested_revision)
        destination = root / str(model["local_dir"])
        destination.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {repo_id}@{revision} -> {destination}", flush=True)
        snapshot_download(
            repo_id=repo_id,
            revision=revision,
            local_dir=destination,
        )
        files = model_files(destination)
        if not files or not (destination / "config.json").is_file():
            raise ModelPreparationError(f"{repo_id} 下载结果缺少 config.json。")
        records.append(
            {
                **model,
                "revision": revision,
                "files": [
                    {
                        "path": path.relative_to(destination).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                    for path in files
                ],
            }
        )
    return {
        "schema_version": "pda_spatial_model_lock_v1",
        "runtime_downloads_allowed": False,
        "models": records,
    }


def verify_lock(root: Path, lock_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ModelPreparationError("空间模型锁不存在或损坏。") from exc
    if payload.get("schema_version") != "pda_spatial_model_lock_v1":
        raise ModelPreparationError("空间模型锁版本不受支持。")
    for model in payload.get("models", []):
        if not isinstance(model, dict):
            raise ModelPreparationError("空间模型锁条目无效。")
        destination = root / str(model.get("local_dir", ""))
        for record in model.get("files", []):
            path = destination / str(record.get("path", ""))
            if not path.is_file():
                raise ModelPreparationError(f"空间模型文件缺失：{path}")
            if path.stat().st_size != int(record.get("size", -1)):
                raise ModelPreparationError(f"空间模型文件大小不一致：{path}")
            if sha256_file(path) != record.get("sha256"):
                raise ModelPreparationError(f"空间模型文件哈希不一致：{path}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载、固定并验证 Depth Anything V2 Small 与 BiRefNet。"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    lock_path = args.lock.expanduser().resolve()
    print("Spatial models:")
    for model in MODELS:
        print(f"- {model['repo_id']} -> {root / str(model['local_dir'])}")
    print("Runtime downloads: disabled after preparation")
    if not (args.download or args.verify):
        print("Plan only. Add --download to prepare fixed local snapshots.")
        return 0
    if args.download:
        if lock_path.is_file():
            print("Existing lock found; verifying instead of following floating revisions.")
        else:
            payload = download_models(root, args.revision)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    verified = verify_lock(root, lock_path)
    print(f"Verified spatial model lock: {len(verified['models'])} models")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelPreparationError as exc:
        print(f"空间模型准备失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
