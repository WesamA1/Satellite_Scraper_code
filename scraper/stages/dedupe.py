"""The dedupe stage: perceptual-hash duplicate grouping.

Ported from reference/review_gui.py's dhash()/find_duplicate_suspects() --
same 64-bit difference hash and the same MAX_HAMMING=8 threshold, which took
real tuning (see that file's comment on why cross-family near-matches are
never flagged: sister programs legitimately share similar-looking photos).

DB-driven instead of CSV-cached: hashes live in images.perceptual_hash and
groups in images.duplicate_group_id, so this never touches a cache file on
disk.

One thing is decided without a human: byte-for-byte identical files
(images.content_sha256) under the same family. They're the same image, so
the best copy is kept and the rest are quarantined as duplicates -- never
deleted. Identical files under *different* families aren't touched here:
which family the image really is goes to a human on the Review page. So do
look-alikes (same or close perceptual hash, different files), since two
different photos can hash alike.
"""

import sqlite3
from pathlib import Path

from scraper.stages import review
from scraper.stages.embed import file_sha256

MAX_HAMMING = 8
HASH_SIZE = 8


def compute_phash(path: Path) -> str | None:
    """64-bit difference hash of an image, as a hex string -- or None if the
    file is missing or unreadable."""
    from PIL import Image

    try:
        with Image.open(path) as image:
            img = image.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), Image.LANCZOS)
    except Exception:
        return None

    # Pillow 12 renamed getdata (removed in Pillow 14); fall back for older versions.
    pixels = list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())
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


def _hash_missing_contents(conn: sqlite3.Connection, families: list[str] | None) -> None:
    query = "SELECT id, path FROM images WHERE status = 'active' AND content_sha256 IS NULL"
    params: tuple = ()
    if families:
        placeholders = ",".join("?" for _ in families)
        query += f" AND family IN ({placeholders})"
        params = tuple(families)
    for row in conn.execute(query, params).fetchall():
        path = Path(row["path"])
        if path.is_file():
            conn.execute("UPDATE images SET content_sha256 = ? WHERE id = ?", (file_sha256(path), row["id"]))
    conn.commit()


def remove_identical_copies(
    conn: sqlite3.Connection, families: list[str] | None = None, quarantine_dir: Path = Path("_quarantine")
) -> dict:
    """Byte-identical active files: within one family, keep the best copy
    and quarantine the rest; across families, leave them for Review's
    "which family?" question. Returns {"identical_copies_removed",
    "families_to_choose"}."""
    _hash_missing_contents(conn, families)
    rows = conn.execute(
        "SELECT id, family, origin, content_sha256 FROM images WHERE status = 'active' AND content_sha256 IS NOT NULL"
    ).fetchall()
    by_content: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_content.setdefault(row["content_sha256"], []).append(row)

    removed, to_choose = 0, 0
    for copies in by_content.values():
        if len(copies) < 2 or (families and not any(c["family"] in families for c in copies)):
            continue
        if len({c["family"] for c in copies}) > 1:
            # Imported-only sets are the old data's own two-names-for-one-photo
            # cases, already annotated under each name: nothing for Review to ask.
            if any(c["origin"] != "imported" for c in copies):
                to_choose += 1
            continue
        review.resolve_identical_copies(conn, [c["id"] for c in copies], quarantine_dir)
        removed += len(copies) - 1
    return {"identical_copies_removed": removed, "families_to_choose": to_choose}


def run_dedupe(
    conn: sqlite3.Connection, families: list[str] | None = None, quarantine_dir: Path = Path("_quarantine")
) -> dict:
    """Removes identical copies within a family (remove_identical_copies),
    then hashes every active, not-yet-hashed image (optionally scoped to
    `families`) and groups duplicates: exact hash matches link regardless
    of family, near-matches (distance <= MAX_HAMMING) link only within the
    same family. Returns {"identical_copies_removed", "families_to_choose",
    "hashed", "groups", "grouped_images"}."""
    identical = remove_identical_copies(conn, families, quarantine_dir)
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
    conn.executemany("UPDATE images SET duplicate_group_id = NULL WHERE id = ?", [(i,) for i in all_ids])

    grouped_images = 0
    for group_id, members in groups.items():
        conn.executemany(
            "UPDATE images SET duplicate_group_id = ? WHERE id = ?",
            [(group_id, member_id) for member_id in members],
        )
        grouped_images += len(members)
    conn.commit()

    return {**identical, "hashed": hashed, "groups": len(groups), "grouped_images": grouped_images}
