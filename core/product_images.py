"""Uploaded product images, saved under PRODUCT_IMAGES_DIR (used by the store and the FP28 admin)."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from . import config
from .config import ALLOWED_IMAGE_TYPES, MAX_PRODUCT_IMAGE_SIZE_BYTES


def save_product_image_file(slug: str, original_name: str, mime_type: str, content: bytes) -> tuple[str | None, str | None]:
    if mime_type not in ALLOWED_IMAGE_TYPES:
        return None, "unsupported image type (use JPEG, PNG, or WebP)"
    if len(content) < 1 or len(content) > MAX_PRODUCT_IMAGE_SIZE_BYTES:
        return None, "image must be between 1 byte and 10 MB"
    config.PRODUCT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(original_name).suffix.lower()
    if not suffix:
        suffix = ".jpg" if "jpeg" in mime_type else ".png"
    filename = f"{slug}-{int(datetime.now(UTC).timestamp())}{suffix}"
    (config.PRODUCT_IMAGES_DIR / filename).write_bytes(content)
    return filename, None
