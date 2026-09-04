from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("unified_deployment", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def test_default_install_is_complete_and_real() -> None:
    parser = deployment.build_parser()

    args = parser.parse_args(["install"])

    assert args.profile == "complete"
    assert args.photo_style == "real"
    assert args.accept_model_licenses is False


def test_explicit_test_provider_is_not_the_default() -> None:
    parser = deployment.build_parser()

    args = parser.parse_args(["install", "--photo-style", "test"])

    assert args.photo_style == "test"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v22.13.0", (22, 13, 0)),
        ("Python 3.12.8", (3, 12, 8)),
        ("pnpm 11.9", (11, 9, 0)),
    ],
)
def test_parse_version(raw: str, expected: tuple[int, int, int]) -> None:
    assert deployment.parse_version(raw) == expected


def test_update_env_preserves_secrets_and_unrelated_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "LLM_API_KEY=keep-me\nPHOTO_STYLE_PROVIDER=pic-style-http\n",
        encoding="utf-8",
    )

    deployment.update_env(
        {
            "PHOTO_STYLE_PROVIDER": "sdxl-local",
            "PHOTO_STYLE_ACCELERATOR": "mps",
        }
    )

    values = deployment.read_env()
    assert values["LLM_API_KEY"] == "keep-me"
    assert values["PHOTO_STYLE_PROVIDER"] == "sdxl-local"
    assert values["PHOTO_STYLE_ACCELERATOR"] == "mps"


def test_noninteractive_real_install_requires_explicit_license_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment.sys.stdin, "isatty", lambda: False)

    with pytest.raises(deployment.DeploymentError, match="accept-model-licenses"):
        deployment.confirm_model_licenses(False)


def test_complete_check_rejects_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployment,
        "doctor_payload",
        lambda: {
            "web_http": 200,
            "api_health": {"status": "ok"},
            "photo_style": {
                "configured_provider": "pic-style-http",
                "production_quality": False,
            },
        },
    )

    with pytest.raises(deployment.DeploymentError, match="验收失败"):
        deployment.check(object())


def test_encrypted_bundle_round_trip(tmp_path: Path) -> None:
    plain = tmp_path / "plain.tar.gz"
    bundle = tmp_path / "runtime.pdabundle"
    restored = tmp_path / "restored.tar.gz"
    plain.write_bytes((b"private-agent-state\n" * 4096) + b"done")

    deployment.encrypt_file(plain, bundle, "correct horse battery staple")
    deployment.decrypt_file(bundle, restored, "correct horse battery staple")

    assert restored.read_bytes() == plain.read_bytes()
    with pytest.raises(deployment.DeploymentError, match="口令错误"):
        deployment.decrypt_file(bundle, tmp_path / "wrong.tar.gz", "incorrect passphrase")


def test_runtime_files_excludes_ephemeral_public_viewer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment, "PROJECT_ROOT", tmp_path)
    data = tmp_path / "backend/data"
    data.mkdir(parents=True)
    (tmp_path / ".env").write_text("A=B\n", encoding="utf-8")
    (data / "agent_memory.sqlite3").write_bytes(b"db")
    (data / "viewer-public-url.json").write_text("{}", encoding="utf-8")

    relative = {
        path.relative_to(tmp_path).as_posix()
        for path in deployment.runtime_files(
            include_models=False,
            include_capabilities=False,
        )
    }

    assert ".env" in relative
    assert "backend/data/agent_memory.sqlite3" in relative
    assert "backend/data/viewer-public-url.json" not in relative


def test_windows_real_quality_requires_managed_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment, "PROJECT_ROOT", tmp_path)
    service = tmp_path / ".capabilities/pic-style"
    service.mkdir(parents=True)
    (service / ".web-agent-deployment.json").write_text(
        '{"profile":"windows-nvidia-real"}', encoding="utf-8"
    )
    (service / ".web-agent-service.json").write_text(
        '{"worker_pid":999999}', encoding="utf-8"
    )
    monkeypatch.setattr(deployment, "process_running", lambda _pid: False)
    monkeypatch.setattr(
        deployment,
        "request_json",
        lambda *_args, **_kwargs: {
            "ready": True,
            "details": {"production_quality": True},
        },
    )

    state = deployment.model_state(
        {
            "PHOTO_STYLE_PROVIDER": "pic-style-http",
            "PHOTO_STYLE_SERVICE_URL": "http://127.0.0.1:18000",
            "PHOTO_STYLE_SERVICE_ROOT": ".capabilities/pic-style",
        }
    )

    assert state["production_quality"] is False
