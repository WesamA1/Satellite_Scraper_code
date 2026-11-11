"""Bringing in the annotations made on another machine's copy of the project.

Each copy has its own database, so labels saved on an annotator's machine
stay there. This reads that machine's scraper.db and adds its annotations to
this one. Only annotations move: no images, no Review decisions (the
`human_review` name and the `variant` attribute are Review's), no model
guesses.

The other database is never opened in place. It is copied to a temporary
folder first (with its -wal and -shm files when they came along), so the
file on the USB stick is not changed and labels still sitting in its
write-ahead log are read too.

Matching an image there to an image here, since ids differ between machines
and Review moves files:

1. the same path, unless both sides know the file's SHA-256 and they differ;
2. otherwise the same SHA-256 -- if several images here share it (the old
   data filed some photos under two families), the one in the same family,
   then the one that is still active.

An image that matches nothing here, or more than one equally well, is
skipped and listed. Nothing is guessed.

What "merge" means for one annotator's answers to one image:

- this machine has none from that annotator  -> they are added;
- it has the same answers                    -> nothing to do;
- it has different ones                      -> whichever were saved last
  win, all of that annotator's answers for the image together (the same as
  re-submitting on the Annotate page). Answers from different annotators
  never touch each other: names are what keeps people apart, so two people
  who typed the same name are one annotator here.

Values, guide versions and timestamps come across exactly as saved. Before
a merge that changes anything, the database is backed up next to itself.
"""

import csv
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from scraper.stages import annotate

DEFAULT_REVIEW_PATH = Path("export/merge_review.csv")
REVIEW_COLUMNS = ["image", "family", "annotator", "result"]


class MergeError(ValueError):
    pass


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _marks(values: tuple) -> str:
    return ", ".join("?" * len(values))


def _open_copy(other_db: Path, folder: Path) -> sqlite3.Connection:
    """A private copy of the other machine's database, opened on its own."""
    copy = folder / "other.db"
    shutil.copyfile(other_db, copy)
    for suffix in ("-wal", "-shm"):
        sidecar = other_db.with_name(other_db.name + suffix)
        if sidecar.is_file():
            shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    needed = {
        "images": {"id", "path", "family"},
        "annotations": {"image_id", "attribute", "value", "annotator", "guide_version", "created_at"},
    }
    try:
        usable = all(columns <= _columns(conn, table) for table, columns in needed.items())
    except sqlite3.DatabaseError:
        usable = False
    if not usable:
        conn.close()
        raise MergeError(f"{other_db} isn't a Ground Station database (scraper.db) that can be read.")
    return conn


def _their_annotations(other: sqlite3.Connection) -> dict[tuple[int, str], dict]:
    """(their image id, annotator) -> that annotator's answers for the image,
    with the image's path, family and SHA-256 as that machine knows them."""
    sha = "i.content_sha256" if "content_sha256" in _columns(other, "images") else "NULL"
    sets: dict[tuple[int, str], dict] = {}
    for row in other.execute(
        f"""SELECT a.image_id, a.annotator, a.attribute, a.value, a.guide_version, a.created_at,
                   i.path, i.family, {sha} AS sha256
            FROM annotations a JOIN images i ON i.id = a.image_id
            WHERE a.annotator NOT IN ({_marks(annotate.RESERVED_ANNOTATORS)})
              AND a.attribute NOT IN ({_marks(annotate.RESERVED_ATTRIBUTES)})
            ORDER BY a.image_id, a.annotator, a.attribute""",
        (*annotate.RESERVED_ANNOTATORS, *annotate.RESERVED_ATTRIBUTES),
    ):
        entry = sets.setdefault(
            (row["image_id"], row["annotator"]),
            {"path": row["path"], "family": row["family"], "sha256": row["sha256"], "rows": {}, "saved": ""},
        )
        entry["rows"][row["attribute"]] = (row["value"], row["guide_version"], row["created_at"])
        entry["saved"] = max(entry["saved"], row["created_at"] or "")
    return sets


def _find_here(theirs: dict, by_path: dict[str, sqlite3.Row], by_sha: dict[str, list[sqlite3.Row]]):
    """(the image here, "", "") or (None, which count it goes under, why)."""
    same_path = by_path.get(theirs["path"])
    sha = theirs["sha256"]
    if same_path is not None and not (sha and same_path["content_sha256"] and sha != same_path["content_sha256"]):
        return same_path, "", ""
    candidates = by_sha.get(sha, []) if sha else []
    for narrow in (lambda c: c["family"] == theirs["family"], lambda c: c["status"] == "active"):
        if len(candidates) > 1:
            candidates = [c for c in candidates if narrow(c)] or candidates
    if len(candidates) == 1:
        return candidates[0], "", ""
    if candidates:
        return None, "ambiguous", f"{len(candidates)} images here are this same file and nothing tells them apart"
    return None, "unmatched", "this image isn't in this machine's database"


def _database_file(conn: sqlite3.Connection) -> Path | None:
    """The file conn's main database is in; None for one held in memory."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}-before-merge-{stamp}{db_path.suffix}")
    backup = sqlite3.connect(backup_path)
    try:
        conn.backup(backup, name="main")
    finally:
        backup.close()
    return backup_path


def merge_from_database(
    conn: sqlite3.Connection,
    other_db: Path,
    schema: dict | None = None,
    dry_run: bool = True,
    review_path: Path | None = None,
) -> dict:
    """Adds the other machine's annotations to this database (conn), which
    is refused as its own source and backed up next to itself before a merge
    that changes anything. With schema, attributes that aren't in today's
    list are counted (they still come across, as saved). Nothing is written
    on a dry run."""
    if not other_db.is_file():
        raise MergeError(f"No database at {other_db}")
    db_path = _database_file(conn)
    if db_path is not None and other_db.resolve() == db_path.resolve():
        raise MergeError("That is this machine's own database. Point at the copy from the other machine.")

    with tempfile.TemporaryDirectory() as folder:
        other = _open_copy(other_db, Path(folder))
        try:
            theirs = _their_annotations(other)
        finally:
            other.close()

    by_path: dict[str, sqlite3.Row] = {}
    by_sha: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT id, path, family, status, content_sha256 FROM images ORDER BY id"):
        by_path[row["path"]] = row
        if row["content_sha256"]:
            by_sha.setdefault(row["content_sha256"], []).append(row)

    # (image here, annotator) -> {attribute: value} and when the last of them was saved.
    answers: dict[tuple[int, str], dict[str, str]] = {}
    saved_at: dict[tuple[int, str], str] = {}
    for row in conn.execute(
        f"""SELECT image_id, annotator, attribute, value, created_at FROM annotations
            WHERE attribute NOT IN ({_marks(annotate.RESERVED_ATTRIBUTES)})""",
        annotate.RESERVED_ATTRIBUTES,
    ):
        key = (row["image_id"], row["annotator"])
        answers.setdefault(key, {})[row["attribute"]] = row["value"]
        saved_at[key] = max(saved_at.get(key, ""), row["created_at"] or "")

    counts = {"added": 0, "updated": 0, "already_here": 0, "kept_newer_here": 0, "unmatched": 0, "ambiguous": 0}
    annotators: dict[str, dict[str, int]] = {}
    skipped: list[dict] = []
    # What will be written: (image here, annotator) -> that annotator's rows.
    # Keyed, because two images there can be one image here.
    incoming: dict[tuple[int, str], dict[str, tuple]] = {}

    for (_their_id, annotator), entry in theirs.items():
        image, outcome, why_not = _find_here(entry, by_path, by_sha)
        if image is not None:
            key = (image["id"], annotator)
            their_answers = {attribute: value for attribute, (value, _, _) in entry["rows"].items()}
            if key not in answers:
                outcome = "added"
            elif answers[key] == their_answers:
                outcome = "already_here"
            elif entry["saved"] > saved_at[key]:
                outcome = "updated"
            else:
                outcome = "kept_newer_here"
                why_not = "this machine has different answers from the same annotator, saved later; kept those"
            if outcome in ("added", "updated"):
                incoming[key] = entry["rows"]
                answers[key], saved_at[key] = their_answers, entry["saved"]
        if why_not:
            skipped.append(
                {"image": entry["path"], "family": entry["family"], "annotator": annotator, "result": why_not}
            )
        counts[outcome] += 1
        tally = annotators.setdefault(annotator, dict.fromkeys(counts, 0))
        tally[outcome] += 1

    known = set(annotate.flat_attributes(schema)) if schema else None
    unknown_attributes: dict[str, int] = {}
    for rows in incoming.values():
        for attribute in rows:
            if known is not None and attribute not in known:
                unknown_attributes[attribute] = unknown_attributes.get(attribute, 0) + 1

    backup_path = None
    if not dry_run and incoming:
        if db_path is not None:
            backup_path = _backup(conn, db_path)
        with conn:
            conn.executemany(
                f"""DELETE FROM annotations WHERE image_id = ? AND annotator = ?
                    AND attribute NOT IN ({_marks(annotate.RESERVED_ATTRIBUTES)})""",
                [(image_id, annotator, *annotate.RESERVED_ATTRIBUTES) for image_id, annotator in incoming],
            )
            conn.executemany(
                """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (image_id, attribute, value, annotator, guide_version, created_at)
                    for (image_id, annotator), rows in incoming.items()
                    for attribute, (value, guide_version, created_at) in rows.items()
                ],
            )

    wrote_review = review_path is not None and bool(skipped) and not dry_run
    if wrote_review:
        review_path.parent.mkdir(parents=True, exist_ok=True)
        with open(review_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
            writer.writeheader()
            writer.writerows(skipped)

    return {
        "sets": len(theirs),
        **counts,
        "annotators": annotators,
        "unknown_attributes": unknown_attributes,
        "skipped": skipped,
        "dry_run": dry_run,
        "backup_path": str(backup_path) if backup_path else None,
        "review_path": str(review_path) if wrote_review else None,
    }
