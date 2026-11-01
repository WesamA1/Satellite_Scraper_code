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
"""

import csv
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def review_queue(conn: sqlite3.Connection, family: str | None = None, limit: int = 50) -> list[dict]:
    """Active images that haven't been reviewed yet, each with its
    duplicate-group siblings (if any) for side-by-side comparison."""
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

    if decision == "keep":
        conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), image_id))
        conn.commit()
        return

    status = "rejected_filter" if decision == "reject" else "rejected_duplicate"
    path = Path(row["path"])
    if path.exists():
        dest = quarantine_dir / row["family"] / path.name
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
