"""
SQLite layer. One DB for everything: scanned files, scan jobs, progress
samples (for the rolling ETA), groups, and review state.

Design notes tied to acceptance criteria:
- Scenario 7 (resumable scans): `files` rows key on (root_id, path) and store
  size+mtime+status, so a restarted scan can skip unchanged, already-done
  files.
- Scenario 12/13/14/15 (progress+ETA persisted server-side): `scan_progress`
  keeps a single current-state row per scan plus `progress_samples`, a small
  rolling history table used to compute a stable rate instead of an
  instantaneous (jumpy) one.
- Scenario 16-19 (save/load session): `review_state` holds per-file marked/
  reviewed flags; session_io.py exports/imports this (plus settings) as
  portable JSON separate from this DB.
"""
import sqlite3
import time
import contextlib
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES roots(id),
    status TEXT NOT NULL DEFAULT 'running',   -- running | done | error | stopped
    rotation_pass INTEGER NOT NULL DEFAULT 0, -- Scenario 11 opt-in slow pass
    stop_requested INTEGER NOT NULL DEFAULT 0, -- set by the Stop button; scanner polls this
    started_at REAL NOT NULL,
    finished_at REAL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES roots(id),
    path TEXT NOT NULL,           -- absolute path
    rel_path TEXT NOT NULL,       -- relative to root, used for Trash mirroring
    filename TEXT,                -- basename(rel_path), stored so "group by filename"
                                   -- and pagination can be done in SQL (GROUP BY)
                                   -- instead of loading every row into Python
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    phash TEXT,                   -- 64-bit hash as hex string, EXIF-orientation-normalized
    phash_rot90 TEXT,             -- populated only when rotation_pass is enabled
    phash_rot180 TEXT,
    phash_rot270 TEXT,
    exif_date TEXT,               -- best-guess display date (EXIF, else file mtime), or NULL
    exif_date_raw TEXT,           -- ONLY set if the file actually has an EXIF capture date
    exif_datetime TEXT,           -- same as exif_date but with time-of-day, 'YYYY-MM-DD HH:MM:SS'
    exif_datetime_raw TEXT,       -- same as exif_date_raw but with time-of-day
    width INTEGER,
    height INTEGER,
    status TEXT NOT NULL DEFAULT 'pending', -- pending | done | error | trashed
    error_message TEXT,
    trashed_path TEXT,
    UNIQUE(root_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_root_status ON files(root_id, status);
CREATE INDEX IF NOT EXISTS idx_files_phash ON files(phash);
CREATE INDEX IF NOT EXISTS idx_files_exif_date ON files(root_id, exif_date);
-- NOTE: no index on `filename` here deliberately -- that column doesn't
-- exist yet on a pre-existing (upgraded) database at the point this script
-- runs, since CREATE TABLE IF NOT EXISTS is a no-op when the table already
-- exists. Its index is created in init_db() below, AFTER the migration
-- that adds the column -- see the "no such column: filename" bug this
-- caused when it lived here instead.

CREATE TABLE IF NOT EXISTS scan_progress (
    scan_id INTEGER PRIMARY KEY REFERENCES scans(id),
    total_files INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'discovering', -- discovering | hashing | clustering | rotation_pass | done
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS progress_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    ts REAL NOT NULL,
    processed_files INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_progress_samples_scan ON progress_samples(scan_id, ts);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER NOT NULL REFERENCES roots(id),
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    method TEXT NOT NULL DEFAULT 'content'  -- currently only 'content'; 'date' is computed on the fly
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id),
    file_id INTEGER NOT NULL REFERENCES files(id),
    PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_file ON group_members(file_id);

-- Per-file review state, independent of scan_id so it survives rescans of
-- the same root as long as the file row itself persists.
CREATE TABLE IF NOT EXISTS review_state (
    file_id INTEGER PRIMARY KEY REFERENCES files(id),
    marked_for_delete INTEGER NOT NULL DEFAULT 0,
    reviewed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

-- Arbitrary saved settings (group-by preference, rotation-pass toggle, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers (Flask) don't block the writer (scanner)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table, column, coltype):
    """Lightweight migration: add a column to an already-existing table if
    it's not there yet, so upgrading doesn't require dropping the DB.
    CREATE TABLE IF NOT EXISTS in SCHEMA only helps for brand-new installs."""
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _backfill_filename(conn):
    """One-time migration for installs from before the `filename` column
    existed: derive it from rel_path for any row that doesn't have it yet.
    Cheap after the first run since new/rescanned files get it set directly
    (see scanner.py), so this only ever touches genuinely old rows."""
    import os
    rows = conn.execute("SELECT id, rel_path FROM files WHERE filename IS NULL").fetchall()
    if not rows:
        return
    conn.executemany(
        "UPDATE files SET filename=? WHERE id=?",
        [(os.path.basename(r["rel_path"]), r["id"]) for r in rows],
    )


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "files", "exif_date_raw", "TEXT")
        _ensure_column(conn, "files", "exif_datetime", "TEXT")
        _ensure_column(conn, "files", "exif_datetime_raw", "TEXT")
        _ensure_column(conn, "scans", "stop_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "files", "filename", "TEXT")
        _backfill_filename(conn)
        # Must come AFTER the filename column migration above -- see the
        # comment by idx_files_root_status in SCHEMA for why this can't
        # just live in the static schema script.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_filename ON files(root_id, filename)")
        conn.commit()


def get_or_create_root(conn, path):
    row = conn.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO roots(path, created_at) VALUES (?, ?)", (path, time.time()))
    return cur.lastrowid


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")
