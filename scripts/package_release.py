from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "dist"
DENIED_EXACT = {
    ".env",
    "backend/data/settings.json",
    "backend/data/runs.jsonl",
    "backend/data/.secret_master_key",
}
DENIED_PARTS = {".venv", "node_modules", "__pycache__", ".capabilities"}


def normalize_version(value: str) -> str:
    version = value.strip()
    if version.startswith("v") and len(version) > 1 and version[1].isdigit():
        version = version[1:]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
        raise ValueError("version must contain only letters, digits, dots, dashes or underscores")
    return version


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    paths = [Path(raw.decode("utf-8")) for raw in completed.stdout.split(b"\0") if raw]
    selected: list[Path] = []
    for path in paths:
        normalized = path.as_posix()
        if normalized in DENIED_EXACT or any(part in DENIED_PARTS for part in path.parts):
            continue
        if normalized.startswith("backend/data/assets/") or normalized.startswith(
            "backend/data/source-images/"
        ):
            continue
        if (PROJECT_ROOT / path).is_file():
            selected.append(path)
    return sorted(selected)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(version: str, output: Path) -> list[Path]:
    version = normalize_version(version)
    output.mkdir(parents=True, exist_ok=True)
    root_name = f"personal-digital-assistant-{version}"
    files = tracked_files()
    if not files:
        raise RuntimeError("no tracked files found")
    tar_path = output / f"{root_name}-macos-linux.tar.gz"
    zip_path = output / f"{root_name}-windows.zip"
    with tarfile.open(tar_path, "w:gz") as archive:
        for relative in files:
            archive.add(PROJECT_ROOT / relative, arcname=Path(root_name) / relative)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(PROJECT_ROOT / relative, Path(root_name) / relative)
    checksum_path = output / "SHA256SUMS"
    checksum_path.write_text(
        "".join(f"{sha256(path)}  {path.name}{os.linesep}" for path in (tar_path, zip_path)),
        encoding="utf-8",
    )
    return [tar_path, zip_path, checksum_path]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build secret-free Web Agent release bundles")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    for artifact in build(args.version, args.output):
        print(artifact)


if __name__ == "__main__":
    main()
