from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_FILE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_MARKER_NAME = ".photo-style-model.json"


def _safe_relative_path(value: str, label: str) -> str:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"{label} must be a safe relative POSIX path")
    if "\\" in value:
        raise ValueError(f"{label} must use POSIX separators")
    return value


class RequiredModelFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int = Field(ge=1)
    sha256: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _safe_relative_path(value, "required file path")

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _FILE_SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase 64-character digest")
        return value


class ModelArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    revision: str
    local_path: str
    source_url: str
    license: str = Field(min_length=1)
    license_url: str
    requires_license_acceptance: bool = True
    estimated_download_bytes: int = Field(ge=1)
    allow_patterns: tuple[str, ...] = Field(min_length=1)
    required_files: tuple[RequiredModelFile, ...] = Field(min_length=1)

    @field_validator("revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("revision must be an immutable 40-character commit SHA")
        return value

    @field_validator("local_path")
    @classmethod
    def validate_local_path(cls, value: str) -> str:
        return _safe_relative_path(value, "local_path")

    @field_validator("allow_patterns")
    @classmethod
    def validate_allow_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _safe_relative_path(value, "allow pattern")
        return values

    @field_validator("source_url", "license_url")
    @classmethod
    def validate_https_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("model and license URLs must use HTTPS")
        return value

    @model_validator(mode="after")
    def validate_required_files(self) -> ModelArtifact:
        paths = [item.path for item in self.required_files]
        if len(paths) != len(set(paths)):
            raise ValueError("required_files contains duplicate paths")
        if sum(item.size_bytes for item in self.required_files) != (
            self.estimated_download_bytes
        ):
            raise ValueError(
                "estimated_download_bytes must equal required file sizes"
            )
        return self


class BaseModelArtifact(ModelArtifact):
    variant: str = "fp16"


class IPAdapterArtifact(ModelArtifact):
    subfolder: str
    weight_name: str
    image_encoder_subfolder: str

    @field_validator("subfolder", "weight_name", "image_encoder_subfolder")
    @classmethod
    def validate_adapter_path(cls, value: str) -> str:
        return _safe_relative_path(value, "IP-Adapter path")


class PreviewAcceleratorArtifact(ModelArtifact):
    weight_name: str

    @field_validator("weight_name")
    @classmethod
    def validate_weight_name(cls, value: str) -> str:
        return _safe_relative_path(value, "preview weight name")


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    precision: str
    attention_backend: str
    vae_tiling: bool
    cpu_offload: str
    max_concurrency: int = Field(ge=1, le=1)
    local_files_only: bool


class ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^personal_agent_model_manifest_v[0-9]+$")
    provider: str
    source_repository: str
    source_commit: str
    source_model_manifest_fingerprint: str
    runtime_downloads_allowed: bool
    base: BaseModelArtifact
    ip_adapter: IPAdapterArtifact
    preview_accelerator: PreviewAcceleratorArtifact
    runtime: RuntimePolicy

    @field_validator("source_commit")
    @classmethod
    def validate_source_commit(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("source_commit must be an immutable commit SHA")
        return value

    @field_validator("source_model_manifest_fingerprint")
    @classmethod
    def validate_source_fingerprint(cls, value: str) -> str:
        if not _FILE_SHA256.fullmatch(value):
            raise ValueError("source manifest fingerprint must be SHA-256")
        return value

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> ModelManifest:
        if self.runtime_downloads_allowed:
            raise ValueError("runtime model downloads must remain disabled")
        if not self.runtime.local_files_only:
            raise ValueError("the native provider must load local files only")
        if self.runtime.max_concurrency != 1:
            raise ValueError("the 8 GB provider must remain single-concurrency")
        return self

    def components(self) -> Iterator[tuple[str, ModelArtifact]]:
        yield "base", self.base
        yield "ip_adapter", self.ip_adapter
        yield "preview_accelerator", self.preview_accelerator

    @property
    def fingerprint(self) -> str:
        serialized = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @property
    def active_download_bytes(self) -> int:
        return sum(item.estimated_download_bytes for _, item in self.components())


def load_model_manifest(path: Path) -> ModelManifest:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("model manifest must be an object")
    return ModelManifest.model_validate(payload)


def resolve_component_dir(root: Path, artifact: ModelArtifact) -> Path:
    resolved_root = root.resolve()
    destination = (resolved_root / Path(artifact.local_path)).resolve()
    if destination == resolved_root or resolved_root not in destination.parents:
        raise ValueError(f"model path escapes model root: {artifact.local_path}")
    return destination


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_provider_gate(manifest: ModelManifest, gate_path: Path) -> list[str]:
    try:
        payload: Any = json.loads(gate_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return [f"cannot read provider gate: {type(exc).__name__}"]
    if not isinstance(payload, dict):
        return ["provider gate must be an object"]
    local_validation = payload.get("local_validation")
    local_validation_valid = (
        isinstance(local_validation, dict)
        and local_validation.get("status") == "engineering_smoke_passed"
        and local_validation.get("production_quality") is False
    )
    checks = (
        (payload.get("schema_version") == "native_provider_gate_v1", "schema"),
        (payload.get("provider") == manifest.provider, "provider"),
        (payload.get("status") == "accepted_upstream", "status"),
        (
            payload.get("source_repository") == manifest.source_repository,
            "source repository",
        ),
        (payload.get("source_commit") == manifest.source_commit, "source commit"),
        (
            payload.get("source_model_manifest_fingerprint")
            == manifest.source_model_manifest_fingerprint,
            "source manifest fingerprint",
        ),
        (
            payload.get("local_validation_required") is not True
            or local_validation_valid,
            "local validation",
        ),
    )
    return [f"provider gate {label} mismatch" for valid, label in checks if not valid]


def verify_prepared_models(
    manifest: ModelManifest,
    root: Path,
    *,
    lock_path: Path,
    verify_hashes: bool = False,
) -> list[str]:
    errors: list[str] = []
    resolved_root = root.resolve()
    lock_files: dict[str, dict[str, Any]] = {}
    lock_components: dict[str, dict[str, Any]] = {}
    try:
        lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        if lock_payload.get("version") != "personal_agent_model_lock_v1":
            errors.append("unsupported model lock version")
        if lock_payload.get("manifest_fingerprint") != manifest.fingerprint:
            errors.append("model lock does not match the current manifest")
        lock_files = {
            str(item.get("path")): item
            for item in lock_payload.get("files", [])
            if isinstance(item, dict)
        }
        lock_components = {
            str(name): item
            for name, item in lock_payload.get("components", {}).items()
            if isinstance(item, dict)
        }
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"cannot read model lock: {type(exc).__name__}")

    for component, artifact in manifest.components():
        directory = resolve_component_dir(resolved_root, artifact)
        if not directory.is_dir():
            errors.append(f"missing prepared model directory: {component}")
            continue
        marker_path = directory / MODEL_MARKER_NAME
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("id") != artifact.id or marker.get("revision") != (
                artifact.revision
            ):
                errors.append(f"prepared marker mismatch: {component}")
            if marker.get("manifest_fingerprint") != manifest.fingerprint:
                errors.append(f"prepared marker is stale: {component}")
            if artifact.requires_license_acceptance and marker.get(
                "license_acceptance_attested"
            ) is not True:
                errors.append(f"license acceptance is not attested: {component}")
        except (OSError, ValueError, TypeError):
            errors.append(f"missing or invalid prepared marker: {component}")

        lock_component = lock_components.get(component)
        if lock_component is None:
            errors.append(f"model lock is missing component: {component}")
        elif artifact.requires_license_acceptance and lock_component.get(
            "license_acceptance_attested"
        ) is not True:
            errors.append(f"model lock lacks license acceptance: {component}")

        for required in artifact.required_files:
            file_path = directory / Path(required.path)
            relative = file_path.relative_to(resolved_root).as_posix()
            if not file_path.is_file():
                errors.append(f"missing required model file: {relative}")
                continue
            if file_path.stat().st_size != required.size_bytes:
                errors.append(f"size mismatch: {relative}")
                continue
            lock_entry = lock_files.get(relative)
            if lock_entry is None:
                errors.append(f"model lock is missing file: {relative}")
            elif (
                lock_entry.get("size_bytes") != required.size_bytes
                or (
                    required.sha256 is not None
                    and lock_entry.get("sha256") != required.sha256
                )
            ):
                errors.append(f"model lock file mismatch: {relative}")
            if verify_hashes and required.sha256:
                if sha256_file(file_path) != required.sha256:
                    errors.append(f"SHA-256 mismatch: {relative}")
    return errors
