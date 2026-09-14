from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from backend.app.assets import DepthAnythingV2Estimator
from backend.app.spatial_segmentation import BiRefNetForegroundSegmenter


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = PROJECT_ROOT / "backend/models/spatial"


def progress(_: int, __: str, ___: str) -> None:
    return


def synthetic_image() -> Image.Image:
    image = Image.new("RGB", (192, 128), (224, 229, 232))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 82, 191, 127), fill=(85, 102, 91))
    draw.ellipse((58, 22, 138, 112), fill=(202, 134, 78))
    draw.ellipse((70, 38, 86, 54), fill=(32, 37, 39))
    draw.ellipse((112, 38, 128, 54), fill=(32, 37, 39))
    return image


def main() -> int:
    image = synthetic_image()
    depth = DepthAnythingV2Estimator(
        str(MODEL_ROOT / "depth-anything-v2-small")
    ).estimate(image, progress)
    if depth.size != image.size or depth.getextrema() == (0, 0):
        raise RuntimeError("Depth Anything V2 smoke test returned an invalid depth map")

    mask_result = BiRefNetForegroundSegmenter(
        str(MODEL_ROOT / "birefnet"),
        image_size=256,
        local_files_only=True,
    ).segment(image, depth, progress)
    mask_extrema = mask_result.mask.getextrema()
    if mask_result.mask.size != image.size or mask_extrema == (0, 0):
        raise RuntimeError("BiRefNet smoke test returned an invalid foreground mask")

    print(
        json.dumps(
            {
                "status": "passed",
                "offline": True,
                "depth_size": list(depth.size),
                "depth_range": list(depth.getextrema()),
                "mask_size": list(mask_result.mask.size),
                "mask_range": list(mask_extrema),
                "segmenter": mask_result.model_name,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
