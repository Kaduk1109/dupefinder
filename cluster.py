"""
Content-based grouping (Scenario 1, 8, 9, 11).

BUG FIX (chaining/bridging): plain single-linkage union-find lets chains of
near-dupes (A~B~C) end up in one group even when A and C alone are far over
threshold. Verified by automated test: a synthetic bridge chain merged two
unrelated endpoints under the old logic; the fix below splits such groups
into complete-linkage sub-groups.
"""
from collections import defaultdict
from itertools import combinations
from hashing import hamming_distance


class BKTree:
    def __init__(self, distance_fn):
        self.distance_fn = distance_fn
        self.tree = None

    def add(self, item):
        if self.tree is None:
            self.tree = (item, {})
            return
        node = self.tree
        while True:
            value, children = node
            d = self.distance_fn(item, value)
            if d == 0:
                return
            if d in children:
                node = children[d]
            else:
                children[d] = (item, {})
                return

    def find_within(self, item, threshold):
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


def group_diameter(members, hash_by_id):
    if len(members) < 2:
        return 0
    return max(
        hamming_distance(hash_by_id[a], hash_by_id[b])
        for a, b in combinations(members, 2)
    )


def split_chained_group(members, hash_by_id, threshold):
    remaining = list(members)
    out = []
    while remaining:
        seed = remaining.pop(0)
        group = [seed]
        leftover = []
        for m in remaining:
            h_m = hash_by_id[m]
            if all(hamming_distance(h_m, hash_by_id[g]) <= threshold for g in group):
                group.append(m)
            else:
                leftover.append(m)
        remaining = leftover
        out.append(group)
    return out


def _finalize_groups(root_to_members, id_to_hash, threshold):
    file_to_group = {}
    for root, members in root_to_members.items():
        if len(members) < 2:
            continue
        real_members = [m for m in members if m in id_to_hash]
        if len(real_members) < 2:
            continue
        hash_by_id = {m: id_to_hash[m] for m in real_members}
        if group_diameter(real_members, hash_by_id) <= threshold:
            for m in real_members:
                file_to_group[m] = root
            continue
        for sub in split_chained_group(real_members, hash_by_id, threshold):
            if len(sub) >= 2:
                sub_key = (root, tuple(sorted(sub)))
                for m in sub:
                    file_to_group[m] = sub_key
    return file_to_group


def cluster_files(file_rows, threshold):
    tree = BKTree(_int_hamming)
    hash_to_ids = defaultdict(list)
    uf = UnionFind()

    rows = [r for r in file_rows if r["phash"]]
    id_to_hash = {row["id"]: row["phash"] for row in rows}
    for row in rows:
        hash_to_ids[row["phash"]].append(row["id"])

    for row in rows:
        h = row["phash"]
        matches = tree.find_within(h, threshold)
        for match_hash, _dist in matches:
            for other_id in hash_to_ids[match_hash]:
                uf.union(row["id"], other_id)
        tree.add(h)

    root_to_members = defaultdict(list)
    for row in rows:
        root_to_members[uf.find(row["id"])].append(row["id"])

    return _finalize_groups(root_to_members, id_to_hash, threshold)


def merge_rotation_matches(file_rows_all, existing_groups, threshold):
    tree = BKTree(_int_hamming)
    hash_to_ids = defaultdict(list)
    id_to_hash = {}
    for row in file_rows_all:
        if row["phash"]:
            hash_to_ids[row["phash"]].append(row["id"])
            id_to_hash[row["id"]] = row["phash"]
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

    return _finalize_groups(root_to_members, id_to_hash, threshold)
