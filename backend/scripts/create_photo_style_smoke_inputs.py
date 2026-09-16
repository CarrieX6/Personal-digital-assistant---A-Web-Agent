from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build_inputs(output: Path) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    content = Image.new("RGB", (512, 384), "#e7efe9")
    draw = ImageDraw.Draw(content)
    draw.rounded_rectangle((86, 54, 426, 330), radius=54, fill="#f8f5eb", outline="#315c4c", width=8)
    draw.ellipse((176, 92, 336, 252), fill="#d7924d", outline="#774827", width=6)
    draw.polygon([(205, 118), (230, 66), (252, 124)], fill="#d7924d", outline="#774827")
    draw.polygon([(286, 124), (310, 66), (332, 119)], fill="#d7924d", outline="#774827")
    draw.ellipse((220, 150, 238, 168), fill="#26352f")
    draw.ellipse((278, 150, 296, 168), fill="#26352f")
    draw.arc((239, 170, 279, 208), 10, 170, fill="#69412e", width=4)

    style = Image.new("RGB", (512, 512), "#172b3d")
    style_draw = ImageDraw.Draw(style)
    palette = ("#fe7f2d", "#fcca46", "#a1c181", "#619b8a", "#233d4d")
    for index, color in enumerate(palette):
        offset = index * 82 - 80
        style_draw.polygon(
            [(0, offset + 120), (512, offset), (512, offset + 105), (0, offset + 225)],
            fill=color,
        )
    for x in range(40, 512, 96):
        style_draw.ellipse((x, 350, x + 54, 404), outline="#fff4d6", width=8)

    content_path = output / "content.png"
    style_path = output / "style.png"
    content.save(content_path, "PNG")
    style.save(style_path, "PNG")
    return content_path, style_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Create non-personal SDXL smoke-test inputs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in build_inputs(args.output):
        print(path)


if __name__ == "__main__":
    main()
