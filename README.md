# Duplicate Image Finder

A background scanner + web review app for finding duplicate/near-duplicate
photos (including resized and rotated copies) across a large photo library,
built for a Synology DS220+ (6 GB RAM, DSM 7.4.1).

## How it's split, and why

- **`scanner.py`** — a standalone script/process. Walks a folder, computes a
  perceptual hash per image, clusters duplicates, and writes everything to
  SQLite. Runs independently of the browser (Scenario 3) and is resumable if
  interrupted (Scenario 7).
- **`app.py`** — a Flask web app. Only *reads* from SQLite and launches new
  scans (as a detached subprocess); it never blocks on scanning itself. This
  is why the review UI loads instantly (Scenario 5) and progress survives a
  closed/reopened browser tab (Scenario 15) — the state lives in the DB, not
  in server memory or the browser.
- **`hashing.py` / `cluster.py`** — the matching logic: a resize-robust DCT
  perceptual hash, EXIF-orientation normalization, and a BK-tree + union-find
  so clustering 120,000+ images doesn't require an O(n²) comparison.
- **`trash.py`** — moves marked files into a `_DupeFinder_Trash` folder under
  each scanned root, preserving relative path.
- **`session_io.py`** — export/import review state as portable JSON,
  separate from the SQLite DB.

## Setup (on the NAS or wherever you'll run this)

```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
python3 db.py       # creates app_data/dupefinder.db
python3 app.py       # starts the review app on http://<nas-ip>:5000
```

Open the web app, paste in a folder path, optionally check "also check for
rotated-by-re-encoding photos", and start a scan. You can close the browser
tab immediately — the scan keeps running as its own process. Reopen the
review page any time to see progress or, once it's done, the results table.

For a fully hands-off setup, wire `python3 app.py` into a Synology Task
Scheduler entry (or systemd/supervisor if you're running this in a
container) so it survives reboots. For very large first-time scans, running
`python3 scanner.py /path --rotation-pass` directly from a terminal (rather
than through the web UI) makes it easy to watch logs in `app_data/scanner.log`.

## Matching behavior (what's caught vs. the known limitation)

| Situation | Caught by default? |
|---|---|
| Renamed file, different EXIF, different folder | Yes |
| Resized once or many times, any generation | Yes |
| Rotated only via EXIF orientation flag | Yes |
| Rotated by re-encoding pixels (e.g. old phone exports) | No — enable the opt-in slower pass |

The opt-in pass additionally hashes 90°/180°/270° rotations of every
otherwise-unmatched image and re-checks them against the index. It's slower
because it triples the hashing work for files that didn't already match, so
it's off by default (Scenario 10) and a checkbox away when you want it
(Scenario 11).

## Notifications

Right now, scan completion goes to `app_data/scanner.log` and a best-effort
desktop notification (`notify-send`, if present — most headless NAS setups
won't have this, so the log is the reliable source of truth). Set
`DUPEFINDER_SMTP_HOST`, `DUPEFINDER_SMTP_USER`, `DUPEFINDER_SMTP_PASSWORD`,
and `DUPEFINDER_NOTIFY_EMAIL` as environment variables to also get an email
— no code changes needed once those are supplied.

## Save/load a review session

"Save session" downloads a small JSON file with your settings and which
files are marked/reviewed. It's independent of the database on purpose, so
you can back it up or move it. Loading a stale session (referencing files
that were moved, renamed, or since deleted) doesn't fail outright — it
applies whatever still matches (falling back to matching by content hash if
a path changed) and reports anything it couldn't reconcile.

## UI refinements (review page)

- **Full-width workspace** so the table has room to breathe.
- **Sticky preview panel** — stays visible at the top as you scroll through a
  long duplicates table, instead of scrolling away.
- **Table layout** — the duplicates area is a real `<table>` now (filename,
  folder, EXIF date/time, file date/time, dimensions, size), grouped with a
  header row per group.
- **Both date fields now include time-of-day**, not just the date, since two
  photos taken seconds apart can otherwise look identical in the table.
- **Double-click a row → opens full size in a new tab**, sized to fit via
  `object-fit: contain`. Once there, **Left/Right arrow keys** cycle through
  "previous, current, next" among the *other files in that same duplicate
  group* (not the whole library — keeps the URL short and the comparison
  relevant). Double-clicking the image itself toggles real browser
  fullscreen (the Fullscreen API requires a genuine user gesture, so this
  works reliably even though the initial auto-fullscreen attempt on tab-open
  may be blocked by some browsers — the image still fills the tab via CSS
  either way).

## Newer features

- **Synology `@eaDir` excluded automatically** — Synology's own per-folder
  thumbnail cache is skipped wherever it appears in the tree, so its
  derivative thumbnails never show up as "duplicates."
- **Group by filename** — a third grouping mode alongside content and date,
  for finding files that share a name (e.g. `IMG_0001.jpg`) across folders.
- **Hover preview** on the review page — hovering a row shows it in a
  "Current" panel next to whatever you hovered previously ("Previous"), so
  two candidates sit side by side for comparison. All formats (including
  HEIC/DNG) are converted to JPEG on the fly for this, so the browser never
  needs to understand the original format itself.
- **HEIC and DNG support** — decoding depends on `pillow-heif` and `rawpy`
  respectively (see `requirements-optional.txt`); the Dockerfile installs
  these best-effort so a missing wheel for your NAS's architecture can't
  break the whole build. Without them, files of that type are skipped with
  a clear message in `scanner.log` rather than crashing the scan.
- **Progress bar now accounts for the rotation pass** — previously the
  progress total only reflected the initial hashing phase, so enabling the
  opt-in rotation pass could show >100% or a stalled bar. The scanner now
  adds the actual rotation-pass workload to the total right before starting
  it. (A related bug was also fixed here: the rotation pass was reading
  stale duplicate-group data from the *previous* scan to decide which files
  still needed checking, which meant it re-checked far more files than
  necessary on a fresh scan.)

## Scan control and results decluttering

- **Stop / Resume** — the progress card has a Stop button while a scan is
  running. It sets a flag the scanner checks between files (roughly once per
  file, both during hashing and the opt-in rotation pass), so it stops
  within about one file's processing time rather than waiting for a whole
  phase to finish. Whatever's been hashed so far is still clustered and made
  reviewable immediately. Resume is just a normal "start scan" on the same
  folder — the scanner's existing per-file resumability (unchanged files are
  skipped) means it naturally continues from where it left off, so there's
  no separate resume mechanism to keep in sync. One caveat: Stop only works
  while the scanner process is actually alive — if it crashed or the NAS
  rebooted, the scan will just look stuck at "running" and Stop won't do
  anything (this isn't new; the same was already true before Stop existed).
  In that case, just start a fresh scan on the same folder.
- **Hide marked from results** — for groups you've already reviewed and
  decided to leave alone, check them and click "Hide marked from results."
  This does **not** touch anything on disk — it just marks those files
  reviewed so they drop out of the duplicates table, decluttering repeat
  visits. A "Show already-reviewed (hidden) items" toggle brings them back
  without losing the flag.
- **Folder column shows the absolute path** (e.g.
  `/volume1/photo/2024/summer`), not a path relative to the scanned root —
  makes it possible to locate a file directly (e.g. in File Station)
  without mentally re-joining it to the root.

## Scale: pagination and the summary panel

At libraries in the hundreds of thousands of files, rendering every
duplicate on one page will freeze the browser — so results are paginated at
the **group** level, not the file level: `/api/groups` returns one page of
duplicate groups (50 by default) at a time, and the SQL behind it only ever
materializes that page's rows, never the whole library. A "group by date"
view is additionally scoped to files that are *already* part of a detected
duplicate group (re-organizing the same duplicate set by date, per the
original spec) rather than "any two files that happen to share a day" —
without that, one date with thousands of unrelated photos could itself
blow up a single page. As a further safety net, any single oversized group
(e.g. thousands of files all literally named `IMG_0001.jpg`) is capped at
100 rows per page with a "too many to list in full" notice, so one
pathological group can't take down a page either. Tested against a
synthetic 270,000-file / 30,000-group database — every grouping mode and
the summary endpoint stayed under ~200ms.

The **Summary** card on the review page shows live totals — duplicate
groups found, extra copies, roughly how much space could be reclaimed
(computed as the size of everything in a group except its largest member,
on the assumption the largest copy is the best-quality one to keep), how
much is currently marked, and how much is already in Trash. These update
whenever you delete, hide, or the scan finishes — they're live counts from
the current DB state, not a frozen snapshot from when the scan completed,
so they naturally shrink as you clean things up.

## What was tested

Every scenario in the acceptance criteria has been exercised end-to-end with
synthetic test images (a resized-twice chain, an EXIF-rotated copy, a
pixel-re-encoded rotated copy, and an unrelated photo): grouping by content
and by date, the opt-in rotation pass, resumability across two scanner runs,
progress/ETA polling, mark → delete → Trash with relative-path preservation,
and session export/import including the rename/stale-reference fallback
path. Not yet tested against a real 120,000-image library or on actual
DS220+ hardware — worth a trial run on a representative subset before
pointing it at the full gallery, to confirm timing and memory headroom.

## Possible follow-ups (not built, flagged for later)

- Thumbnails in the review table (currently text + dimensions/size only).
- A fully portable session snapshot that also carries scan/group history for
  moving to a brand-new NAS, vs. today's "resume on the same DB" checkpoint.
- WSGI server (gunicorn/waitress) instead of Flask's dev server for the
  actual deployment.
