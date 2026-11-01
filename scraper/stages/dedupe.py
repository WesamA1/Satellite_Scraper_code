"""The dedupe stage: perceptual-hash duplicate grouping.

Ported from reference/review_gui.py's dhash()/find_duplicate_suspects() --
same 64-bit difference hash and the same MAX_HAMMING=8 threshold, which took
real tuning (see that file's comment on why cross-family near-matches are
never flagged: sister programs legitimately share similar-looking photos).

DB-driven instead of CSV-cached: hashes live in images.perceptual_hash and
groups in images.duplicate_group_id, so this never touches a cache file on
disk. This stage only detects and groups -- it never changes an image's
status. A human decides what happens to each group on the Review page,
same human-in-the-loop principle as the license gate and promotion flow.
"""

import sqlite3
from pathlib import Path

MAX_HAMMING = 8
HASH_SIZE = 8


def compute_phash(path: Path) -> str | None:
    """64-bit difference hash of an image, as a hex string -- or None if the
    file is missing or unreadable."""
    from PIL import Image

    try:
        img = Image.open(path).convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
    except Exception:
        return None

    pixels = list(img.getdata())
    row_width = HASH_SIZE + 1
    bits = 0
    for row in range(HASH_SIZE):
        for col in range(HASH_SIZE):
            bits = (bits << 1) | (pixels[row * row_width + col] > pixels[row * row_width + col + 1])
    return format(bits, "016x")


def _hamming_distance(hash_a: str, hash_b: str) -> int:
    return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


class _UnionFind:
    """Minimal union-find for grouping duplicate links into components --
    an image can be "close" to several others, and they all need to end up
    in one shared group rather than separate overlapping pairs."""

    def __init__(self):
        self._parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_a] = root_b

    def components(self, members: list[int]) -> dict[int, list[int]]:
        groups: dict[int, list[int]] = {}
        for member in members:
            groups.setdefault(self.find(member), []).append(member)
        return groups


def _hash_missing_images(conn: sqlite3.Connection, families: list[str] | None) -> int:
    query = "SELECT id, path FROM images WHERE status = 'active' AND perceptual_hash IS NULL"
    params: tuple = ()
    if families:
        placeholders = ",".join("?" for _ in families)
        query += f" AND family IN ({placeholders})"
        params = tuple(families)

    rows = conn.execute(query, params).fetchall()
    hashed = 0
    for row in rows:
        phash = compute_phash(Path(row["path"]))
        if phash is not None:
            conn.execute("UPDATE images SET perceptual_hash = ? WHERE id = ?", (phash, row["id"]))
            hashed += 1
    conn.commit()
    return hashed


def run_dedupe(conn: sqlite3.Connection, families: list[str] | None = None) -> dict:
    """Hashes every active, not-yet-hashed image (optionally scoped to
    `families`), then groups duplicates: exact hash matches link regardless
    of family, near-matches (distance <= MAX_HAMMING) link only within the
    same family. Returns {"hashed", "groups", "grouped_images"}."""
    hashed = _hash_missing_images(conn, families)

    query = "SELECT id, family, perceptual_hash FROM images WHERE status = 'active' AND perceptual_hash IS NOT NULL"
    params: tuple = ()
    if families:
        placeholders = ",".join("?" for _ in families)
        query += f" AND family IN ({placeholders})"
        params = tuple(families)
    rows = conn.execute(query, params).fetchall()

    uf = _UnionFind()
    by_hash: dict[str, list[int]] = {}
    by_family: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        by_hash.setdefault(row["perceptual_hash"], []).append(row["id"])
        by_family.setdefault(row["family"], []).append((row["id"], row["perceptual_hash"]))

    for ids in by_hash.values():
        for other_id in ids[1:]:
            uf.union(ids[0], other_id)

    for members in by_family.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                id_a, hash_a = members[i]
                id_b, hash_b = members[j]
                if _hamming_distance(hash_a, hash_b) <= MAX_HAMMING:
                    uf.union(id_a, id_b)

    all_ids = [row["id"] for row in rows]
    groups = {root: members for root, members in uf.components(all_ids).items() if len(members) > 1}

    # Only clear groups within this run's own scope (all active+hashed images
    # when families=None) -- never touch images outside the requested
    # families, or a scoped run would wipe out an earlier unscoped grouping.
    if all_ids:
        placeholders = ",".join("?" for _ in all_ids)
        conn.execute(f"UPDATE images SET duplicate_group_id = NULL WHERE id IN ({placeholders})", all_ids)

    grouped_images = 0
    for group_id, members in groups.items():
        conn.executemany(
            "UPDATE images SET duplicate_group_id = ? WHERE id = ?",
            [(group_id, member_id) for member_id in members],
        )
        grouped_images += len(members)
    conn.commit()

    return {"hashed": hashed, "groups": len(groups), "grouped_images": grouped_images}
