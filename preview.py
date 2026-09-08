"""
Serves browser-displayable JPEG previews for the review page, regardless of
the original file's format (including HEIC/DNG) or orientation. Two sizes
are produced: a small one for the hover-preview panel, and a larger one for
the double-click "open in a new tab" view.

Converting on the fly means the browser never has to understand HEIC or DNG
itself -- it only ever sees plain JPEGs from this module.

Results are cached to disk (keyed by file id + mtime, so a rescanned/changed
file gets a fresh preview automatically) since decoding a raw DNG in
particular is too slow to redo on every mouse hover.
"""
import io
import os

from PIL import Image, ImageDraw, ImageOps

import config
import hashing


def _cache_path(file_id, mtime, suffix):
    return os.path.join(config.PREVIEW_CACHE_DIR, f"{file_id}_{int(mtime)}_{suffix}.jpg")


def _placeholder_bytes(max_dim, reason="Preview unavailable"):
    w, h = max_dim, int(max_dim * 0.66)
    img = Image.new("RGB", (w, h), (235, 235, 238))
    draw = ImageDraw.Draw(img)
    draw.text((14, 14), reason, fill=(120, 122, 128))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def get_preview_bytes(path, file_id, mtime, max_dim, suffix):
    """Returns JPEG bytes for the given file, downscaled to fit within
    max_dim x max_dim, using a disk cache. Never raises -- an unreadable or
    unsupported file gets a clear placeholder image instead, since this is
    used for a hover panel where a hard error would be disruptive."""
    cache_path = _cache_path(file_id, mtime, suffix)
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    try:
        img = hashing.open_any_image(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        data = buf.getvalue()
    except hashing.UnreadableImageError:
        data = _placeholder_bytes(max_dim, "Preview unavailable for this file")

    try:
        os.makedirs(config.PREVIEW_CACHE_DIR, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(data)
    except OSError:
        pass  # cache is a nice-to-have; serve the bytes either way

    return data
