from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "manage_photo_style.py"
SPEC = importlib.util.spec_from_file_location("manage_photo_style", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def test_update_env_preserves_unrelated_secrets(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_API_KEY=keep-this-secret\nPHOTO_STYLE_PROVIDER=local-preview\n",
        encoding="utf-8",
    )

    deployment.update_env(
        env_path,
        {
            "PHOTO_STYLE_PROVIDER": "pic-style-http",
            "PHOTO_STYLE_SERVICE_URL": "http://127.0.0.1:18000",
        },
    )

    values = deployment.read_env(env_path)
    assert values["LLM_API_KEY"] == "keep-this-secret"
    assert values["PHOTO_STYLE_PROVIDER"] == "pic-style-http"
    assert values["PHOTO_STYLE_SERVICE_URL"] == "http://127.0.0.1:18000"


def test_configure_agent_records_reproducible_service_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "agent"
    service = project / ".capabilities" / "pic-style"
    project.mkdir()
    monkeypatch.setattr(deployment, "PROJECT_ROOT", project)

    deployment.configure_agent(service, 18000, auto_start=True)

    values = deployment.read_env(project / ".env")
    assert values["PHOTO_STYLE_PROVIDER"] == "pic-style-http"
    assert values["PHOTO_STYLE_AUTO_START"] == "true"
    assert values["PHOTO_STYLE_SERVICE_ROOT"] == ".capabilities/pic-style"


def test_archive_source_state_is_reusable_without_git(tmp_path: Path) -> None:
    service = tmp_path / "pic-style"
    service.mkdir()
    deployment.write_source_state(
        service,
        requested_ref=deployment.PINNED_SOURCE_REF,
        resolved_commit=deployment.PINNED_SOURCE_REF,
        method="github-api-archive",
    )

    assert deployment.git_commit(service) == deployment.PINNED_SOURCE_REF
    assert (
        deployment.ensure_checkout(service, deployment.PINNED_SOURCE_REF)
        == deployment.PINNED_SOURCE_REF
    )
    with pytest.raises(deployment.DeploymentError, match="其他版本"):
        deployment.ensure_checkout(service, "different-ref")


def test_doctor_exposes_macos_mps_as_engineering_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(deployment.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(deployment, "nvidia_summary", lambda: None)
    monkeypatch.setattr(
        deployment,
        "torch_accelerator_summary",
        lambda: {
            "torch": "2.test",
            "cuda_available": False,
            "mps_available": True,
        },
    )
    monkeypatch.setattr(deployment, "request_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "git_commit", lambda _root: None)

    payload = deployment.doctor(tmp_path / "pic-style", 18000)

    assert payload["real_provider_automatic_deployment_supported"] is True
    assert "macos-mps-native" in payload["supported_real_profiles"]
    assert "quality_gate" in payload["real_provider_reason"]


def test_doctor_fails_closed_without_local_accelerator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment.platform, "system", lambda: "Linux")
    monkeypatch.setattr(deployment.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(deployment, "nvidia_summary", lambda: None)
    monkeypatch.setattr(
        deployment,
        "torch_accelerator_summary",
        lambda: {
            "torch": None,
            "cuda_available": False,
            "mps_available": False,
        },
    )
    monkeypatch.setattr(deployment, "request_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "git_commit", lambda _root: None)

    payload = deployment.doctor(tmp_path / "pic-style", 18000)

    assert payload["real_provider_automatic_deployment_supported"] is False
    assert payload["supported_real_profiles"] == ["remote-http"]


def test_prepare_windows_gpu_refuses_unsupported_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployment,
        "doctor",
        lambda *_args, **_kwargs: {
            "supported_real_profiles": ["remote-http", "macos-mps-native"],
        },
    )

    with pytest.raises(deployment.DeploymentError, match="Windows"):
        deployment.prepare_windows_gpu(
            tmp_path / "pic-style",
            accept_model_licenses=False,
        )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:18000/", "http://127.0.0.1:18000"),
        ("http://192.168.1.20:18000", "http://192.168.1.20:18000"),
        ("https://gpu.example.com/style/", "https://gpu.example.com/style"),
    ],
)
def test_remote_provider_url_policy_accepts_private_http_or_https(
    url: str,
    expected: str,
) -> None:
    assert deployment.validate_remote_service_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://gpu.example.com:18000",
        "ftp://127.0.0.1/service",
        "https://user:secret@gpu.example.com",
        "https://gpu.example.com/?token=secret",
    ],
)
def test_remote_provider_url_policy_rejects_insecure_or_secret_urls(url: str) -> None:
    with pytest.raises(deployment.DeploymentError):
        deployment.validate_remote_service_url(url)


def test_configure_remote_does_not_write_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    monkeypatch.setattr(deployment, "PROJECT_ROOT", project)

    deployment.configure_remote_agent(
        "https://gpu.example.com/photo-style",
        "workspace-a",
    )

    values = deployment.read_env(project / ".env")
    assert values["PHOTO_STYLE_PROVIDER"] == "pic-style-http"
    assert values["PHOTO_STYLE_AUTO_START"] == "false"
    assert values["PHOTO_STYLE_TENANT_ID"] == "workspace-a"
    assert "PHOTO_STYLE_SERVICE_API_KEY" not in values


def test_prepare_macos_mps_refuses_unavailable_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployment,
        "doctor",
        lambda *_args, **_kwargs: {"supported_real_profiles": ["remote-http"]},
    )

    with pytest.raises(deployment.DeploymentError, match="Apple Silicon"):
        deployment.prepare_macos_mps(accept_model_licenses=False)
