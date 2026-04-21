"""Image preview rendering — pure transformation layer.

Downsizes a raw image and re-encodes as WebP.  No I/O, no storage, no DB.
Callers supply raw bytes; callers handle caching + HTTP concerns.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError


PREVIEW_MAX_EDGE_PX = 512
PREVIEW_WEBP_QUALITY = 75
PREVIEW_MEDIA_TYPE = "image/webp"


class PreviewRenderError(Exception):
    """Raised when the input bytes are not a decodable image."""


def generate_image_preview(raw_bytes: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(raw_bytes)) as image:
            if max(image.width, image.height) > PREVIEW_MAX_EDGE_PX:
                image.thumbnail(
                    (PREVIEW_MAX_EDGE_PX, PREVIEW_MAX_EDGE_PX),
                    Image.Resampling.LANCZOS,
                )
            has_alpha = "A" in image.getbands()
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = io.BytesIO()
            image.save(output, format="WEBP", quality=PREVIEW_WEBP_QUALITY, method=6)
            return output.getvalue()
    except UnidentifiedImageError as exc:
        raise PreviewRenderError("File is not a decodable image") from exc
