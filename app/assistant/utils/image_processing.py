from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProcessedImage:
    output_path: str
    original_width: int
    original_height: int
    output_width: int
    output_height: int
    output_format: str


def prepare_chat_image(
    input_path: str,
    output_dir: str,
    *,
    max_long_side: int = 500,
    downscale_divisor: int = 4,
    output_format: str = "PNG",
) -> ProcessedImage:
    """
    Load an image from disk, downscale and compress it for LLM vision calls.

    Rules:
    - Never upscale.
    - Default: divide dimensions by `downscale_divisor`.
    - Also cap the long side to `max_long_side` after the divisor step.
    - Save to `output_dir` with a UUID name.
    """
    try:
        from PIL import Image
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "Pillow is required for image processing. Install it (pip install pillow)."
        ) from e

    if not input_path:
        raise ValueError("input_path is required")
    if not os.path.exists(input_path):
        raise FileNotFoundError(input_path)

    os.makedirs(output_dir, exist_ok=True)

    with Image.open(input_path) as im:
        im.load()
        original_width, original_height = im.size

        # Some formats load as "P" or "RGBA"; keep alpha if present for PNG output.
        if output_format.upper() == "PNG":
            if im.mode not in ("RGB", "RGBA"):
                # Convert paletted/L/etc.
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        else:
            # Safer default for non-PNG outputs.
            if im.mode != "RGB":
                im = im.convert("RGB")

        # Step 1: divisor downscale (never upscale)
        divisor = max(1, int(downscale_divisor))
        new_w = max(1, original_width // divisor)
        new_h = max(1, original_height // divisor)

        # Step 2: cap long side (never upscale)
        long_side = max(new_w, new_h)
        if max_long_side and long_side > max_long_side:
            scale = float(max_long_side) / float(long_side)
            new_w = max(1, int(new_w * scale))
            new_h = max(1, int(new_h * scale))

        # Resize only if we're actually shrinking
        if new_w < original_width or new_h < original_height:
            im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
        else:
            new_w, new_h = original_width, original_height

        out_name = f"{uuid.uuid4().hex}.png" if output_format.upper() == "PNG" else f"{uuid.uuid4().hex}.{output_format.lower()}"
        output_path = os.path.join(output_dir, out_name)

        save_kwargs = {}
        if output_format.upper() == "PNG":
            save_kwargs = {"optimize": True}

        im.save(output_path, format=output_format, **save_kwargs)

    return ProcessedImage(
        output_path=output_path,
        original_width=original_width,
        original_height=original_height,
        output_width=new_w,
        output_height=new_h,
        output_format=output_format.upper(),
    )

