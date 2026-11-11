"""Importing a data/ folder into the database, so imported and scraped images
are one pool: one duplicate scan, one Review queue, one export.

1. Every image under data/images/ is added to the images table with
   origin='imported', its family taken from the file name (the old tool
   named files <family>[_<source>]_<number>), and marked reviewed -- people
   already looked at these. Files stay where they are; re-running only adds
   new ones.
2. Each annotation CSV's answers come in through the old-CSV importer,
   matched by file name against imported images only (scraped files can
   share their names). Rows it skips -- column-shifted, for one -- leave
   their image unannotated, so it comes up on the Annotate page instead.
3. Identical files are merged (dedupe.remove_identical_copies), and an
   imported copy always wins: a scraped copy of the same file is
   quarantined and hands its source/license record to the imported one.
   When the two are filed under different families, Review asks which,
   starting on the imported family.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scraper import ledger
from scraper.ml.dataset import DEFAULT_DATA_DIR, data_sources, family_from_name, portable_path
from scraper.stages import annotate, dedupe
from scraper.stages.annotate_folder import IMAGE_EXTENSIONS
from scraper.stages.embed import file_sha256


def _image_files(image_dirs: list[Path]) -> list[Path]:
    return sorted(
        path
        for directory in image_dirs
        for path in directory.rglob("*")
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def _family_for(conn: sqlite3.Connection, path: Path, image_dirs: list[Path], sha: str) -> str:
    """The family an image in data/ belongs to: the ledger's (it remembers a
    family chosen in Review even when the file name says otherwise), else
    its family folder (data/images/<family>/), else its file name."""
    entry = ledger.entry(conn, sha)
    if entry is not None:
        return entry["family"]
    for directory in image_dirs:
        try:
            relative = path.relative_to(directory)
        except ValueError:
            continue
        if len(relative.parts) > 1:
            return relative.parts[0]
    return family_from_name(path.name)


def _apply_ledger(conn: sqlite3.Connection, new: list[Path], hashes: dict[Path, str], quarantine_dir: Path) -> int:
    """For the images just added: put back each one's source and license
    from the ledger (so a rebuild after a wipe keeps them), record the rest
    as kept, and set aside any the ledger says were rejected. Returns how
    many were set aside."""
    from scraper import master
    from scraper.stages import review

    set_aside = 0
    for path in new:
        row = conn.execute("SELECT id, family FROM images WHERE path = ?", (portable_path(path),)).fetchone()
        if row is None:
            continue
        entry = ledger.entry(conn, hashes[path])
        if entry is not None and entry["state"] == "rejected":
            review.quarantine_image(conn, row["id"], "rejected_filter", quarantine_dir, "rejected_before")
            set_aside += 1
            continue
        for src in ledger.sources_for(conn, hashes[path]):
            taken = conn.execute(
                "SELECT 1 FROM sources WHERE image_id = ? OR (source_module = ? AND external_id = ?)",
                (row["id"], src["source"], src["external_id"]),
            ).fetchone()
            if taken:
                continue
            conn.execute(
                """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (row["id"], src["source"], src["external_id"], src["source_url"] or "", src["retrieved_at"] or ""),
            )
            conn.execute(
                """INSERT INTO licenses (image_id, license_short_name, license_url, author, attribution_required,
                                         credit, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    src["license_short_name"] or "",
                    src["license_url"],
                    src["author"],
                    src["attribution_required"],
                    src["credit"],
                    src["retrieved_at"] or "",
                ),
            )
            break
        conn.commit()
        master.remember_keep(conn, row["id"], by="import")
    return set_aside


def import_data_folder(
    conn: sqlite3.Connection,
    schema: dict,
    data_dir: Path = DEFAULT_DATA_DIR,
    quarantine_dir: Path = Path("_quarantine"),
    dry_run: bool = False,
) -> dict:
    """Adds data/'s images and answers to the database and merges identical
    copies. A dry run only counts what would be added."""
    csvs, image_dirs = data_sources(data_dir)
    if not image_dirs:
        raise ValueError(f"No images/ folder in {data_dir} -- expected {data_dir}/images/.")

    known = {row["path"] for row in conn.execute("SELECT path FROM images")}
    files = _image_files(image_dirs)
    new = [path for path in files if portable_path(path) not in known and str(path) not in known]
    result = {"images_found": len(files), "images_added": len(new), "dry_run": dry_run, "annotations": {}}
    if dry_run:
        return result

    now = datetime.now(timezone.utc).isoformat()
    hashes = {p: file_sha256(p) for p in new}
    with conn:
        conn.executemany(
            """INSERT INTO images (family, path, status, created_at, reviewed_at, content_sha256, origin)
               VALUES (?, ?, 'active', ?, ?, ?, 'imported')""",
            [(_family_for(conn, p, image_dirs, hashes[p]), portable_path(p), now, now, hashes[p]) for p in new],
        )
    result["rejected_before"] = _apply_ledger(conn, new, hashes, quarantine_dir)

    if csvs and not schema:
        raise ValueError("There are no attributes yet -- set up the attribute list before importing annotations.")
    for annotator, csv_path in csvs:
        summary = annotate.import_legacy_csv(conn, csv_path, schema, annotator, review_path=None, origin="imported")
        result["annotations"][annotator] = {
            key: summary[key]
            for key in ("rows", "imported", "already_annotated", "misaligned", "unmatched", "ambiguous")
        }

    result.update(dedupe.remove_identical_copies(conn, quarantine_dir=quarantine_dir))
    return result


def format_result(result: dict) -> list[str]:
    """The import as log lines, for the CLI and the web job."""
    verb = "would add" if result["dry_run"] else "added"
    lines = [f"Images: {result['images_found']} in the data folder, {verb} {result['images_added']} new."]
    if result.get("rejected_before"):
        lines.append(f"{result['rejected_before']} of them were rejected before (per the ledger) and set aside.")
    for annotator, s in result["annotations"].items():
        skipped = ", ".join(
            f"{s[k]} {label}"
            for k, label in (
                ("misaligned", "column-shifted"),
                ("unmatched", "with no image"),
                ("ambiguous", "matching several images"),
                ("already_annotated", "already imported"),
            )
            if s[k]
        )
        lines.append(
            f"Annotations ({annotator}): {s['imported']} of {s['rows']} rows imported"
            + (f"; skipped {skipped}" if skipped else "")
            + "."
        )
    if "identical_copies_removed" in result:
        lines.append(
            f"Identical copies: {result['identical_copies_removed']} merged into one image "
            f"(the extra copies were set aside, not deleted)."
        )
        if result["families_to_choose"]:
            lines.append(
                f"{result['families_to_choose']} file(s) are filed under more than one family -- "
                "Review will ask which family each one belongs to."
            )
    return lines
