"""
Central configuration for the Duplicate Image Finder.

Keep this file simple and editable — on the NAS you'll typically only ever
touch DB_PATH, TRASH_DIRNAME, and the SMTP_* settings.
"""
import os

# Where the SQLite database lives. Put it somewhere durable (not inside a
# scanned folder, so it never gets treated as "just another file").
APP_DATA_DIR = os.environ.get("DUPEFINDER_DATA_DIR", os.path.join(os.path.dirname(__file__), "app_data"))
DB_PATH = os.path.join(APP_DATA_DIR, "dupefinder.db")

# Name of the trash subfolder created under each scanned root (Scenario 6).
TRASH_DIRNAME = os.environ.get("DUPEFINDER_TRASH_DIRNAME", "_DupeFinder_Trash")

# Directories to never descend into. @eaDir is Synology's own thumbnail/index
# cache -- every indexed folder gets one, full of derivative thumbnail files
# (e.g. @eaDir/IMG_4283.jpg/SYNOPHOTO_THUMB_B.jpg) that would otherwise show
# up as "duplicates" of the real photo.
EXCLUDED_DIRNAMES = {TRASH_DIRNAME, "@eaDir"}

# File extensions considered "images" for scanning purposes. HEIC/DNG decode
# support depends on the pillow-heif / rawpy packages installed at build
# time (see requirements.txt) -- if those aren't available, files of that
# type are skipped with a clear error rather than crashing the scan.
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".dng",
}

# Perceptual-hash / clustering tuning.
PHASH_SIZE = 8          # -> 64-bit hash (8x8 DCT low-frequency coefficients)
PHASH_HIGHFREQ_FACTOR = 4  # standard pHash oversampling factor before DCT
MATCH_HAMMING_THRESHOLD = 8   # <=8 bits different out of 64 => considered a duplicate/near-duplicate
                              # (loosened a bit further below for heavily-resized derivatives)

# Progress reporting cadence (Scenario 12/15): write progress to DB at most
# this often, so we don't hammer SQLite on every single file.
PROGRESS_WRITE_INTERVAL_SECONDS = 2.0
PROGRESS_WRITE_INTERVAL_FILES = 25

# Rolling-window ETA (Scenario 13/14): only trust the rate once we have at
# least this many samples, and compute the rate from a window this wide.
ETA_MIN_SAMPLES = 5
ETA_WINDOW_SECONDS = 120

# Optional email notification (Scenario 4). Leave SMTP_HOST empty to fall
# back to desktop notification + log file only.
SMTP_HOST = os.environ.get("DUPEFINDER_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("DUPEFINDER_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("DUPEFINDER_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("DUPEFINDER_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("DUPEFINDER_SMTP_FROM", SMTP_USER)
NOTIFY_EMAIL_TO = os.environ.get("DUPEFINDER_NOTIFY_EMAIL", "")

LOG_PATH = os.path.join(APP_DATA_DIR, "scanner.log")

# On-demand preview images for the review page (hover panel + double-click
# full view). Cached to disk since decoding a raw DNG especially is not
# cheap and the same file may be hovered repeatedly during a review session.
PREVIEW_CACHE_DIR = os.path.join(APP_DATA_DIR, "preview_cache")
THUMB_MAX_DIM = 640    # hover-preview panel size
FULL_MAX_DIM = 3000    # double-click "open in new tab" size cap

os.makedirs(APP_DATA_DIR, exist_ok=True)
os.makedirs(PREVIEW_CACHE_DIR, exist_ok=True)
