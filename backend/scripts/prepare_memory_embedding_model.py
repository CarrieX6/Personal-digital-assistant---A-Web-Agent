from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.app.transformer_embedding import (  # noqa: E402
    MANIFEST_NAME,
    MANIFEST_VERSION,
    LocalTransformerMemoryEmbeddingProvider,
    load_embedding_manifest,
    sha256_file,
    verify_embedding_model_files,
)


DEFAULT_REPO_ID = "ibm-granite/granite-embedding-97m-multilingual-r2"
DEFAULT_MODEL_PATH = (
    REPOSITORY_ROOT
    / "backend"
    / "models"
    / "embeddings"
    / "granite-embedding-97m-multilingual-r2"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ALLOW_PATTERNS = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "vocab.txt",
    "merges.txt",
    "tokenizer.model",
    "sentencepiece.bpe.model",
)


def _model_files(model_path: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in model_path.rglob("*")
            if path.is_file()
            and path.name != MANIFEST_NAME
            and ".cache" not in path.relative_to(model_path).parts
        ),
        key=lambda item: item.relative_to(model_path).as_posix(),
    )


def _write_manifest(model_path: Path, *, revision: str) -> Path:
    files = _model_files(model_path)
    names = {path.relative_to(model_path).as_posix() for path in files}
    required = {"config.json", "model.safetensors"}
    if not required.issubset(names):
        raise RuntimeError("下载结果缺少 config.json 或 model.safetensors。")
    if not names.intersection({"tokenizer.json", "tokenizer.model", "vocab.txt"}):
        raise RuntimeError("下载结果缺少可用的 Tokenizer 文件。")
    payload: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "model_id": DEFAULT_REPO_ID,
        "revision": revision,
        "dimension": 384,
        "pooling": "cls",
        "normalize": True,
        "model_max_length": 32768,
        "runtime_max_length": 1024,
        "chunk_overlap": 64,
        "query_prefix": "",
        "document_prefix": "",
        "local_files_only": True,
        "trust_remote_code": False,
        "files": [
            {
                "path": path.relative_to(model_path).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in files
        ],
    }
    destination = model_path / MANIFEST_NAME
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _resolve_revision(repo_id: str, requested: str) -> str:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise RuntimeError("请先安装 backend/requirements.txt。") from exc
    info = HfApi().model_info(repo_id=repo_id, revision=requested)
    revision = str(info.sha or "").lower()
    if not _COMMIT_SHA.fullmatch(revision):
        raise RuntimeError("Hugging Face 未返回固定的模型提交 SHA。")
    return revision


def _download(model_path: Path, *, requested_revision: str) -> str:
    if model_path.exists() and any(model_path.iterdir()):
        if (model_path / MANIFEST_NAME).exists():
            raise RuntimeError(
                "目标模型已经包含清单；请使用 --verify 验证，或选择新的目录。"
            )
        unexpected_root_entries = [
            path.name
            for path in model_path.iterdir()
            if path.name != ".cache" and path.name not in _ALLOW_PATTERNS
        ]
        unexpected = [
            path.relative_to(model_path).as_posix()
            for path in _model_files(model_path)
            if path.relative_to(model_path).as_posix() not in _ALLOW_PATTERNS
        ]
        unexpected = sorted(set(unexpected_root_entries + unexpected))
        if unexpected:
            raise RuntimeError(
                "目标目录包含非模型文件，拒绝覆盖：" + ", ".join(unexpected[:5])
            )
        print("发现未完成的受控模型下载，将从本地缓存继续。")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("请先安装 backend/requirements.txt。") from exc
    revision = _resolve_revision(DEFAULT_REPO_ID, requested_revision)
    model_path.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=DEFAULT_REPO_ID,
        revision=revision,
        local_dir=model_path,
        allow_patterns=list(_ALLOW_PATTERNS),
    )
    _write_manifest(model_path, revision=revision)
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "显式下载、固定版本并验证本地 Granite 长期记忆 Embedding 模型。"
        )
    )
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    model_path = args.model_path.expanduser().resolve()
    print(f"Model: {DEFAULT_REPO_ID}")
    print("License: Apache-2.0")
    print(f"Destination: {model_path}")
    print("Runtime downloads: disabled")
    if not (args.download or args.verify or args.smoke_test):
        print("Plan only. Add --download to prepare the pinned local model.")
        return 0

    if args.download:
        revision = _download(model_path, requested_revision=args.revision)
        print(f"Pinned revision: {revision}")

    manifest = load_embedding_manifest(model_path / MANIFEST_NAME)
    verify_embedding_model_files(model_path, manifest, verify_hashes=True)
    print(f"Verified manifest: sha256={manifest.fingerprint}")
    if args.smoke_test:
        provider = LocalTransformerMemoryEmbeddingProvider(model_path)
        try:
            provider.warmup()
            print(
                f"Smoke test passed: model_id={provider.model_id} "
                f"dimension={provider.dimension}"
            )
        finally:
            provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
