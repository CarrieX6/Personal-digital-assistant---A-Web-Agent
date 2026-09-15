from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from PIL import Image

from backend.app.sdxl_style_provider import (
    NativeSDXLStyleProvider,
    accelerator_memory_mib,
    content_dimensions,
    harmonize_style_palette,
    lcm_scheduler_config,
    map_native_parameters,
    provider_gate_local_validation_status,
    resolve_torch_accelerator,
    scheduler_steps_for_effective_steps,
    style_layer_scale,
)
from backend.app.style_model_manifest import (
    load_model_manifest,
    verify_provider_gate,
)
from backend.app.style_transfer import (
    StyleParameters,
    build_style_provider_from_env,
    compose_style_negative_prompt,
    compose_style_prompt,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = PROJECT_ROOT / "backend" / "config" / "photo-style-models.json"
GATE_PATH = (
    PROJECT_ROOT / "backend" / "config" / "photo-style-provider-gate.json"
)


def test_native_model_manifest_and_upstream_gate_are_pinned() -> None:
    manifest = load_model_manifest(MANIFEST_PATH)

    assert manifest.provider == "sdxl_ip_adapter_8gb_v1"
    assert manifest.source_commit == "c8e0b641f7f334faf73167211b5f0a8033e3b1dc"
    assert manifest.source_model_manifest_fingerprint == (
        "4b42cfcbd6febc8c79a6970dcb3ff580b080d1a53aaaf4b908ab0a77c38a23d8"
    )
    assert manifest.base.id == "stabilityai/stable-diffusion-xl-base-1.0"
    assert manifest.ip_adapter.id == "h94/IP-Adapter"
    assert manifest.active_download_bytes == 10_561_842_988
    assert manifest.runtime_downloads_allowed is False
    assert manifest.runtime.local_files_only is True
    assert verify_provider_gate(manifest, GATE_PATH) == []
    assert provider_gate_local_validation_status(GATE_PATH) == (
        "engineering_smoke_passed"
    )
    assert provider_gate_local_validation_status(GATE_PATH, "cuda") == (
        "engineering_smoke_passed"
    )
    assert provider_gate_local_validation_status(GATE_PATH, "mps") == "pending"


def test_accelerator_resolution_keeps_cuda_and_mps_explicit() -> None:
    class Availability:
        def __init__(self, available: bool) -> None:
            self.available = available

        def is_available(self) -> bool:
            return self.available

    torch = SimpleNamespace(
        cuda=Availability(True),
        backends=SimpleNamespace(mps=Availability(True)),
    )

    assert resolve_torch_accelerator(torch, "auto") == "cuda"
    assert resolve_torch_accelerator(torch, "cuda") == "cuda"
    assert resolve_torch_accelerator(torch, "mps") == "mps"
    assert resolve_torch_accelerator(torch, "cpu") is None

    torch.cuda.available = False
    assert resolve_torch_accelerator(torch, "auto") == "mps"
    assert resolve_torch_accelerator(torch, "cuda") is None


def test_mps_memory_diagnostics_use_driver_allocation() -> None:
    torch = SimpleNamespace(
        mps=SimpleNamespace(
            driver_allocated_memory=lambda: 768 * 1024**2,
        )
    )

    assert accelerator_memory_mib(torch, "mps") == 768


def test_native_parameter_mapping_matches_accepted_8gb_path() -> None:
    mapped = map_native_parameters(StyleParameters(seed=1701))

    assert mapped["path"] == "base_sdxl_v1"
    assert mapped["steps"] == 20
    assert mapped["style_scale"] == 0.854
    assert mapped["denoise_strength"] == 0.418
    assert mapped["guidance_scale"] == 5.98
    scheduler_steps = scheduler_steps_for_effective_steps(
        int(mapped["steps"]),
        float(mapped["denoise_strength"]),
    )
    assert int(scheduler_steps * float(mapped["denoise_strength"])) == 20
    assert style_layer_scale(0.85) == {
        "down": {"block_2": [0.0, 0.85]},
        "up": {"block_0": [0.0, 0.85, 0.0]},
    }
    assert lcm_scheduler_config(
        {"name": "base", "skip_prk_steps": True}
    ) == {"name": "base"}


def test_native_content_dimensions_are_sdxl_safe() -> None:
    width, height = content_dimensions(
        1600,
        900,
        max_long_edge=768,
        pixel_budget=600_000,
    )

    assert (width, height) == (768, 432)
    assert width % 8 == height % 8 == 0
    assert width * height <= 600_000


def test_style_presets_add_medium_contract_without_copying_reference_content() -> None:
    parameters = StyleParameters(
        style_preset="cyberpunk",
        prompt="保留原图招牌位置",
        seed=1701,
    ).validated()

    mapped = map_native_parameters(parameters)
    prompt = compose_style_prompt(parameters)
    negative = compose_style_negative_prompt(parameters)

    assert mapped["style_scale"] == 0.1025
    assert mapped["denoise_strength"] == 0.488
    assert mapped["guidance_scale"] == 6.23
    assert mapped["palette_mix"] == 0.28
    assert "cyan and magenta neon" in prompt
    assert "never for its people, objects, or layout" in prompt
    assert "保留原图招牌位置" in prompt
    assert "subject copied from the style reference" in negative
    assert "bright natural daylight" in negative


def test_ink_palette_harmonization_is_deterministic_and_desaturates() -> None:
    source = Image.new("RGB", (80, 48), "#1b82d1")
    style = Image.new("RGB", (60, 60), "#b8aa98")
    parameters = StyleParameters(style_preset="ink_wash", seed=1701)

    first, mix = harmonize_style_palette(source, [style], parameters)
    second, _ = harmonize_style_palette(source, [style], parameters)

    assert first.size == source.size
    assert first.tobytes() == second.tobytes()
    assert mix == 0.2941
    red, green, blue = first.getpixel((0, 0))
    assert max(red, green, blue) - min(red, green, blue) < 80


def test_native_provider_preflight_does_not_load_or_download(tmp_path: Path) -> None:
    provider = NativeSDXLStyleProvider(
        model_root=tmp_path / "missing-models",
        manifest_path=MANIFEST_PATH,
        gate_path=GATE_PATH,
        lock_path=tmp_path / "missing-models" / "model-lock.json",
    )

    status = provider.status()

    assert status["loaded"] is False
    assert status["models_ready"] is False
    assert status["gate"] == "accepted_upstream"
    assert status["preflight_errors"]
    assert not (tmp_path / "missing-models").exists()
    provider.close()


def test_native_provider_loads_mps_without_cuda_cpu_offload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Availability:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeMPS:
        empty_calls = 0

        @classmethod
        def empty_cache(cls) -> None:
            cls.empty_calls += 1

    torch = ModuleType("torch")
    torch.float16 = "float16"
    torch.cuda = FakeCuda()
    torch.backends = SimpleNamespace(mps=Availability())
    torch.mps = FakeMPS()

    class FakePipeline:
        def __init__(self) -> None:
            self.scheduler = SimpleNamespace(config={"name": "base"})
            self.to_device = None
            self.attention_slicing = False
            self.cpu_offload = False

        def load_ip_adapter(self, *_args, **_kwargs) -> None:
            return None

        def set_ip_adapter_scale(self, _scale) -> None:
            return None

        def enable_vae_tiling(self) -> None:
            return None

        def enable_model_cpu_offload(self) -> None:
            self.cpu_offload = True

        def to(self, device: str):
            self.to_device = device
            return self

        def enable_attention_slicing(self) -> None:
            self.attention_slicing = True

        def maybe_free_model_hooks(self) -> None:
            return None

    pipeline = FakePipeline()

    class FakeAutoPipeline:
        @classmethod
        def from_pretrained(cls, *_args, **kwargs):
            assert kwargs["local_files_only"] is True
            assert kwargs["torch_dtype"] == "float16"
            return pipeline

    diffusers = ModuleType("diffusers")
    diffusers.AutoPipelineForImage2Image = FakeAutoPipeline
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setattr(
        "backend.app.sdxl_style_provider.verify_provider_gate",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "backend.app.sdxl_style_provider.verify_prepared_models",
        lambda *_args, **_kwargs: [],
    )

    provider = NativeSDXLStyleProvider(
        model_root=tmp_path,
        manifest_path=MANIFEST_PATH,
        gate_path=GATE_PATH,
        lock_path=tmp_path / "model-lock.json",
        unload_after_generation=False,
        accelerator="mps",
    )
    provider._load(lambda *_args: None)

    assert provider._resolved_accelerator == "mps"
    assert pipeline.to_device == "mps"
    assert pipeline.attention_slicing is True
    assert pipeline.cpu_offload is False
    provider.close()
    assert FakeMPS.empty_calls == 1


def test_environment_can_select_native_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PHOTO_STYLE_PROVIDER", "sdxl-local")
    monkeypatch.setenv("PHOTO_STYLE_MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("PHOTO_STYLE_MODEL_MANIFEST", str(MANIFEST_PATH))
    monkeypatch.setenv("PHOTO_STYLE_PROVIDER_GATE", str(GATE_PATH))
    monkeypatch.setenv(
        "PHOTO_STYLE_MODEL_LOCK",
        str(tmp_path / "models" / "model-lock.json"),
    )
    monkeypatch.setenv("PHOTO_STYLE_UNLOAD_AFTER_GENERATION", "true")
    monkeypatch.setenv("PHOTO_STYLE_ACCELERATOR", "mps")

    provider = build_style_provider_from_env()

    assert isinstance(provider, NativeSDXLStyleProvider)
    assert provider.name == "sdxl_ip_adapter_8gb_v1"
    assert provider.unload_after_generation is True
    assert provider.accelerator == "mps"
    assert provider.status()["loaded"] is False
    provider.close()


def test_native_provider_executes_diffusers_contract_without_weights(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeTensor:
        def __init__(self, value: float) -> None:
            self.value = value

        def detach(self):
            return self

        def to(self, **_kwargs):
            return self

        def unsqueeze(self, _dimension: int):
            return self

        def __mul__(self, value: float):
            return FakeTensor(self.value * value)

        def __add__(self, other):
            return FakeTensor(self.value + other.value)

    class FakeImageProjection:
        pass

    class FakeScheduler:
        config = {"name": "fake"}

        @classmethod
        def from_config(cls, _config):
            return cls()

    class FakeCuda:
        OutOfMemoryError = MemoryError

        @staticmethod
        def reset_peak_memory_stats() -> None:
            return None

        @staticmethod
        def max_memory_reserved() -> int:
            return 512 * 1024**2

        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def empty_cache() -> None:
            return None

    class FakeGenerator:
        def __init__(self, device: str) -> None:
            assert device == "cpu"

        def manual_seed(self, seed: int):
            assert seed == 1701
            return self

    class FakeTorch:
        cuda = FakeCuda()
        Generator = FakeGenerator

        @staticmethod
        def cat(tensors, dim: int):
            assert dim == 0
            return FakeTensor(sum(item.value for item in tensors))

    class FakePipeline:
        def __init__(self) -> None:
            self.scheduler = FakeScheduler()
            self._execution_device = "cpu"
            projection = SimpleNamespace(image_projection_layers=[FakeImageProjection()])
            self.unet = SimpleNamespace(encoder_hid_proj=projection)
            self.style_scale = None

        def set_ip_adapter_scale(self, scale) -> None:
            self.style_scale = scale

        def encode_image(self, *_args):
            return FakeTensor(1.0), FakeTensor(0.0)

        def maybe_free_model_hooks(self) -> None:
            return None

        def disable_lora(self) -> None:
            return None

        def __call__(self, **kwargs):
            step_count = int(
                kwargs["num_inference_steps"] * kwargs["strength"]
            )
            callback = kwargs["callback_on_step_end"]
            for index in range(step_count):
                callback(self, index, None, {})
            return SimpleNamespace(images=[kwargs["image"].copy()])

    diffusers = ModuleType("diffusers")
    diffusers.DPMSolverMultistepScheduler = FakeScheduler
    diffusers.LCMScheduler = FakeScheduler
    diffusers_models = ModuleType("diffusers.models")
    diffusers_embeddings = ModuleType("diffusers.models.embeddings")
    diffusers_embeddings.ImageProjection = FakeImageProjection
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "diffusers.models", diffusers_models)
    monkeypatch.setitem(
        sys.modules,
        "diffusers.models.embeddings",
        diffusers_embeddings,
    )

    provider = NativeSDXLStyleProvider(
        model_root=tmp_path,
        manifest_path=MANIFEST_PATH,
        gate_path=GATE_PATH,
        lock_path=tmp_path / "model-lock.json",
        unload_after_generation=False,
    )
    provider._pipeline = FakePipeline()
    provider._torch = FakeTorch()
    provider._manifest = load_model_manifest(MANIFEST_PATH)
    provider._base_scheduler_config = {"name": "fake"}
    provider._resolved_accelerator = "cuda"
    progress: list[int] = []

    result = provider.stylize(
        Image.new("RGB", (320, 224), "#b58a6d"),
        [
            Image.new("RGB", (128, 192), "#315a84"),
            Image.new("RGB", (192, 128), "#c49a44"),
        ],
        StyleParameters(seed=1701),
        lambda value, _stage, _message: progress.append(value),
    )

    assert result.provider_name == "sdxl_ip_adapter_8gb_v1"
    assert result.image.size == (320, 224)
    inference = result.metadata["inference_parameters"]
    assert inference["actual_steps"] == 20
    assert inference["style_reference_count"] == 2
    assert result.metadata["runtime"]["peak_reserved_vram_mib"] == 512
    assert result.metadata["runtime"]["accelerator"] == "cuda"
    assert result.metadata["runtime"]["unload_after_generation"] is False
    assert result.metadata["production_quality"] is False
    assert result.metadata["local_quality_validation"] == (
        "engineering_smoke_passed"
    )
    assert max(progress) == 86
    assert provider._pipeline is not None
    assert provider._torch is not None
    assert provider._unload_count == 0
    provider.close()
    assert provider._pipeline is None
    assert provider._torch is None
    assert provider._embedding_cache == {}
    assert provider._unload_count == 1


def test_native_provider_uses_disposable_worker_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "progress",
                                "value": 30,
                                "stage": "encoding_style",
                                "message": "working",
                            }
                        ),
                        json.dumps({"type": "result"}),
                    ]
                )
                + "\n"
            )
            self.return_code: int | None = None

        def wait(self, timeout=None) -> int:
            self.return_code = 0
            return 0

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.return_code = 0

        def kill(self) -> None:
            self.return_code = -9

    def fake_popen(command, **kwargs):
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        payload = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        with Image.open(payload["content_path"]) as source:
            source.save(payload["result_path"], "PNG")
        Path(payload["metadata_path"]).write_text(
            json.dumps({"runtime": {"model_load_seconds": 1.25}}),
            encoding="utf-8",
        )
        return FakeProcess()

    monkeypatch.setattr(
        "backend.app.sdxl_style_provider.subprocess.Popen",
        fake_popen,
    )
    provider = NativeSDXLStyleProvider(
        model_root=tmp_path,
        manifest_path=MANIFEST_PATH,
        gate_path=GATE_PATH,
        lock_path=tmp_path / "model-lock.json",
    )
    progress: list[int] = []

    result = provider.stylize(
        Image.new("RGB", (40, 32), "#b58a6d"),
        [Image.new("RGB", (24, 24), "#315a84")],
        StyleParameters(seed=1701),
        lambda value, _stage, _message: progress.append(value),
    )

    assert result.image.size == (40, 32)
    assert result.metadata["runtime"]["process_isolation"] is True
    assert result.metadata["runtime"]["unload_after_generation"] is True
    assert progress == [30]
    assert provider._pipeline is None
    assert provider.unload_after_generation is True
    assert provider._unload_count == 1
