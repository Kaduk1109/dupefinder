"""
One-off / on-demand re-clustering of already-scanned files, using the
current (fixed) cluster.py algorithm, WITHOUT re-scanning the filesystem or
recomputing any phash. Safe to run any time after a scan has completed --
reads phash values already stored in `files`, re-runs cluster_files(), and
atomically replaces this root's rows in `groups`/`group_members`.

Usage: python rebuild_groups.py <root_id>
"""
import sys
import db
import cluster
import config


def rebuild_groups(root_id):
    with db.get_conn() as conn:
        scan = conn.execute(
            "SELECT id FROM scans WHERE root_id=? ORDER BY started_at DESC LIMIT 1",
            (root_id,),
        ).fetchone()
        if scan is None:
            raise SystemExit(f"No scan found for root_id={root_id}")
        scan_id = scan["id"]

        rows = conn.execute(
            "SELECT id, phash FROM files WHERE root_id=? AND status='done'",
            (root_id,),
        ).fetchall()
        if not rows:
            raise SystemExit(f"No done files found for root_id={root_id}")

        file_to_group = cluster.cluster_files(rows, config.MATCH_HAMMING_THRESHOLD)

        # Replace this root's groups atomically -- review page never sees a
        # half-empty intermediate state, since get_conn()'s context manager
        # only commits once this whole block finishes without error.
        conn.execute(
            "DELETE FROM group_members WHERE group_id IN "
            "(SELECT id FROM groups WHERE root_id=?)",
            (root_id,),
        )
        conn.execute("DELETE FROM groups WHERE root_id=?", (root_id,))

        group_id_by_key = {}
        for file_id, key in file_to_group.items():
            if key not in group_id_by_key:
                cur = conn.execute(
                    "INSERT INTO groups(root_id, scan_id, method) VALUES (?, ?, 'content')",
                    (root_id, scan_id),
                )
                group_id_by_key[key] = cur.lastrowid
            conn.execute(
                "INSERT INTO group_members(group_id, file_id) VALUES (?, ?)",
                (group_id_by_key[key], file_id),
            )

        print(
            f"root_id={root_id}: пересобрано {len(group_id_by_key)} групп "
            f"из {len(rows)} файлов (было привязано к scan_id={scan_id})"
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python rebuild_groups.py <root_id>")
    rebuild_groups(int(sys.argv[1]))