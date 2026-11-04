"""The review stage: human keep/reject/duplicate decisions and variant
labeling.

Decision handling (the move-to-quarantine-and-update-status pattern) mirrors
scraper/stages/filter.py's run_filter -- a human manually rejecting an image
here is the same bucket the CLIP filter already represents ('rejected_filter'),
just applied by a person instead of a model. 'rejected_duplicate' was already
a valid status in the schema's own CHECK constraint before this stage existed.

Variant labeling reuses the existing annotations table as-is (attribute=
'variant') -- no schema change needed there, only the images.reviewed_at
column (migration 0004) is new, since nothing before this stage needed to
tell "still needs review" apart from "reviewed and kept".

Identical files (same content_sha256, migration 0006) are one image, so
they're decided together. Copies within one family are cleaned up by the
dedupe stage without asking (resolve_identical_copies). Copies filed under
different families need one human answer -- which family it really is --
before the image can be kept; choose_family applies that answer to every
copy. A reject likewise rejects every copy.
"""

import csv
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identical_copies(conn: sqlite3.Connection, image_id: int) -> list[sqlite3.Row]:
    """Other active images that are byte-for-byte the same file."""
    return conn.execute(
        """SELECT other.id, other.family, other.path, other.reviewed_at FROM images AS this
           JOIN images AS other ON other.content_sha256 = this.content_sha256 AND other.id != this.id
           WHERE this.id = ? AND this.content_sha256 IS NOT NULL AND other.status = 'active'
           ORDER BY other.id""",
        (image_id,),
    ).fetchall()


def family_choice(conn: sqlite3.Connection, image_id: int) -> dict | None:
    """When this file was also downloaded under other families: which ones,
    for Review to ask which family the image really belongs to."""
    row = conn.execute("SELECT family FROM images WHERE id = ?", (image_id,)).fetchone()
    copies = identical_copies(conn, image_id)
    families = {c["family"] for c in copies} | ({row["family"]} if row else set())
    if len(families) < 2:
        return None
    return {"families": sorted(families), "copies": len(copies) + 1}


def best_copy(conn: sqlite3.Connection, ids: list[int], prefer: int | None = None) -> int:
    """Which of several identical files to keep: one with annotations (so
    they stay attached to an active image), then `prefer` (the copy on
    screen), then one already reviewed, then the first downloaded."""
    placeholders = ",".join("?" for _ in ids)
    annotated = {
        r["image_id"]
        for r in conn.execute(
            f"SELECT DISTINCT image_id FROM annotations WHERE image_id IN ({placeholders}) AND attribute != 'variant'",
            ids,
        )
    }
    reviewed = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM images WHERE id IN ({placeholders}) AND reviewed_at IS NOT NULL", ids)
    }
    return min(ids, key=lambda i: (i not in annotated, i != prefer, i not in reviewed, i))


def review_queue(conn: sqlite3.Connection, family: str | None = None, limit: int = 50) -> list[dict]:
    """Active images that haven't been reviewed yet, each with its
    duplicate-group siblings (if any) for side-by-side comparison, and
    the families to choose between when the same file sits in several."""
    query = "SELECT id, family, path, duplicate_group_id FROM images WHERE status = 'active' AND reviewed_at IS NULL"
    params: tuple = ()
    if family:
        query += " AND family = ?"
        params = (family,)
    query += " ORDER BY id LIMIT ?"
    rows = conn.execute(query, (*params, limit)).fetchall()

    queue = []
    for row in rows:
        siblings = []
        if row["duplicate_group_id"] is not None:
            sibling_rows = conn.execute(
                "SELECT id FROM images WHERE duplicate_group_id = ? AND id != ?",
                (row["duplicate_group_id"], row["id"]),
            ).fetchall()
            siblings = [s["id"] for s in sibling_rows]
        queue.append(
            {
                "id": row["id"],
                "family": row["family"],
                "duplicate_group_id": row["duplicate_group_id"],
                "duplicate_siblings": siblings,
                "family_choice": family_choice(conn, row["id"]),
            }
        )
    return queue


def decide(conn: sqlite3.Connection, image_id: int, decision: str, quarantine_dir: Path = Path("_quarantine")) -> None:
    """decision is one of 'keep', 'reject', 'duplicate'. 'keep' leaves the
    file in place; 'reject'/'duplicate' move it into quarantine_dir, same as
    run_filter's rejects, so quarantine/ stays the one place to look for
    anything no longer in the active set."""
    if decision not in ("keep", "reject", "duplicate"):
        raise ValueError(f"Unknown decision: {decision!r}")

    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise ValueError(f"No image with id {image_id}")

    if decision == "keep" and family_choice(conn, image_id):
        raise ValueError("This file was also found under other families -- choose its family first.")
    if decision == "reject":
        # Same file, same verdict: rejecting one copy rejects them all.
        for copy in identical_copies(conn, image_id):
            _quarantine(conn, copy["id"], "rejected_filter", quarantine_dir)

    if decision == "keep":
        conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), image_id))
        conn.commit()
        return

    _quarantine(conn, image_id, "rejected_filter" if decision == "reject" else "rejected_duplicate", quarantine_dir)


def _free_path(dest: Path) -> Path:
    """dest, or dest with a _2/_3... suffix if that name is already taken --
    so a move never overwrites a file already sitting there."""
    candidate, n = dest, 2
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    return candidate


def _quarantine(conn: sqlite3.Connection, image_id: int, status: str, quarantine_dir: Path) -> None:
    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    path = Path(row["path"])
    if path.exists():
        dest = _free_path(quarantine_dir / row["family"] / path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        new_path = str(dest)
    else:
        new_path = row["path"]  # already missing -- just record the decision

    conn.execute(
        "UPDATE images SET path = ?, status = ?, reviewed_at = ? WHERE id = ?",
        (new_path, status, _now(), image_id),
    )
    conn.commit()


def resolve_identical_copies(conn: sqlite3.Connection, ids: list[int], quarantine_dir: Path, prefer=None) -> int:
    """Keeps the best of several identical files and quarantines the rest as
    duplicates. Returns the id kept."""
    keep = best_copy(conn, ids, prefer)
    for image_id in ids:
        if image_id != keep:
            _quarantine(conn, image_id, "rejected_duplicate", quarantine_dir)
    return keep


def choose_family(
    conn: sqlite3.Connection,
    image_id: int,
    family: str,
    quarantine_dir: Path = Path("_quarantine"),
    catalog: Path = Path("satellites.csv"),
) -> dict:
    """The answer to "which family is this file?" for a file downloaded under
    several: keeps one copy, files it under `family` (moving it into that
    family's folder), and quarantines the other copies. `family` has to be
    one the copies were found under, or one from the satellite catalog."""
    family = (family or "").strip()
    choice = family_choice(conn, image_id)
    if choice is None:
        raise ValueError("This image isn't a file shared between families.")
    if family not in choice["families"]:
        from scraper.families import load_families

        known = load_families(catalog) if catalog.is_file() else []
        if family not in known:
            raise ValueError(f"'{family}' isn't one of the found families or in the satellite catalog.")

    ids = [image_id] + [c["id"] for c in identical_copies(conn, image_id)]
    keep = resolve_identical_copies(conn, ids, quarantine_dir, prefer=image_id)

    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (keep,)).fetchone()
    path = Path(row["path"])
    new_path = path
    # Downloads live at <image dir>/<family>/<file>; follow that layout when
    # it's there, and otherwise leave the file where it is.
    if family != row["family"] and path.exists() and path.parent.name == row["family"]:
        new_path = _free_path(path.parent.parent / family / path.name)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(new_path))
    conn.execute(
        "UPDATE images SET family = ?, path = ?, duplicate_group_id = NULL WHERE id = ?", (family, str(new_path), keep)
    )
    conn.commit()
    return {"kept": keep, "family": family, "quarantined": len(ids) - 1}


def available_variants(family: str, catalog: Path = Path("satellites.csv")) -> list[str]:
    """Distinct VARIANT_NAME values for `family`, sorted. Empty for a family
    with no distinct variants -- the review page falls back to free text."""
    variants = set()
    with open(catalog, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["FAMILY_NAME"].strip() != family:
                continue
            variant = row.get("VARIANT_NAME", "").strip()
            if variant and variant != family:
                variants.add(variant)
    return sorted(variants)


def record_variant(conn: sqlite3.Connection, image_id: int, variant: str, annotator: str = "human_review") -> None:
    conn.execute(
        """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
           VALUES (?, 'variant', ?, ?, '1', ?)
           ON CONFLICT(image_id, attribute, annotator)
           DO UPDATE SET value = excluded.value, created_at = excluded.created_at""",
        (image_id, variant, annotator, _now()),
    )
    conn.commit()


def get_variant(conn: sqlite3.Connection, image_id: int, annotator: str = "human_review") -> str | None:
    row = conn.execute(
        "SELECT value FROM annotations WHERE image_id = ? AND attribute = 'variant' AND annotator = ?",
        (image_id, annotator),
    ).fetchone()
    return row["value"] if row else None
