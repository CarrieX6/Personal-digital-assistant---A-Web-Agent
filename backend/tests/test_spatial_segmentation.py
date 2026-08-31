from __future__ import annotations

import numpy as np
from PIL import Image

from backend.app.assets import build_layered_scene
from backend.app.spatial_segmentation import (
    AutoForegroundSegmenter,
    DepthThresholdForegroundSegmenter,
    ForegroundMaskResult,
)


def _progress(_value: int, _stage: str, _message: str) -> None:
    return None


def test_depth_fallback_is_explicitly_marked_low_confidence() -> None:
    image = Image.new("RGB", (120, 80), "#8c725c")
    depth = Image.linear_gradient("L").resize(image.size)

    result = DepthThresholdForegroundSegmenter().segment(
        image,
        depth,
        _progress,
    )

    assert result.mask.size == image.size
    assert result.quality_score <= 0.52
    assert "semantic_segmenter_unavailable" in result.warnings


def test_semantic_mask_keeps_subject_whole_even_when_depth_varies() -> None:
    width, height = 160, 100
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:, :] = (35, 72, 90)
    pixels[20:85, 45:125] = (208, 142, 78)
    image = Image.fromarray(pixels, "RGB")
    depth_values = np.tile(np.linspace(40, 220, width, dtype=np.uint8), (height, 1))
    depth = Image.fromarray(depth_values, "L")
    semantic_mask = Image.new("L", image.size, 0)
    semantic_mask.paste(255, (45, 20, 125, 85))

    _, foreground = build_layered_scene(image, depth, semantic_mask)
    alpha = np.asarray(foreground.getchannel("A"))

    assert alpha[50, 50] >= 220
    assert alpha[50, 120] >= 220
    assert alpha[10, 10] == 0


def test_auto_segmenter_uses_birefnet_before_depth_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPATIAL_FOREGROUND_SEGMENTER", "auto")
    image = Image.new("RGB", (32, 24), "#8c725c")
    depth = Image.linear_gradient("L").resize(image.size)

    class UnsupportedVision:
        @staticmethod
        def supported() -> bool:
            return False

    class FakeBiRefNet:
        def segment(self, image, depth, progress):
            del depth
            progress(66, "segmenting", "fake birefnet")
            return ForegroundMaskResult(
                mask=Image.new("L", image.size, 255),
                model_name="fake-birefnet",
                quality_score=0.91,
            )

    segmenter = AutoForegroundSegmenter(tmp_path)
    segmenter.native = UnsupportedVision()
    segmenter.birefnet = FakeBiRefNet()

    result = segmenter.segment(image, depth, _progress)

    assert result.model_name == "fake-birefnet"
    assert result.quality_score == 0.91


def test_auto_segmenter_falls_back_to_depth_when_birefnet_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPATIAL_FOREGROUND_SEGMENTER", "auto")
    image = Image.new("RGB", (32, 24), "#8c725c")
    depth = Image.linear_gradient("L").resize(image.size)

    class UnsupportedVision:
        @staticmethod
        def supported() -> bool:
            return False

    class FailingBiRefNet:
        def segment(self, image, depth, progress):
            del image, depth, progress
            raise RuntimeError("missing local weights")

    segmenter = AutoForegroundSegmenter(tmp_path)
    segmenter.native = UnsupportedVision()
    segmenter.birefnet = FailingBiRefNet()

    result = segmenter.segment(image, depth, _progress)

    assert result.model_name == "深度蒙版降级 v2"
    assert "semantic_segmenter_unavailable" in result.warnings
