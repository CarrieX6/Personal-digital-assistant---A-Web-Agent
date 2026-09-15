from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.sdxl_style_provider import NativeSDXLStyleProvider  # noqa: E402
from backend.app.style_transfer import StyleParameters  # noqa: E402


DEFAULT_MANIFEST = PROJECT_ROOT / "backend" / "config" / "photo-style-models.json"
DEFAULT_GATE = PROJECT_ROOT / "backend" / "config" / "photo-style-provider-gate.json"
DEFAULT_ROOT = PROJECT_ROOT / "backend" / "models" / "photo-style"


def _load_image(path: Path) -> Image.Image:
    with Image.open(path) as source:
        source.load()
        return ImageOps.exif_transpose(source).convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one local-only SDXL + IP-Adapter style generation."
    )
    parser.add_argument("--content", type=Path, required=True)
    parser.add_argument(
        "--style",
        type=Path,
        action="append",
        required=True,
        help="Repeat one to three times.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt", default="")
    parser.add_argument(
        "--mode",
        choices=("preserve_layout", "recompose"),
        default="preserve_layout",
    )
    parser.add_argument(
        "--quality",
        choices=("preview", "standard", "high"),
        default="standard",
    )
    parser.add_argument(
        "--style-preset",
        choices=(
            "auto",
            "ink_wash",
            "cyberpunk",
            "oil_painting",
            "post_impressionist",
            "watercolor",
            "anime",
            "cinematic",
        ),
        default="auto",
    )
    parser.add_argument("--style-strength", type=float, default=0.7)
    parser.add_argument("--content-strength", type=float, default=0.8)
    parser.add_argument("--detail-strength", type=float, default=0.7)
    parser.add_argument("--seed", type=int, default=1701)
    parser.add_argument("--model-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument("--model-lock", type=Path)
    parser.add_argument("--enable-lcm-preview", action="store_true")
    parser.add_argument("--verify-model-hashes", action="store_true")
    args = parser.parse_args()
    if not 1 <= len(args.style) <= 3:
        raise SystemExit("Provide one to three --style images.")

    provider = NativeSDXLStyleProvider(
        model_root=args.model_root,
        manifest_path=args.manifest,
        gate_path=args.gate,
        lock_path=args.model_lock or args.model_root / "model-lock.json",
        lcm_preview_enabled=args.enable_lcm_preview,
        verify_hashes=args.verify_model_hashes,
    )
    try:
        status = provider.status()
        if status["ready"] is not True:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            raise SystemExit(
                "SDXL preflight failed; install GPU dependencies and prepare models first."
            )
        result = provider.stylize(
            _load_image(args.content),
            [_load_image(path) for path in args.style],
            StyleParameters(
                mode=args.mode,
                quality=args.quality,
                style_preset=args.style_preset,
                style_strength=args.style_strength,
                content_strength=args.content_strength,
                detail_strength=args.detail_strength,
                prompt=args.prompt,
                seed=args.seed,
            ).validated(),
            lambda value, stage, message: print(
                f"progress={value} stage={stage} message={message}",
                flush=True,
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.image.save(args.output, "PNG", optimize=True)
        report_path = args.output.with_suffix(".json")
        report_path.write_text(
            json.dumps(
                {
                    "provider": result.provider_name,
                    "model": result.model_name,
                    "parameters": {
                        "mode": args.mode,
                        "quality": args.quality,
                        "style_preset": args.style_preset,
                        "style_strength": args.style_strength,
                        "content_strength": args.content_strength,
                        "detail_strength": args.detail_strength,
                        "prompt": args.prompt,
                        "seed": args.seed,
                    },
                    "provider_metadata": result.metadata,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.output} and {report_path}")
    finally:
        provider.close()


if __name__ == "__main__":
    main()
