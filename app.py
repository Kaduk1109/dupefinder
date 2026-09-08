"""
Flask review app (Scenario 5, 12-15, 16-19).

Deliberately thin: this process never scans images itself. It only reads
from SQLite (fast, even with 120k+ rows) and launches/reads the status of
the separate scanner.py background process. That split is what makes
Scenario 3 (non-blocking large scans) and Scenario 15 (progress survives
page reloads) hold: the browser/Flask can restart or disconnect at any
point without affecting the scan.
"""
import os
import subprocess
import sys
import tempfile
import time

from flask import Flask, request, jsonify, render_template, send_file, abort, Response

import config
import db
import preview
import session_io
from trash import move_to_trash

app = Flask(__name__)
db.init_db()


# ---------------------------------------------------------------- pages ----

@app.route("/")
def index():
    with db.get_conn() as conn:
        roots = conn.execute(
            """
            SELECT r.id, r.path,
                   (SELECT s.id FROM scans s WHERE s.root_id=r.id ORDER BY s.started_at DESC LIMIT 1) AS last_scan_id
            FROM roots r ORDER BY r.created_at DESC
            """
        ).fetchall()
        roots = [dict(r) for r in roots]
        for r in roots:
            if r["last_scan_id"]:
                scan = conn.execute("SELECT status, rotation_pass FROM scans WHERE id=?", (r["last_scan_id"],)).fetchone()
                r["last_scan_status"] = scan["status"] if scan else None
    return render_template("index.html", roots=roots)


@app.route("/review/<int:root_id>")
def review(root_id):
    with db.get_conn() as conn:
        root = conn.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
        if root is None:
            abort(404)
        scan = conn.execute(
            "SELECT * FROM scans WHERE root_id=? ORDER BY started_at DESC LIMIT 1", (root_id,)
        ).fetchone()
    return render_template("review.html", root=dict(root), scan=dict(scan) if scan else None)


# ------------------------------------------------------------- scan API ----

@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    data = request.get_json(force=True)
    folder = data.get("folder", "").strip()
    rotation_pass = bool(data.get("rotation_pass", False))

    if not folder or not os.path.isdir(folder):
        return jsonify({"error": f"Not a valid directory: {folder}"}), 400

    with db.get_conn() as conn:
        root_id = db.get_or_create_root(conn, os.path.abspath(folder))
        cur = conn.execute(
            "INSERT INTO scans(root_id, status, rotation_pass, started_at) VALUES (?, 'running', ?, ?)",
            (root_id, int(rotation_pass), time.time()),
        )
        scan_id = cur.lastrowid

    # Launch as a fully detached background process (Scenario 3): survives
    # this Flask worker restarting, and does not block the request/response
    # cycle regardless of library size.
    scanner_script = os.path.join(os.path.dirname(__file__), "scanner.py")
    cmd = [sys.executable, scanner_script, folder, "--scan-id", str(scan_id)]
    if rotation_pass:
        cmd.append("--rotation-pass")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # detach from Flask's process group
    )

    return jsonify({"root_id": root_id, "scan_id": scan_id})


def _calculate_eta(conn, scan_id, total, processed):
    """Scenario 13/14: rolling-window rate, 'calculating...' until stable."""
    now = time.time()
    window_start = now - config.ETA_WINDOW_SECONDS
    samples = conn.execute(
        "SELECT ts, processed_files FROM progress_samples "
        "WHERE scan_id=? AND ts >= ? ORDER BY ts ASC",
        (scan_id, window_start),
    ).fetchall()

    if len(samples) < config.ETA_MIN_SAMPLES:
        return {"status": "calculating"}

    oldest, latest = samples[0], samples[-1]
    dt = latest["ts"] - oldest["ts"]
    d_processed = latest["processed_files"] - oldest["processed_files"]
    if dt <= 0 or d_processed <= 0:
        return {"status": "calculating"}

    rate = d_processed / dt  # files/sec
    remaining = max(total - processed, 0)
    eta_seconds = remaining / rate if rate > 0 else None
    return {
        "status": "ok",
        "files_per_second": round(rate, 2),
        "eta_seconds": eta_seconds,
        "eta_human": _human_duration(eta_seconds) if eta_seconds is not None else None,
    }


def _human_duration(seconds):
    if seconds is None:
        return None
    minutes = seconds / 60
    hours = minutes / 60
    days = hours / 24
    if days >= 1:
        return f"~{days:.1f} days"
    if hours >= 1:
        return f"~{hours:.1f} hours"
    if minutes >= 1:
        return f"~{minutes:.0f} minutes"
    return "<1 minute"


@app.route("/api/scan/progress/<int:scan_id>")
def api_scan_progress(scan_id):
    with db.get_conn() as conn:
        scan = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if scan is None:
            abort(404)
        prog = conn.execute("SELECT * FROM scan_progress WHERE scan_id=?", (scan_id,)).fetchone()
        if prog is None:
            return jsonify({"status": scan["status"], "phase": "discovering", "processed": 0, "total": 0})

        eta = _calculate_eta(conn, scan_id, prog["total_files"], prog["processed_files"])

    return jsonify({
        "scan_status": scan["status"],
        "phase": prog["phase"],
        "processed": prog["processed_files"],
        "total": prog["total_files"],
        "updated_at": prog["updated_at"],
        "eta": eta,
        "error_message": scan["error_message"],
        "rotation_pass": bool(scan["rotation_pass"]),
    })


@app.route("/api/scan/stop/<int:scan_id>", methods=["POST"])
def api_scan_stop(scan_id):
    """Sets a flag the scanner process polls between files and checks
    promptly (every file during hashing, every file during the rotation
    pass) -- not an immediate kill, but it stops within roughly one file's
    processing time rather than waiting for a whole phase to finish.
    Whatever's been hashed so far is still clustered and made reviewable
    (see run_scan's ScanStopped handling), and a later /api/scan/start on
    the same folder picks up exactly where this left off via the existing
    per-file resumability (Scenario 7) -- no separate 'resume' machinery
    needed."""
    with db.get_conn() as conn:
        scan = conn.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        if scan is None:
            abort(404)
        if scan["status"] != "running":
            return jsonify({"error": f"scan is not running (status: {scan['status']})"}), 400
        conn.execute("UPDATE scans SET stop_requested=1 WHERE id=?", (scan_id,))
    return jsonify({"ok": True})


# ----------------------------------------------------------- results API ---

FILE_COLUMNS = """
    f.id, f.path, f.rel_path, f.size, f.width, f.height,
    f.exif_date, f.exif_date_raw, f.exif_datetime, f.exif_datetime_raw,
    COALESCE(rs.marked_for_delete, 0) AS marked_for_delete,
    COALESCE(rs.reviewed, 0) AS reviewed
"""

# Two-step pagination pattern used by all three grouping modes below, so a
# 270,000-file library never means building one giant response: step 1 asks
# SQL for just the *count per group key* (bounded by the number of DUPLICATE
# GROUPS, not files -- typically orders of magnitude smaller), pick out one
# page of keys from that, then step 2 fetches full row detail only for the
# files belonging to that page's groups. A whole-library JSON blob and a
# 270k-row HTML table are both avoided.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_GROUP_ROWS = 100  # per-group cap within a page (see WITH ranked... below);
                      # protects against one pathologically large group (e.g.
                      # thousands of files all named IMG_0001.jpg)


def _paginate(request):
    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    page_size = request.args.get("page_size", default=DEFAULT_PAGE_SIZE, type=int) or DEFAULT_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    return page, page_size


@app.route("/api/groups")
def api_groups():
    root_id = request.args.get("root_id", type=int)
    group_by = request.args.get("group_by", default="content")
    show_reviewed = request.args.get("show_reviewed", default="0") in ("1", "true", "True")
    page, page_size = _paginate(request)
    if root_id is None:
        return jsonify({"error": "root_id required"}), 400
    if group_by not in ("content", "date", "filename"):
        return jsonify({"error": f"unknown group_by: {group_by}"}), 400

    # By default, files marked "reviewed" (via the "Hide marked from
    # results" button) are left out entirely -- that's the whole point of
    # that feature: stop seeing folders/files you've already checked.
    # show_reviewed=1 brings them back without losing the flag, so nothing
    # is destructively lost the way an actual delete would be.
    reviewed_filter = "" if show_reviewed else "AND COALESCE(rs.reviewed, 0) = 0"

    # "Date" re-groups the SAME set of already-detected duplicates by day
    # instead of by content-group (this is what "switch grouping mode" means
    # per the original spec) -- it does NOT mean "any two unrelated photos
    # that happen to share a date." Without this restriction, a library
    # where many genuinely different photos share a capture day turns each
    # date into a giant non-duplicate "group," which is both meaningless and
    # -- as a 270k-file stress test surfaced -- slow and memory-heavy even
    # with pagination, since a single date could span thousands of files.
    dup_membership_filter = (
        "AND f.id IN (SELECT gm2.file_id FROM group_members gm2 "
        "JOIN groups g2 ON g2.id = gm2.group_id WHERE g2.root_id = f.root_id)"
        if group_by == "date" else ""
    )

    with db.get_conn() as conn:
        if group_by == "date":
            key_rows = conn.execute(
                f"""
                SELECT COALESCE(f.exif_date, 'Unknown date') AS gkey, COUNT(*) AS cnt
                FROM files f
                LEFT JOIN review_state rs ON rs.file_id = f.id
                WHERE f.root_id=? AND f.status='done' {reviewed_filter} {dup_membership_filter}
                GROUP BY gkey HAVING COUNT(*) >= 2
                ORDER BY gkey
                """,
                (root_id,),
            ).fetchall()
        elif group_by == "filename":
            key_rows = conn.execute(
                f"""
                SELECT f.filename AS gkey, COUNT(*) AS cnt
                FROM files f
                LEFT JOIN review_state rs ON rs.file_id = f.id
                WHERE f.root_id=? AND f.status='done' {reviewed_filter}
                GROUP BY f.filename HAVING COUNT(*) >= 2
                ORDER BY f.filename
                """,
                (root_id,),
            ).fetchall()
        else:  # "content" -- Scenario 1/2 default grouping
            key_rows = conn.execute(
                f"""
                SELECT g.id AS gkey, COUNT(*) AS cnt
                FROM group_members gm
                JOIN groups g ON g.id = gm.group_id
                JOIN files f ON f.id = gm.file_id
                LEFT JOIN review_state rs ON rs.file_id = f.id
                WHERE g.root_id=? AND f.status='done' {reviewed_filter}
                GROUP BY g.id HAVING COUNT(*) >= 2
                ORDER BY g.id
                """,
                (root_id,),
            ).fetchall()

        total_groups = len(key_rows)
        start = (page - 1) * page_size
        page_key_rows = key_rows[start:start + page_size]
        page_keys = [r["gkey"] for r in page_key_rows]
        counts_by_key = {r["gkey"]: r["cnt"] for r in page_key_rows}

        groups = {k: [] for k in page_keys}
        if page_keys:
            placeholders = ",".join("?" * len(page_keys))
            # A per-group cap, enforced in SQL via a window function, so a
            # single pathologically large group (e.g. thousands of files
            # all literally named IMG_0001.jpg from merged SD-card dumps)
            # can't blow up one page's response/DOM on its own -- caps the
            # per-page row count to page_size * MAX_GROUP_ROWS regardless of
            # how big any individual group actually is.
            if group_by == "date":
                rows = conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT {FILE_COLUMNS}, COALESCE(f.exif_date, 'Unknown date') AS group_key,
                               ROW_NUMBER() OVER (PARTITION BY COALESCE(f.exif_date, 'Unknown date')
                                                   ORDER BY f.rel_path) AS rn
                        FROM files f
                        LEFT JOIN review_state rs ON rs.file_id = f.id
                        WHERE f.root_id=? AND f.status='done' {reviewed_filter} {dup_membership_filter}
                          AND COALESCE(f.exif_date, 'Unknown date') IN ({placeholders})
                    )
                    SELECT * FROM ranked WHERE rn <= {MAX_GROUP_ROWS} ORDER BY group_key, rel_path
                    """,
                    (root_id, *page_keys),
                ).fetchall()
            elif group_by == "filename":
                rows = conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT {FILE_COLUMNS}, f.filename AS group_key,
                               ROW_NUMBER() OVER (PARTITION BY f.filename ORDER BY f.rel_path) AS rn
                        FROM files f
                        LEFT JOIN review_state rs ON rs.file_id = f.id
                        WHERE f.root_id=? AND f.status='done' {reviewed_filter}
                          AND f.filename IN ({placeholders})
                    )
                    SELECT * FROM ranked WHERE rn <= {MAX_GROUP_ROWS} ORDER BY group_key, rel_path
                    """,
                    (root_id, *page_keys),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT {FILE_COLUMNS}, g.id AS group_key,
                               ROW_NUMBER() OVER (PARTITION BY g.id ORDER BY f.rel_path) AS rn
                        FROM group_members gm
                        JOIN groups g ON g.id = gm.group_id
                        JOIN files f ON f.id = gm.file_id
                        LEFT JOIN review_state rs ON rs.file_id = f.id
                        WHERE g.root_id=? AND f.status='done' {reviewed_filter}
                          AND g.id IN ({placeholders})
                    )
                    SELECT * FROM ranked WHERE rn <= {MAX_GROUP_ROWS} ORDER BY group_key, rel_path
                    """,
                    (root_id, *page_keys),
                ).fetchall()
            for r in rows:
                groups[r["group_key"]].append(dict(r))

    # "Folder" is the ABSOLUTE path's directory, not just relative to the
    # scanned root -- makes it possible to locate a file directly (e.g. in
    # File Station) without having to mentally join it to the root path.
    for members in groups.values():
        for f in members:
            f["folder"] = os.path.dirname(f["path"])
            f.pop("rn", None)

    return jsonify({
        "group_by": group_by,
        "page": page,
        "page_size": page_size,
        "total_groups": total_groups,
        "total_pages": max(1, -(-total_groups // page_size)),  # ceil div
        "groups": [
            {
                "key": k,
                "files": v,
                "total_members": counts_by_key.get(k, len(v)),
                "truncated": counts_by_key.get(k, len(v)) > len(v),
            }
            for k, v in groups.items()
        ],
    })


@app.route("/api/summary")
def api_summary():
    """Headline numbers for the review page: how many duplicates were
    found, how many "extra" copies they represent, and roughly how much
    space could be reclaimed by keeping just the largest copy in each
    group. Reflects LIVE state (shrinks as you trash/hide things), not a
    frozen snapshot from when the scan finished -- deliberately ignores the
    "hide from results" filter, since "how many duplicates exist" should
    stay a stable, honest number regardless of what you've since decluttered
    from view."""
    root_id = request.args.get("root_id", type=int)
    if root_id is None:
        return jsonify({"error": "root_id required"}), 400

    with db.get_conn() as conn:
        total_done = conn.execute(
            "SELECT COUNT(*) AS c FROM files WHERE root_id=? AND status='done'", (root_id,)
        ).fetchone()["c"]

        trashed = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size), 0) AS s FROM files WHERE root_id=? AND status='trashed'",
            (root_id,),
        ).fetchone()

        group_stats = conn.execute(
            """
            SELECT g.id AS gid, COUNT(*) AS cnt, SUM(f.size) AS total_size, MAX(f.size) AS max_size
            FROM group_members gm
            JOIN groups g ON g.id = gm.group_id
            JOIN files f ON f.id = gm.file_id
            WHERE g.root_id=? AND f.status='done'
            GROUP BY g.id HAVING COUNT(*) >= 2
            """,
            (root_id,),
        ).fetchall()

        marked = conn.execute(
            """
            SELECT COUNT(*) AS c, COALESCE(SUM(f.size), 0) AS s
            FROM files f JOIN review_state rs ON rs.file_id = f.id
            WHERE f.root_id=? AND rs.marked_for_delete=1 AND f.status='done'
            """,
            (root_id,),
        ).fetchone()

    total_groups = len(group_stats)
    total_dup_files = sum(r["cnt"] for r in group_stats)
    extra_copies = total_dup_files - total_groups  # one "keeper" per group isn't extra
    potential_savings = sum((r["total_size"] or 0) - (r["max_size"] or 0) for r in group_stats)

    return jsonify({
        "total_files_scanned": total_done,
        "total_duplicate_groups": total_groups,
        "total_duplicate_files": total_dup_files,
        "extra_copies": extra_copies,
        "potential_space_savings_bytes": potential_savings,
        "already_trashed_count": trashed["c"],
        "already_trashed_space_bytes": trashed["s"],
        "marked_for_delete_count": marked["c"],
        "marked_for_delete_space_bytes": marked["s"],
    })


@app.route("/api/hide", methods=["POST"])
def api_hide():
    """Marks files 'reviewed' so they drop out of /api/groups by default --
    NOT a file-system delete, just hides already-checked files/groups from
    the results so they stop cluttering future review sessions. Reversible
    via the "Show reviewed" toggle, and this same 'reviewed' flag is what
    session export/import already carries (Scenario 16-19), so it survives
    a save/reload too."""
    data = request.get_json(force=True)
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "file_ids required"}), 400

    now = time.time()
    with db.get_conn() as conn:
        for fid in file_ids:
            existing = conn.execute("SELECT marked_for_delete FROM review_state WHERE file_id=?", (fid,)).fetchone()
            mfd = existing["marked_for_delete"] if existing else 0
            conn.execute(
                "INSERT INTO review_state(file_id, marked_for_delete, reviewed, updated_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET reviewed=1, updated_at=excluded.updated_at",
                (fid, mfd, now),
            )
    return jsonify({"ok": True, "hidden": len(file_ids)})


@app.route("/api/thumb/<int:file_id>")
def api_thumb(file_id):
    """Small JPEG for the hover-preview panel (Scenario: side-by-side
    current/previous preview on the review page)."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT path, mtime FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        abort(404)
    data = preview.get_preview_bytes(row["path"], file_id, row["mtime"], config.THUMB_MAX_DIM, "thumb")
    return Response(data, mimetype="image/jpeg")


@app.route("/api/full/<int:file_id>")
def api_full(file_id):
    """Larger JPEG for the double-click "open in a new tab" view."""
    with db.get_conn() as conn:
        row = conn.execute("SELECT path, mtime FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        abort(404)
    data = preview.get_preview_bytes(row["path"], file_id, row["mtime"], config.FULL_MAX_DIM, "full")
    return Response(data, mimetype="image/jpeg")


@app.route("/view/<int:file_id>")
def view_image(file_id):
    """Full-viewport image viewer for double-click. Accepts an optional
    ?ids=1,4,7 (the file ids of the duplicate group it was opened from, in
    display order) so the page can support Left/Right-arrow navigation
    between "previous, current, next" without closing the tab. Falls back
    to just the single image if no ids are given or file_id isn't in them."""
    ids_param = request.args.get("ids", "")
    ids = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
    if file_id not in ids:
        ids = [file_id]

    with db.get_conn() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, rel_path FROM files WHERE id IN ({placeholders})", ids
        ).fetchall()
    by_id = {r["id"]: r["rel_path"] for r in rows}
    images = [{"id": i, "rel_path": by_id[i]} for i in ids if i in by_id]
    if not images:
        abort(404)
    start_index = next((idx for idx, im in enumerate(images) if im["id"] == file_id), 0)

    return render_template("view.html", images=images, start_index=start_index)


@app.route("/api/mark", methods=["POST"])
def api_mark():
    data = request.get_json(force=True)
    file_id = data.get("file_id")
    marked_for_delete = data.get("marked_for_delete")
    reviewed = data.get("reviewed")

    with db.get_conn() as conn:
        existing = conn.execute("SELECT * FROM review_state WHERE file_id=?", (file_id,)).fetchone()
        mfd = int(marked_for_delete) if marked_for_delete is not None else (existing["marked_for_delete"] if existing else 0)
        rev = int(reviewed) if reviewed is not None else (existing["reviewed"] if existing else 0)
        conn.execute(
            "INSERT INTO review_state(file_id, marked_for_delete, reviewed, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(file_id) DO UPDATE SET marked_for_delete=excluded.marked_for_delete, "
            "reviewed=excluded.reviewed, updated_at=excluded.updated_at",
            (file_id, mfd, rev, time.time()),
        )
    return jsonify({"ok": True})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    """Scenario 6: move marked files into the per-root Trash folder."""
    data = request.get_json(force=True)
    file_ids = data.get("file_ids", [])
    if not file_ids:
        return jsonify({"error": "file_ids required"}), 400

    results = []
    with db.get_conn() as conn:
        for fid in file_ids:
            row = conn.execute(
                "SELECT f.*, r.path AS root_path FROM files f JOIN roots r ON r.id=f.root_id WHERE f.id=?",
                (fid,),
            ).fetchone()
            if row is None:
                results.append({"file_id": fid, "ok": False, "error": "not found"})
                continue
            try:
                dest = move_to_trash(row["root_path"], row["rel_path"], row["path"])
                conn.execute(
                    "UPDATE files SET status='trashed', trashed_path=? WHERE id=?", (dest, fid)
                )
                results.append({"file_id": fid, "ok": True, "trashed_path": dest})
            except Exception as e:
                conn.execute(
                    "UPDATE files SET status='error', error_message=? WHERE id=?", (str(e), fid)
                )
                results.append({"file_id": fid, "ok": False, "error": str(e)})
    return jsonify({"results": results})


# ------------------------------------------------------------ session API --

@app.route("/api/session/export")
def api_session_export():
    root_id = request.args.get("root_id", type=int)
    with db.get_conn() as conn:
        root = conn.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    if root is None:
        abort(404)

    group_by = request.args.get("group_by", default="content")
    payload = session_io.export_session(root["path"], extra_settings={"group_by": group_by})

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    import json
    json.dump(payload, tmp, indent=2)
    tmp.close()
    return send_file(tmp.name, as_attachment=True, download_name="dupefinder_session.json", mimetype="application/json")


@app.route("/api/session/import", methods=["POST"])
def api_session_import():
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    import json
    try:
        payload = json.load(request.files["file"].stream)
    except Exception as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400

    result = session_io.import_session(payload)
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
