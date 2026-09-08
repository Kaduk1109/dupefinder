"""
Perceptual hashing.

We implement pHash (DCT-based) ourselves rather than depending on the
`imagehash` package, since it's not preinstalled and this environment has no
network access to pip-install it. The math is the standard pHash recipe:

  1. Apply EXIF orientation (Scenario 9: rotation-normalized fingerprinting —
     an EXIF-flag rotation must not change the hash).
  2. Convert to grayscale and downscale to (size * highfreq_factor)^2.
  3. Run a 2D DCT.
  4. Keep the top-left `size x size` low-frequency block (excluding the DC
     term), threshold against the median -> a `size*size`-bit hash.

pHash is inherently robust to resizing/recompression (Scenario 1, 8) because
it captures low-frequency structure, not pixel-exact data, and it degrades
gracefully across multiple resize generations since the low-frequency
content barely changes each time an image is scaled down/up.

Rotation via re-encoded pixels (Scenario 10/11) is NOT normalized by default
because there's no cheap way to detect "this image is really the same scene
rotated 90 degrees" without re-hashing rotated variants -- that's exactly
the opt-in slower pass, implemented via `rotated_variants()` below.
"""
import os
import numpy as np
from PIL import Image, ImageOps
from scipy.fft import dctn

from config import PHASH_SIZE, PHASH_HIGHFREQ_FACTOR

# HEIC/HEIF: if pillow-heif is installed, registering it makes PIL's own
# Image.open() understand .heic/.heif transparently -- no separate code path
# needed anywhere else in this file.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
except Exception:
    HEIF_SUPPORT = False

# DNG (and other camera raw formats): PIL cannot decode raw sensor data, so
# this goes through rawpy/libraw instead when available.
try:
    import rawpy
    RAW_SUPPORT = True
except Exception:
    RAW_SUPPORT = False

RAW_EXTENSIONS = {".dng"}


class UnreadableImageError(Exception):
    pass


def open_any_image(path):
    """Open any supported image -- including HEIC (via pillow-heif, if
    installed) and DNG (via rawpy, if installed) -- and return a plain PIL
    Image. Raises UnreadableImageError with a clear reason otherwise."""
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTENSIONS:
        if not RAW_SUPPORT:
            raise UnreadableImageError(
                f"{path}: DNG support requires the 'rawpy' package, which isn't installed in this image"
            )
        try:
            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8)
            return Image.fromarray(rgb)
        except Exception as e:
            raise UnreadableImageError(f"{path}: DNG decode failed ({e})") from e

    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception as e:
        hint = ""
        if ext in (".heic", ".heif") and not HEIF_SUPPORT:
            hint = " (HEIC support requires the 'pillow-heif' package, which isn't installed in this image)"
        raise UnreadableImageError(f"{path}: {e}{hint}") from e


def _load_normalized(path):
    """Open an image, apply EXIF orientation, return a PIL Image + (w, h)."""
    img = open_any_image(path)
    img = ImageOps.exif_transpose(img)  # bakes EXIF orientation into pixel order
    orig_size = img.size
    return img, orig_size


def phash_from_pil(img, size=PHASH_SIZE, highfreq_factor=PHASH_HIGHFREQ_FACTOR):
    """Compute a pHash for an already-loaded, already-oriented PIL image.
    Returns a hex string representing a size*size-bit hash."""
    img_side = size * highfreq_factor
    im = img.convert("L").resize((img_side, img_side), Image.LANCZOS)
    pixels = np.asarray(im, dtype=np.float64)
    dct = dctn(pixels, norm="ortho")
    low_freq = dct[:size, :size]
    # Exclude the DC term (top-left coefficient) when computing the median,
    # standard pHash practice -- the DC term just reflects overall brightness.
    flat = low_freq.flatten()
    median = np.median(flat[1:])
    bits = (flat > median)
    # Pack bits -> integer -> hex string, fixed width for consistent storage/comparison.
    bit_str = "".join("1" if b else "0" for b in bits)
    value = int(bit_str, 2)
    hexlen = (size * size + 3) // 4
    return format(value, f"0{hexlen}x")


def compute_file_hash(path):
    """Full pipeline for a file on disk: load, orient, hash, and also return
    basic metadata (post-orientation width/height, capture date/time).

    Four time-related fields are returned:
    - exif_datetime_raw / exif_date_raw: ONLY set if the file actually has an
      EXIF capture timestamp; None otherwise. Lets the review page show "no
      EXIF date" honestly rather than silently substituting the file date.
    - exif_datetime / exif_date: the best-guess display value -- the EXIF
      version if present, else derived from the file's mtime. exif_date is
      used for "group by date" (day-level, so grouping isn't split by the
      few seconds/minutes two derivative copies' mtimes might differ by).
    """
    img, _orig_size = _load_normalized(path)
    h = phash_from_pil(img)
    width, height = img.size
    exif_datetime_raw = extract_exif_datetime_raw(path)
    exif_datetime = exif_datetime_raw or _mtime_datetime(path)
    exif_date_raw = exif_datetime_raw[:10] if exif_datetime_raw else None
    exif_date = exif_datetime[:10] if exif_datetime else None
    return {
        "phash": h,
        "width": width,
        "height": height,
        "exif_date": exif_date,
        "exif_date_raw": exif_date_raw,
        "exif_datetime": exif_datetime,
        "exif_datetime_raw": exif_datetime_raw,
    }


def rotated_variants(path):
    """For the opt-in slow pass (Scenario 11): compute hashes of the 90/180/270
    degree rotations of a physically re-encoded image, so we can also match
    against duplicates that were rotated by re-saving pixels rather than by
    an EXIF flag."""
    img, _ = _load_normalized(path)
    variants = {}
    for degrees in (90, 180, 270):
        rotated = img.rotate(-degrees, expand=True)
        variants[degrees] = phash_from_pil(rotated)
    return variants


def hamming_distance(hex_a, hex_b):
    if hex_a is None or hex_b is None:
        return None
    int_a = int(hex_a, 16)
    int_b = int(hex_b, 16)
    return bin(int_a ^ int_b).count("1")


def extract_exif_datetime_raw(path):
    """Strictly EXIF DateTimeOriginal (or DateTime as fallback tag) ->
    'YYYY-MM-DD HH:MM:SS'. Returns None if the file has no such tag --
    deliberately does NOT fall back to file mtime, so the review page can
    distinguish "this photo's real capture time" from "we're guessing"."""
    import datetime
    try:
        img = Image.open(path)  # header-only read; safe even for formats
        exif = img.getexif()    # whose pixel data we can't fully decode
        # 0x9003 = DateTimeOriginal, 0x0132 = DateTime (fallback)
        raw = exif.get(0x9003) or exif.get(0x0132)
        if raw:
            # EXIF format: "YYYY:MM:DD HH:MM:SS"
            date_part, _, time_part = raw.partition(" ")
            date_part = date_part.replace(":", "-")
            dt_str = f"{date_part} {time_part}".strip()
            datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")  # validate
            return dt_str
    except Exception:
        pass
    return None


def extract_exif_date_raw(path):
    dt = extract_exif_datetime_raw(path)
    return dt[:10] if dt else None


def _mtime_datetime(path):
    import datetime
    try:
        mtime = os.path.getmtime(path)
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _mtime_date(path):
    dt = _mtime_datetime(path)
    return dt[:10] if dt else None


def extract_exif_date(path):
    """Best-guess display date: EXIF if present, else file mtime date."""
    return extract_exif_date_raw(path) or _mtime_date(path)
