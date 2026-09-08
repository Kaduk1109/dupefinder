"""
Content-based grouping (Scenario 1, 8, 9, 11).

At 120,000+ images, comparing every pair (O(n^2) = ~1.4*10^10 comparisons)
is not practical on a DS220+. Instead we index all pHashes in a BK-tree
(a metric tree keyed on Hamming distance) so that for each image we can find
"all hashes within threshold N" in roughly O(log n) average time, then union
matches together with a union-find structure so that chains of near-dupes
(A~B~C even if A and C alone are slightly over threshold) end up in one group.

Date-based grouping (Scenario 2) doesn't need this machinery -- it's a
straight SQL GROUP BY on exif_date, computed on the fly in app.py.
"""
from collections import defaultdict
from hashing import hamming_distance


class BKTree:
    """Classic BK-tree over an integer-distance metric (Hamming distance here)."""

    def __init__(self, distance_fn):
        self.distance_fn = distance_fn
        self.tree = None  # (item, {distance: child_node})

    def add(self, item):
        if self.tree is None:
            self.tree = (item, {})
            return
        node = self.tree
        while True:
            value, children = node
            d = self.distance_fn(item, value)
            if d == 0:
                return  # exact duplicate hash already present, nothing to add
            if d in children:
                node = children[d]
            else:
                children[d] = (item, {})
                return

    def find_within(self, item, threshold):
        """Return list of items in the tree within `threshold` distance of item."""
        if self.tree is None:
            return []
        results = []
        candidates = [self.tree]
        while candidates:
            value, children = candidates.pop()
            d = self.distance_fn(item, value)
            if d <= threshold:
                results.append((value, d))
            lo, hi = d - threshold, d + threshold
            for child_d, child_node in children.items():
                if lo <= child_d <= hi:
                    candidates.append(child_node)
        return results


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _int_hamming(hex_a, hex_b):
    return hamming_distance(hex_a, hex_b)


def cluster_files(file_rows, threshold):
    """file_rows: iterable of sqlite Rows with .id and .phash (non-null).
    Returns: dict[file_id] -> group_key (an arbitrary hashable group id),
    only for files that ended up in a group of size >= 2."""
    tree = BKTree(_int_hamming)
    hash_to_ids = defaultdict(list)
    uf = UnionFind()

    rows = [r for r in file_rows if r["phash"]]
    for row in rows:
        hash_to_ids[row["phash"]].append(row["id"])

    for row in rows:
        h = row["phash"]
        matches = tree.find_within(h, threshold)
        for match_hash, _dist in matches:
            for other_id in hash_to_ids[match_hash]:
                uf.union(row["id"], other_id)
        tree.add(h)

    # Group by union-find root; keep only groups with 2+ members.
    root_to_members = defaultdict(list)
    for row in rows:
        root_to_members[uf.find(row["id"])].append(row["id"])

    file_to_group = {}
    for root, members in root_to_members.items():
        if len(members) >= 2:
            for m in members:
                file_to_group[m] = root
    return file_to_group


def merge_rotation_matches(file_rows_all, existing_groups, threshold):
    """Scenario 11: for files NOT already in a group, check their rotated
    variant hashes (phash_rot90/180/270) against every other file's primary
    hash. If a match is found, merge them into the same group.

    `existing_groups`: dict[file_id] -> group_key from cluster_files().
    Returns an updated dict[file_id] -> group_key (new synthetic keys for
    brand-new groups formed only via rotation matching)."""
    tree = BKTree(_int_hamming)
    hash_to_ids = defaultdict(list)
    for row in file_rows_all:
        if row["phash"]:
            hash_to_ids[row["phash"]].append(row["id"])
            tree.add(row["phash"])

    uf = UnionFind()
    for fid, gkey in existing_groups.items():
        uf.union(fid, ("existing", gkey))

    found_any = False
    for row in file_rows_all:
        for col in ("phash_rot90", "phash_rot180", "phash_rot270"):
            rh = row[col] if col in row.keys() else None
            if not rh:
                continue
            for match_hash, _dist in tree.find_within(rh, threshold):
                for other_id in hash_to_ids[match_hash]:
                    if other_id == row["id"]:
                        continue
                    uf.union(row["id"], other_id)
                    found_any = True

    if not found_any:
        return existing_groups

    root_to_members = defaultdict(list)
    all_ids = {row["id"] for row in file_rows_all}
    for fid in all_ids:
        root_to_members[uf.find(fid)].append(fid)

    updated = {}
    for root, members in root_to_members.items():
        if len(members) >= 2:
            for m in members:
                updated[m] = root
    return updated
