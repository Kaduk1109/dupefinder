"""
Scenarios 16-19: save/load a session (settings + review progress + marked
files) as a plain, portable JSON file, kept deliberately separate from the
internal SQLite DB (Scenario 19) so it can be inspected/versioned/moved by
hand.

This is intentionally a *manual checkpoint* of DB state, not a full
NAS-to-NAS DB snapshot: it records enough per-file identity (root path +
relative path + the file's own pHash) to re-associate marks with rows in a
DB that has since been rescanned, added to, or partially changed -- but it
does not try to recreate scan history, groups, or hashes wholesale. If you
later need a full portable snapshot (e.g. to move to a brand-new NAS/DB),
that would additionally dump/restore the `files`/`groups` tables themselves;
flagged here as a possible v2 rather than built by default, since the
common case is "I'm resuming review on the same NAS."

Reconciliation on import (Scenario 18): a saved entry is matched first by
(root_path, rel_path); if that row is gone (moved/deleted) but a file with
the same pHash exists under the same root, we fall back to matching by
content hash instead of giving up on that entry. Anything that still can't
be matched is reported back as a warning rather than raised as an error, and
everything that DID match is still applied.
"""
import json
import time

import db

SESSION_FORMAT_VERSION = 1


def export_session(root_path, extra_settings=None):
    with db.get_conn() as conn:
        root = conn.execute("SELECT id FROM roots WHERE path=?", (root_path,)).fetchone()
        if root is None:
            raise ValueError(f"No scanned root found for {root_path}")
        root_id = root["id"]

        settings_rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in settings_rows}
        if extra_settings:
            settings.update(extra_settings)

        rows = conn.execute(
            """
            SELECT f.rel_path, f.phash, f.status,
                   COALESCE(rs.marked_for_delete, 0) AS marked_for_delete,
                   COALESCE(rs.reviewed, 0) AS reviewed
            FROM files f
            LEFT JOIN review_state rs ON rs.file_id = f.id
            WHERE f.root_id = ?
              AND (rs.marked_for_delete = 1 OR rs.reviewed = 1)
            """,
            (root_id,),
        ).fetchall()

        files_payload = [
            {
                "rel_path": r["rel_path"],
                "phash": r["phash"],
                "marked_for_delete": bool(r["marked_for_delete"]),
                "reviewed": bool(r["reviewed"]),
            }
            for r in rows
        ]

    return {
        "format_version": SESSION_FORMAT_VERSION,
        "exported_at": time.time(),
        "root_path": root_path,
        "settings": settings,
        "files": files_payload,
    }


def export_session_to_file(root_path, out_path, extra_settings=None):
    payload = export_session(root_path, extra_settings=extra_settings)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def import_session(payload):
    """Applies a previously-exported payload against the current DB.
    Returns {"applied": int, "skipped": int, "warnings": [str, ...]}."""
    warnings = []
    applied = 0
    skipped = 0

    root_path = payload.get("root_path")
    with db.get_conn() as conn:
        root = conn.execute("SELECT id FROM roots WHERE path=?", (root_path,)).fetchone()
        if root is None:
            warnings.append(
                f"Root '{root_path}' from the save file has never been scanned here -- "
                f"nothing could be applied. Scan it first, then reload this file."
            )
            return {"applied": 0, "skipped": len(payload.get("files", [])), "warnings": warnings}
        root_id = root["id"]

        for key, value in payload.get("settings", {}).items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

        for entry in payload.get("files", []):
            # A rel_path match only counts if that row is still actually
            # present at that path (status != 'missing'/'trashed' as tracked
            # by the scanner's discovery reconciliation) -- otherwise a
            # renamed/moved file would silently "match" its old, stale row.
            row = conn.execute(
                "SELECT id FROM files WHERE root_id=? AND rel_path=? AND status NOT IN ('missing')",
                (root_id, entry.get("rel_path")),
            ).fetchone()

            if row is None and entry.get("phash"):
                # Scenario 18: the exact path is gone (moved/renamed/rescanned) --
                # fall back to matching on content hash before giving up.
                row = conn.execute(
                    "SELECT id FROM files WHERE root_id=? AND phash=? AND status NOT IN ('missing')",
                    (root_id, entry["phash"]),
                ).fetchone()
                if row is not None:
                    warnings.append(
                        f"'{entry.get('rel_path')}' moved or was renamed; "
                        f"matched by content hash instead."
                    )

            if row is None:
                warnings.append(f"'{entry.get('rel_path')}' no longer found in the scanned data; skipped.")
                skipped += 1
                continue

            conn.execute(
                "INSERT INTO review_state(file_id, marked_for_delete, reviewed, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET marked_for_delete=excluded.marked_for_delete, "
                "reviewed=excluded.reviewed, updated_at=excluded.updated_at",
                (row["id"], int(entry.get("marked_for_delete", False)), int(entry.get("reviewed", False)), time.time()),
            )
            applied += 1

    return {"applied": applied, "skipped": skipped, "warnings": warnings}
