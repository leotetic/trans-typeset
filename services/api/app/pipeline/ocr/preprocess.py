from __future__ import annotations

import hashlib
from pathlib import Path


def image_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preprocess_region_image(path: Path) -> tuple[Path, list[str]]:
    flags: list[str] = []
    try:
        from PIL import Image, ImageOps
    except Exception:
        return path, ["ocr_preprocess_pillow_unavailable"]

    try:
        image = Image.open(path)
    except Exception:
        return path, ["ocr_preprocess_open_failed"]

    try:
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"}:
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        if image.width < 900:
            scale = max(2, min(4, round(900 / max(image.width, 1))))
            image = image.resize(
                (image.width * scale, image.height * scale),
                resample=Image.Resampling.LANCZOS,
            )
            flags.append("ocr_image_upscaled")
        padded = ImageOps.expand(image, border=max(8, image.height // 12), fill="white")
        grayscale = ImageOps.grayscale(padded)
        processed = ImageOps.autocontrast(grayscale)
        output_path = path.with_name(f"{path.stem}.ocr.png")
        processed.save(output_path)
        return output_path, flags
    except Exception:
        return path, ["ocr_preprocess_failed"]
