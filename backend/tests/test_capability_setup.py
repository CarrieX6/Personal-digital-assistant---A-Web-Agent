from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.capability_setup as capability_setup_module
from backend.app.capability_setup import (
    CapabilityInstallRequest,
    CapabilitySetupService,
    CapabilitySetupStore,
    HostFacts,
    create_capability_setup_router,
)


def host(
    *,
    profile: str = "macos-apple-silicon",
    system: str = "Darwin",
    architecture: str = "arm64",
    docker: bool = False,
    is_spark: bool = False,
) -> HostFacts:
    return HostFacts(
        system=system,
        release="test",
        architecture=architecture,
        python_version="3.12.0",
        node_version="v22.13.0",
        profile=profile,
        profile_label=profile,
        gpu_name="GB10" if is_spark else "Apple Silicon 统一内存 GPU",
        docker_available=docker,
        memory_gb=64,
        disk_free_gb=200,
        is_dgx_spark=is_spark,
        validation_note="test fixture",
    )


def test_spark_plan_blocks_unverified_cuda_installers(tmp_path: Path) -> None:
    service = CapabilitySetupService(
        tmp_path,
        host=host(
            profile="linux-aarch64-dgx-spark",
            system="Linux",
            architecture="aarch64",
            docker=True,
            is_spark=True,
        ),
    )
    try:
        style = service.plan("photo-style-transfer")
        flux = service.plan("flux-gs")

        assert style.supported is False
        assert style.compatibility == "blocked"
        assert "ARM64" in (style.reason or "")
        assert service.plan("spatial-photo").supported is False
        assert service.plan("semantic-memory").supported is False
        assert flux.supported is False
        assert "SM 12.1" in (flux.reason or "")
    finally:
        service.close()


def test_host_contract_can_explain_runtime_repairs() -> None:
    dependencies = capability_setup_module._runtime_dependencies(
        system="Darwin",
        python_version="3.12.8",
        node_version=None,
    )
    by_id = {item.id: item for item in dependencies}

    assert by_id["python"].status == "ready"
    assert by_id["node"].status == "missing"
    assert "brew install node@22" in (by_id["node"].repair_command or "")
    assert by_id["node"].repair_steps


def test_keg_only_homebrew_node_can_be_detected(tmp_path: Path, monkeypatch) -> None:
    node = tmp_path / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("", encoding="utf-8")

    monkeypatch.setattr(capability_setup_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        capability_setup_module.shutil,
        "which",
        lambda command: "/opt/homebrew/bin/brew" if command == "brew" else None,
    )

    def fake_capture(command: list[str], timeout: float = 4) -> str | None:
        del timeout
        if command[1:] == ["--prefix", "node@22"]:
            return str(tmp_path)
        if command == [str(node), "--version"]:
            return "v22.23.2"
        return None

    monkeypatch.setattr(capability_setup_module, "_capture", fake_capture)

    assert (
        capability_setup_module._homebrew_binary_version(
            "node@22", "node", "--version"
        )
        == "v22.23.2"
    )


def test_model_license_and_explicit_confirmation_are_hard_gates(tmp_path: Path) -> None:
    service = CapabilitySetupService(tmp_path, host=host())
    try:
        try:
            service.start_install(
                "photo-style-transfer",
                CapabilityInstallRequest(confirm_install=True, accept_licenses=False),
            )
        except ValueError as exc:
            assert "许可证" in str(exc)
        else:
            raise AssertionError("license gate did not reject the install")

        try:
            service.start_install(
                "spatial-photo",
                CapabilityInstallRequest(confirm_install=False),
            )
        except ValueError as exc:
            assert "确认" in str(exc)
        else:
            raise AssertionError("confirmation gate did not reject the install")
    finally:
        service.close()


def test_ready_capability_cannot_be_installed_again(tmp_path: Path) -> None:
    service = CapabilitySetupService(tmp_path, host=host())
    try:
        try:
            service.start_install(
                "agent-core",
                CapabilityInstallRequest(confirm_install=True),
            )
        except ValueError as exc:
            assert "已经就绪" in str(exc)
        else:
            raise AssertionError("ready capability was installed again")
    finally:
        service.close()


def test_installations_are_serialized_across_capabilities(tmp_path: Path) -> None:
    service = CapabilitySetupService(tmp_path, host=host())
    try:
        service.store.create("spatial-photo")
        try:
            service.start_install(
                "semantic-memory",
                CapabilityInstallRequest(
                    confirm_install=True,
                    accept_licenses=True,
                ),
            )
        except ValueError as exc:
            assert "另一项" in str(exc)
        else:
            raise AssertionError("parallel model installation was accepted")
    finally:
        service.close()


def test_install_job_is_persistent_and_uses_allowlisted_recipe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(capability_setup_module, "PROJECT_ROOT", tmp_path)
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], on_log) -> None:
        commands.append(command)
        on_log("download token=private-value")
        lock = tmp_path / "backend/models/spatial/spatial-model-lock.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("{}\n", encoding="utf-8")

    service = CapabilitySetupService(
        tmp_path / "data",
        host=host(),
        command_runner=runner,
    )
    try:
        created = service.start_install(
            "spatial-photo",
            CapabilityInstallRequest(confirm_install=True, accept_licenses=True),
        )
        deadline = time.monotonic() + 3
        current = created
        while current.status not in {"ready", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            current = service.get_job(created.id)

        assert current.status == "ready"
        assert current.progress == 100
        assert "private-value" not in "\n".join(current.logs)
        assert len(commands) == 3
        assert commands[0][1:] == (
            "-m",
            "pip",
            "install",
            "-r",
            "backend/requirements-segmentation.txt",
        )
        assert commands[1][1:] == (
            "backend/scripts/prepare_spatial_models.py",
            "--download",
            "--verify",
        )
        assert commands[2][1:] == ("backend/scripts/smoke_spatial_models.py",)
        assert "SPATIAL_BIREFNET_LOCAL_FILES_ONLY=true" in (
            tmp_path / ".env"
        ).read_text(encoding="utf-8")
    finally:
        service.close()


def test_interrupted_install_is_recovered_as_failed(tmp_path: Path) -> None:
    database = tmp_path / "setup.sqlite3"
    store = CapabilitySetupStore(database)
    job = store.create("spatial-photo")
    store.update(job.id, status="installing", stage="download", progress=50)
    store.close()

    recovered = CapabilitySetupStore(database)
    try:
        current = recovered.get(job.id)
        assert current.status == "failed"
        assert current.error == "service_restarted"
        assert current.stage == "interrupted"
    finally:
        recovered.close()


def test_setup_api_is_local_owner_only(tmp_path: Path) -> None:
    service = CapabilitySetupService(tmp_path, host=host())
    app = FastAPI()
    app.include_router(create_capability_setup_router(service))
    try:
        local = TestClient(app)
        assert local.get("/api/setup/host").status_code == 200
        assert len(local.get("/api/setup/capabilities").json()) == 5

        remote = TestClient(app, client=("198.51.100.10", 50000))
        denied = remote.get("/api/setup/host")
        assert denied.status_code == 403
        assert "当前设备" in denied.json()["detail"]
    finally:
        service.close()
