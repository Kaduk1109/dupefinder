"""
Background scanner. Meant to be launched as its own OS process (see
app.py's /scan/start route, which uses subprocess.Popen) so that it keeps
running independently of the Flask app and of whether a browser tab is
open (Scenario 3, 15) -- Flask/the review app only ever *reads* progress
from SQLite; it never blocks on the scan itself.

Usage:
    python3 scanner.py /path/to/photos [--rotation-pass] [--scan-id N]

Run standalone for a first scan; for a truly hands-off deployment, wrap
this in a cron job or a Synology Task Scheduler "run in background" task.
"""
import argparse
import logging
import os
import sys
import time
import traceback

from config import (
    IMAGE_EXTENSIONS, EXCLUDED_DIRNAMES, MATCH_HAMMING_THRESHOLD,
    PROGRESS_WRITE_INTERVAL_SECONDS, PROGRESS_WRITE_INTERVAL_FILES, LOG_PATH,
)
import db
import hashing
import cluster
import notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scanner")


def discover_files(root_path):
    """Yield (abs_path, rel_path, size, mtime) for every image file under
    root_path, skipping the Trash subfolder and Synology's @eaDir thumbnail
    caches (wherever they appear in the tree -- Synology creates one per
    indexed folder, not just at the root)."""
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRNAMES]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                stat = os.stat(abs_path)
            except OSError:
                continue
            rel_path = os.path.relpath(abs_path, root_path)
            yield abs_path, rel_path, stat.st_size, stat.st_mtime


def sync_file_list_to_db(conn, root_id, root_path):
    """Discovery phase (feeds Scenario 7 resumability): upsert every
    discovered file into `files`. A file whose size+mtime match what's
    already stored AND whose status is already 'done' is left completely
    alone (no re-hash). Anything else (new file, changed file, previously
    errored file) is (re)marked 'pending' so the hashing phase picks it up.

    Also reconciles the other direction: any previously-known file that is
    NO LONGER found on disk (renamed, moved, or deleted outside the app) is
    marked status='missing' rather than left as a stale 'done' row. This
    matters for Scenario 18 -- without it, a saved session's rel_path match
    would silently "succeed" against a row whose file has actually moved,
    masking exactly the mismatch it's supposed to catch. Already-trashed
    files (status='trashed') are left alone; they're expected to be absent
    from their original path.

    Returns (total_files, already_done_count)."""
    seen_paths = set()
    total = 0
    already_done = 0
    for abs_path, rel_path, size, mtime in discover_files(root_path):
        seen_paths.add(abs_path)
        total += 1
        row = conn.execute(
            "SELECT id, size, mtime, status FROM files WHERE root_id=? AND path=?",
            (root_id, abs_path),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO files(root_id, path, rel_path, filename, size, mtime, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (root_id, abs_path, rel_path, os.path.basename(rel_path), size, mtime),
            )
        elif row["size"] == size and row["mtime"] == mtime and row["status"] == "done":
            already_done += 1
        else:
            conn.execute(
                "UPDATE files SET size=?, mtime=?, filename=?, status='pending', phash=NULL, "
                "phash_rot90=NULL, phash_rot180=NULL, phash_rot270=NULL, error_message=NULL "
                "WHERE id=?",
                (size, mtime, os.path.basename(rel_path), row["id"]),
            )

    known_rows = conn.execute(
        "SELECT id, path FROM files WHERE root_id=? AND status != 'trashed'", (root_id,)
    ).fetchall()
    for row in known_rows:
        if row["path"] not in seen_paths:
            conn.execute("UPDATE files SET status='missing' WHERE id=?", (row["id"],))

    conn.commit()
    return total, already_done


class ProgressWriter:
    """Batches progress writes so we're not hitting SQLite on every file
    (Scenario 12), while still writing a rolling history of samples so the
    review app can compute a stable ETA (Scenario 13/14) even after a
    restart/reconnect (Scenario 15, since it all lives in the DB)."""

    def __init__(self, conn, scan_id, total_files, phase):
        self.conn = conn
        self.scan_id = scan_id
        self.total_files = total_files
        self.phase = phase
        self.processed = 0
        self._last_write_ts = 0
        self._last_write_count = -1
        conn.execute(
            "INSERT INTO scan_progress(scan_id, total_files, processed_files, phase, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(scan_id) DO UPDATE SET total_files=?, processed_files=?, phase=?, updated_at=?",
            (self.scan_id, total_files, 0, phase, time.time(),
             total_files, 0, phase, time.time()),
        )
        conn.commit()

    def set_phase(self, phase):
        self.phase = phase
        self._flush(force=True)

    def increase_total(self, n):
        """Scenario fix: the opt-in rotation pass does real additional work
        (re-hashing rotation variants of every still-unmatched file) that
        wasn't counted in the original discovery total. Without this, the
        progress bar either stalls at 100% through the whole rotation phase
        or overshoots past 100% -- neither is an honest picture of what's
        left. Called once, right before the rotation phase starts, with the
        exact count of files it's about to process."""
        if n <= 0:
            return
        self.total_files += n
        self._flush(force=True)

    def tick(self, n=1):
        self.processed += n
        now = time.time()
        due_by_count = (self.processed - self._last_write_count) >= PROGRESS_WRITE_INTERVAL_FILES
        due_by_time = (now - self._last_write_ts) >= PROGRESS_WRITE_INTERVAL_SECONDS
        if due_by_count or due_by_time:
            self._flush()

    def _flush(self, force=False):
        now = time.time()
        self.conn.execute(
            "UPDATE scan_progress SET total_files=?, processed_files=?, phase=?, updated_at=? WHERE scan_id=?",
            (self.total_files, self.processed, self.phase, now, self.scan_id),
        )
        self.conn.execute(
            "INSERT INTO progress_samples(scan_id, ts, processed_files) VALUES (?, ?, ?)",
            (self.scan_id, now, self.processed),
        )
        self.conn.commit()
        self._last_write_ts = now
        self._last_write_count = self.processed

    def finish(self):
        self._flush(force=True)


class ScanStopped(Exception):
    """Raised internally when a Stop request is noticed mid-scan. Caught
    within run_scan -- never meant to propagate to the generic error
    handler, since stopping isn't a failure."""
    pass


def is_stop_requested(conn, scan_id):
    row = conn.execute("SELECT stop_requested FROM scans WHERE id=?", (scan_id,)).fetchone()
    return bool(row and row["stop_requested"])


def hash_pending_files(conn, root_id, scan_id, progress):
    rows = conn.execute(
        "SELECT id, path FROM files WHERE root_id=? AND status='pending'", (root_id,)
    ).fetchall()
    for row in rows:
        if is_stop_requested(conn, scan_id):
            raise ScanStopped()
        try:
            result = hashing.compute_file_hash(row["path"])
            conn.execute(
                "UPDATE files SET phash=?, width=?, height=?, exif_date=?, exif_date_raw=?, "
                "exif_datetime=?, exif_datetime_raw=?, status='done', error_message=NULL WHERE id=?",
                (result["phash"], result["width"], result["height"],
                 result["exif_date"], result["exif_date_raw"],
                 result["exif_datetime"], result["exif_datetime_raw"], row["id"]),
            )
        except hashing.UnreadableImageError as e:
            log.warning("Unreadable image, skipping: %s", e)
            conn.execute(
                "UPDATE files SET status='error', error_message=? WHERE id=?",
                (str(e), row["id"]),
            )
        except Exception as e:
            log.error("Unexpected error hashing %s: %s", row["path"], e)
            conn.execute(
                "UPDATE files SET status='error', error_message=? WHERE id=?",
                (str(e), row["id"]),
            )
        conn.commit()
        progress.tick()


def rehash_rotation_variants(conn, root_id, scan_id, progress, already_grouped_ids):
    """Scenario 11: only run when the user opts in. Computes rotated-variant
    hashes for files that aren't already in a multi-member group -- no point
    rotation-checking something that already matched normally.

    `already_grouped_ids` comes from the in-memory clustering result
    (cluster.cluster_files), NOT from the `groups`/`group_members` DB
    tables -- those are only written by write_groups() *after* this runs,
    so querying them here would still show last scan's (or no) groups and
    wrongly rotation-check everything."""
    rows = conn.execute(
        "SELECT id, path FROM files WHERE root_id=? AND status='done'", (root_id,)
    ).fetchall()
    rows = [r for r in rows if r["id"] not in already_grouped_ids]
    for row in rows:
        if is_stop_requested(conn, scan_id):
            raise ScanStopped()
        try:
            variants = hashing.rotated_variants(row["path"])
            conn.execute(
                "UPDATE files SET phash_rot90=?, phash_rot180=?, phash_rot270=? WHERE id=?",
                (variants[90], variants[180], variants[270], row["id"]),
            )
        except hashing.UnreadableImageError:
            pass
        conn.commit()
        progress.tick()


def write_groups(conn, root_id, scan_id, file_to_group):
    """Replace this root's groups with a fresh set reflecting file_to_group
    (dict[file_id] -> arbitrary group key)."""
    old_group_ids = [r["id"] for r in conn.execute("SELECT id FROM groups WHERE root_id=?", (root_id,)).fetchall()]
    for gid in old_group_ids:
        conn.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
    conn.execute("DELETE FROM groups WHERE root_id=?", (root_id,))

    key_to_group_id = {}
    for file_id, key in file_to_group.items():
        if key not in key_to_group_id:
            cur = conn.execute(
                "INSERT INTO groups(root_id, scan_id, method) VALUES (?, ?, 'content')",
                (root_id, scan_id),
            )
            key_to_group_id[key] = cur.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO group_members(group_id, file_id) VALUES (?, ?)",
            (key_to_group_id[key], file_id),
        )
    conn.commit()
    return len(key_to_group_id)


def run_scan(root_path, rotation_pass=False, existing_scan_id=None):
    root_path = os.path.abspath(root_path)
    if not os.path.isdir(root_path):
        raise SystemExit(f"Not a directory: {root_path}")

    db.init_db()
    with db.get_conn() as conn:
        root_id = db.get_or_create_root(conn, root_path)
        if existing_scan_id:
            scan_id = existing_scan_id
        else:
            cur = conn.execute(
                "INSERT INTO scans(root_id, status, rotation_pass, started_at) VALUES (?, 'running', ?, ?)",
                (root_id, int(rotation_pass), time.time()),
            )
            scan_id = cur.lastrowid
        conn.commit()

    try:
        with db.get_conn() as conn:
            log.info("Discovering files under %s", root_path)
            total, already_done = sync_file_list_to_db(conn, root_id, root_path)
            log.info("Discovered %d files (%d already up to date from a prior run)", total, already_done)
            progress = ProgressWriter(conn, scan_id, total, phase="hashing")
            progress.tick(already_done)  # count resumed files immediately (Scenario 7)

            stopped = False
            try:
                hash_pending_files(conn, root_id, scan_id, progress)
            except ScanStopped:
                stopped = True

            progress.set_phase("clustering")
            file_rows = conn.execute(
                "SELECT id, phash FROM files WHERE root_id=? AND status='done'", (root_id,)
            ).fetchall()
            file_to_group = cluster.cluster_files(file_rows, MATCH_HAMMING_THRESHOLD)

            if rotation_pass and not stopped:
                already_grouped_ids = set(file_to_group.keys())
                all_done_ids = {r["id"] for r in file_rows}
                pending_rotation_count = len(all_done_ids - already_grouped_ids)
                progress.increase_total(pending_rotation_count)
                progress.set_phase("rotation_pass")
                try:
                    rehash_rotation_variants(conn, root_id, scan_id, progress, already_grouped_ids)
                    all_rows = conn.execute(
                        "SELECT id, phash, phash_rot90, phash_rot180, phash_rot270 "
                        "FROM files WHERE root_id=? AND status='done'", (root_id,)
                    ).fetchall()
                    file_to_group = cluster.merge_rotation_matches(all_rows, file_to_group, MATCH_HAMMING_THRESHOLD)
                except ScanStopped:
                    stopped = True

            # Even on a stop, cluster/write groups from whatever's been
            # hashed so far -- partial results are still worth reviewing,
            # and this is what makes "Resume" meaningfully continue rather
            # than throwing away progress on the grouping side too.
            n_groups = write_groups(conn, root_id, scan_id, file_to_group)
            final_status = "stopped" if stopped else "done"
            progress.set_phase(final_status)
            progress.finish()

            conn.execute(
                "UPDATE scans SET status=?, finished_at=? WHERE id=?",
                (final_status, time.time(), scan_id),
            )
            conn.commit()

            if stopped:
                log.info("Scan stopped by request: %d/%d files processed, %d duplicate groups so far",
                          progress.processed, progress.total_files, n_groups)
            else:
                log.info("Scan complete: %d files, %d duplicate groups", total, n_groups)

        if stopped:
            notify.scan_stopped(root_path, progress.processed, progress.total_files)
        else:
            notify.scan_complete(root_path, total, n_groups)

    except Exception as e:
        log.error("Scan failed: %s\n%s", e, traceback.format_exc())
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE scans SET status='error', error_message=?, finished_at=? WHERE id=?",
                (str(e), time.time(), scan_id),
            )
        notify.scan_failed(root_path, str(e))
        raise


def main():
    parser = argparse.ArgumentParser(description="Scan a folder for duplicate images.")
    parser.add_argument("folder", help="Folder to scan")
    parser.add_argument("--rotation-pass", action="store_true",
                         help="Opt-in slower pass matching pixel-re-encoded rotations (Scenario 11)")
    parser.add_argument("--scan-id", type=int, default=None,
                         help="Resume/continue an existing scan row instead of creating a new one")
    args = parser.parse_args()
    run_scan(args.folder, rotation_pass=args.rotation_pass, existing_scan_id=args.scan_id)


if __name__ == "__main__":
    main()
