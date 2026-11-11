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

Imported images (origin='imported', from a data/ folder) take precedence:
when one is the same file as a scraped image, the imported copy is kept --
it's the one people already annotated -- and takes over the scraped copy's
source and license record, so the license provenance isn't lost.

The master set (scraper/master.py): Keep moves an image into its one home,
data/images/<family>/, and every decision is remembered in the ledger
(data/ledger.db), so a rejected image never comes back from any source.
A rejected file that was in data/ goes to data/_rejected/<family>/ rather
than _quarantine/, which a wipe empties.
"""

import csv
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# What a reviewer can say about a rejected image (stored in images.reject_reason).
# The dataset wants one recognizable satellite per image -- a photo or a
# rendering, and partial views are fine.
REJECT_REASONS = {
    "several_satellites": "Several satellites",
    "not_recognizable": "Not recognizable (half-built, too small or too blurry)",
    "not_a_satellite": "Not a satellite",
    "other": "Other",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identical_copies(conn: sqlite3.Connection, image_id: int) -> list[sqlite3.Row]:
    """Other active images that are byte-for-byte the same file."""
    return conn.execute(
        """SELECT other.id, other.family, other.path, other.reviewed_at, other.origin FROM images AS this
           JOIN images AS other ON other.content_sha256 = this.content_sha256 AND other.id != this.id
           WHERE this.id = ? AND this.content_sha256 IS NOT NULL AND other.status = 'active'
           ORDER BY other.id""",
        (image_id,),
    ).fetchall()


def family_choice(conn: sqlite3.Connection, image_id: int) -> dict | None:
    """When this file was also downloaded under other families: which ones,
    for Review to ask which family the image really belongs to."""
    row = conn.execute("SELECT family, origin FROM images WHERE id = ?", (image_id,)).fetchone()
    copies = identical_copies(conn, image_id)
    families = {c["family"] for c in copies} | ({row["family"]} if row else set())
    if len(families) < 2:
        return None
    imported = [c["family"] for c in copies if c["origin"] == "imported"]
    if row and row["origin"] == "imported":
        imported.insert(0, row["family"])
    return {
        "families": sorted(families),
        "copies": len(copies) + 1,
        "preselected": imported[0] if imported else None,  # imported copies win, so start on their family
    }


def best_copy(conn: sqlite3.Connection, ids: list[int], prefer: int | None = None) -> int:
    """Which of several identical files to keep: an imported copy first (it's
    what people annotated in the old tool), then one with annotations (so
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
    imported = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM images WHERE id IN ({placeholders}) AND origin = 'imported'", ids)
    }
    return min(ids, key=lambda i: (i not in imported, i not in annotated, i != prefer, i not in reviewed, i))


# What's waiting for a person: active, never reviewed.
_WAITING = "status = 'active' AND reviewed_at IS NULL"
# Of those, what the filter hasn't scored yet (it only screens scraped images).
_UNSCREENED = _WAITING + " AND origin = 'scraped' AND filter_score IS NULL"
# What the filter set aside on its own (not a person's reject).
_SCREENED_OUT = "status = 'rejected_filter' AND reject_reason = 'filter'"


def review_queue(conn: sqlite3.Connection, family: str | None = None, limit: int = 50) -> list[dict]:
    """Active images that haven't been reviewed yet, each with its
    duplicate-group siblings (if any) for side-by-side comparison, and
    the families to choose between when the same file sits in several.

    The most likely satellites come first (the filter's score, best first),
    so the junk the filter let through sinks to the end of the queue; images
    it hasn't scored come last."""
    query = f"SELECT id, family, path, duplicate_group_id, filter_score FROM images WHERE {_WAITING}"
    params: tuple = ()
    if family:
        query += " AND family = ?"
        params = (family,)
    query += " ORDER BY filter_score IS NULL, filter_score DESC, id LIMIT ?"
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
                "filter_score": row["filter_score"],
            }
        )
    return queue


def queue_counts(conn: sqlite3.Connection, family: str | None = None) -> dict:
    """How many images wait for review, how many of those the filter hasn't
    screened yet, and how many the filter has set aside."""
    scope, params = (" AND family = ?", (family,)) if family else ("", ())

    def count(where: str) -> int:
        return conn.execute(f"SELECT COUNT(*) FROM images WHERE {where}{scope}", params).fetchone()[0]

    return {
        "waiting": count(_WAITING),
        "unscreened": count(_UNSCREENED),
        "screened_out": count(_SCREENED_OUT),
    }


def decide(
    conn: sqlite3.Connection,
    image_id: int,
    decision: str,
    quarantine_dir: Path = Path("_quarantine"),
    reason: str = "other",
) -> None:
    """decision is one of 'keep', 'reject', 'duplicate'. 'keep' leaves the
    file in place; 'reject'/'duplicate' move it into quarantine_dir, same as
    run_filter's rejects, so quarantine/ stays the one place to look for
    anything no longer in the active set. A reject records `reason` (a
    REJECT_REASONS key)."""
    if decision not in ("keep", "reject", "duplicate"):
        raise ValueError(f"Unknown decision: {decision!r}")
    if decision == "reject" and reason not in REJECT_REASONS:
        raise ValueError(f"Unknown reject reason {reason!r}. Pick one of: {', '.join(REJECT_REASONS)}")

    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise ValueError(f"No image with id {image_id}")

    if decision == "keep" and family_choice(conn, image_id):
        raise ValueError("This file was also found under other families -- choose its family first.")
    if decision == "reject":
        # Same file, same verdict: rejecting one copy rejects them all.
        for copy in identical_copies(conn, image_id):
            quarantine_image(conn, copy["id"], "rejected_filter", quarantine_dir, reason)

    from scraper import master

    if decision == "keep":
        conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), image_id))
        conn.commit()
        master.keep(conn, image_id, by="review")
        return

    if decision == "reject":
        quarantine_image(conn, image_id, "rejected_filter", quarantine_dir, reason)
    else:
        # A look-alike of another image (not the same file): remember it as
        # rejected, so the same file never comes back.
        quarantine_image(conn, image_id, "rejected_duplicate", quarantine_dir)
        master.remember_rejection(conn, image_id, "duplicate")


def decide_many(
    conn: sqlite3.Connection,
    keep: list[int],
    reject: list[int],
    reason: str,
    quarantine_dir: Path = Path("_quarantine"),
) -> dict:
    """A whole page of the queue at once: keep some, reject the rest for one
    reason. An image that needs something more from a person (its family
    chosen first), or that someone already decided, is skipped and stays
    where it is -- one bad row never stops the rest."""
    if reject and reason not in REJECT_REASONS:
        raise ValueError(f"Say why they're rejected: one of {', '.join(REJECT_REASONS)}.")
    if set(keep) & set(reject):
        raise ValueError("An image can't be both kept and rejected.")

    counts = {"kept": 0, "rejected": 0, "skipped": 0}
    for decision, ids, done in (("keep", keep, "kept"), ("reject", reject, "rejected")):
        for image_id in ids:
            row = conn.execute(f"SELECT 1 FROM images WHERE id = ? AND {_WAITING}", (image_id,)).fetchone()
            # A reject takes a file's identical copies with it, so a later id may be gone already.
            if row is None or family_choice(conn, image_id):
                counts["skipped"] += 1
                continue
            decide(conn, image_id, decision, quarantine_dir, reason)
            counts[done] += 1
    return counts


def screened_out(conn: sqlite3.Connection, family: str | None = None, limit: int = 100) -> list[dict]:
    """The images the filter set aside, the closest calls first, so a wrong
    call is quick to spot and undo with keep_after_all(). `by_trained_screener`
    marks the ones the trained screener rejected (scraper/ml/screener.py)."""
    query = (
        "SELECT id, family, filter_score, learned_score,"
        " learned_score IS NOT NULL AND learned_score < 0 AS by_trained_screener"
        f" FROM images WHERE {_SCREENED_OUT}"
    )
    params: tuple = ()
    if family:
        query += " AND family = ?"
        params = (family,)
    query += " ORDER BY filter_score IS NULL, filter_score DESC, id LIMIT ?"
    rows = conn.execute(query, (*params, limit)).fetchall()
    return [{**dict(row), "by_trained_screener": bool(row["by_trained_screener"])} for row in rows]


def keep_after_all(conn: sqlite3.Connection, image_id: int) -> Path:
    """Overrules the filter: an image it set aside goes into the master set
    as kept, and the ledger remembers it as kept."""
    from scraper import master

    row = conn.execute(f"SELECT path FROM images WHERE id = ? AND {_SCREENED_OUT}", (image_id,)).fetchone()
    if row is None:
        raise ValueError(f"Image {image_id} wasn't set aside by the filter.")
    if not Path(row["path"]).is_file():
        raise ValueError(f"Image {image_id}'s file is missing: {row['path']}")
    conn.execute(
        "UPDATE images SET status = 'active', reject_reason = NULL, reviewed_at = ? WHERE id = ?", (_now(), image_id)
    )
    conn.commit()
    return master.keep(conn, image_id, by="review")


def quarantine_image(
    conn: sqlite3.Connection, image_id: int, status: str, quarantine_dir: Path, reason: str | None = None
) -> None:
    """Sets an image aside: the file moves out of the active set (to
    data/_rejected/<family>/ if it was in data/, otherwise quarantine_dir)
    and the decision is remembered in the ledger. An identical copy set
    aside (rejected_duplicate) only adds its source to the one image's
    ledger entry; a rejection is remembered as one ("rejected_before"
    already is)."""
    from scraper import master
    from scraper.ml.dataset import portable_path

    row = conn.execute("SELECT family, path, origin FROM images WHERE id = ?", (image_id,)).fetchone()
    path = Path(row["path"])
    master.image_hashes(conn, image_id)  # while the file is where the record says
    if path.exists():
        dest = master.rejected_destination(conn, row["family"], path, quarantine_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        new_path = portable_path(dest)
    else:
        new_path = row["path"]  # already missing -- just record the decision

    if reason is None and status == "rejected_duplicate":
        reason = "duplicate"
    conn.execute(
        "UPDATE images SET path = ?, status = ?, reviewed_at = ?, reject_reason = ? WHERE id = ?",
        (new_path, status, _now(), reason, image_id),
    )
    conn.commit()
    if status == "rejected_duplicate":
        master.remember_identical_copy(conn, image_id)
    elif reason != "rejected_before":
        master.remember_rejection(conn, image_id, reason or "other")


def quarantine_source(conn: sqlite3.Connection, source_module: str, quarantine_dir: Path, reason: str) -> int:
    """Sets aside every active image downloaded from one source (e.g. an
    off-limits site that was switched on by mistake). Files move to
    quarantine; nothing is deleted. Returns how many were set aside."""
    ids = [
        r["id"]
        for r in conn.execute(
            """SELECT DISTINCT i.id FROM images i JOIN sources s ON s.image_id = i.id
               WHERE s.source_module = ? AND i.status = 'active' AND i.origin = 'scraped'""",
            (source_module,),
        )
    ]
    for image_id in ids:
        quarantine_image(conn, image_id, "rejected_filter", quarantine_dir, reason=reason)
    return len(ids)


def resolve_identical_copies(conn: sqlite3.Connection, ids: list[int], quarantine_dir: Path, prefer=None) -> int:
    """Keeps the best of several identical files and quarantines the rest as
    duplicates. Returns the id kept."""
    keep = best_copy(conn, ids, prefer)
    for image_id in ids:
        if image_id != keep:
            _take_over_provenance(conn, keep, image_id)
            quarantine_image(conn, image_id, "rejected_duplicate", quarantine_dir)
    return keep


def _take_over_provenance(conn: sqlite3.Connection, keep: int, dropped: int) -> None:
    """Same file, same license: when the kept copy has no source/license
    record (an imported image) and the dropped copy does, move it over."""
    for table in ("sources", "licenses"):
        has_own = conn.execute(f"SELECT 1 FROM {table} WHERE image_id = ?", (keep,)).fetchone()
        if not has_own:
            conn.execute(f"UPDATE {table} SET image_id = ? WHERE image_id = ?", (keep, dropped))
    conn.commit()


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
    from scraper import ledger, master

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
        new_path = master.free_path(path.parent.parent / family / path.name)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(new_path))
    conn.execute(
        "UPDATE images SET family = ?, path = ?, duplicate_group_id = NULL WHERE id = ?", (family, str(new_path), keep)
    )
    conn.commit()

    sha, _ = master.image_hashes(conn, keep)
    if sha:
        in_home = master.is_in_home(conn, new_path)
        ledger.set_family(conn, sha, family, ledger.relative_to_data(conn, new_path) if in_home else None)
    return {"kept": keep, "family": family, "quarantined": len(ids) - 1}


# Variants people have added for a family, on top of the catalog's own. The
# catalog's come from split rules in scraper/catalog.py and satellites.csv is
# rebuilt on every refresh, so hand-added ones live in their own file next to
# it (kept in git, like attributes.json) -- a refresh never touches them.
KNOWN_VARIANTS_FILE = "known_variants.json"
MAX_VARIANT_LENGTH = 80


def known_variants_path(catalog: Path) -> Path:
    return catalog.parent / KNOWN_VARIANTS_FILE


def load_known_variants(path: Path) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): [str(v) for v in vs] for k, vs in data.items() if isinstance(vs, list)}


def _catalog_variants(family: str, catalog: Path) -> set[str]:
    variants = set()
    if not catalog.is_file():
        return variants
    with open(catalog, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["FAMILY_NAME"].strip() != family:
                continue
            variant = row.get("VARIANT_NAME", "").strip()
            if variant and variant != family:
                variants.add(variant)
    return variants


def available_variants(family: str, catalog: Path = Path("satellites.csv")) -> list[str]:
    """Distinct variants for `family`, sorted: the catalog's VARIANT_NAME
    values plus any added to known_variants.json. Empty for a family with
    neither -- the review page falls back to free text."""
    known = load_known_variants(known_variants_path(catalog)).get(family, [])
    return sorted(_catalog_variants(family, catalog) | set(known))


def add_known_variant(family: str, variant: str, catalog: Path = Path("satellites.csv")) -> bool:
    """Adds `variant` to `family`'s known variants. False when the family
    already has it (any capitalization), so nothing is written twice."""
    family, variant = (family or "").strip(), " ".join((variant or "").split())
    if not family or not variant:
        raise ValueError("Give both a family and a variant name.")
    if len(variant) > MAX_VARIANT_LENGTH:
        raise ValueError(f"Variant names are {MAX_VARIANT_LENGTH} characters at most.")
    if variant.lower() == family.lower() or variant.lower() in {v.lower() for v in available_variants(family, catalog)}:
        return False
    path = known_variants_path(catalog)
    data = load_known_variants(path)
    data[family] = sorted({*data.get(family, []), variant})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return True


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
