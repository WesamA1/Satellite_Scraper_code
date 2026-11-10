"""Adding images from an outside source (a folder someone hands you, a batch
from another archive) to the master set, with their license recorded.

Each image is copied -- never moved -- into the inbox
(satellite_images/<family>/) and goes through the same checks as a scraped
download: rejected before -> set aside; already in the master set ->
recorded as another source, no second copy. New ones wait in Review, or go
straight into the master set with `already_reviewed`.

What every image needs:
- a family: one for the whole batch, a subfolder per family
  (<folder>/<FAMILY>/<file>), or a "family" column in a manifest;
- a license: one for the whole batch, or a "license" column in a manifest.
  An image without a license isn't added.

A manifest is an optional CSV with a "file" column (the file's name, or its
path inside the folder) and any of: family, source_url, license,
license_url, author, credit. Its values win over the batch's.
"""

import csv
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scraper import license_gate, master
from scraper.ml.dataset import portable_path
from scraper.sources.base import ImageRecord
from scraper.stages import review
from scraper.stages.download import _free_dest, _is_image

MANIFEST_FIELDS = ("family", "source_url", "license", "license_url", "author", "credit")


def source_key(source_name: str) -> str:
    """The source name as recorded: outside:<slug>."""
    slug = re.sub(r"[^a-z0-9]+", "_", source_name.lower()).strip("_")
    if not slug:
        raise ValueError("Give the outside source a name.")
    return f"outside:{slug}"


def _load_manifest(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "file" not in [n.strip().lower() for n in reader.fieldnames]:
            raise ValueError(f"{path.name} needs a 'file' column.")
        rows = {}
        for row in reader:
            clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if clean.get("file"):
                rows[clean["file"].replace("\\", "/")] = clean
    return rows


def add_folder(
    conn: sqlite3.Connection,
    folder: Path,
    source_name: str,
    license_short_name: str = "",
    license_url: str = "",
    author: str = "",
    credit: str = "",
    source_url: str = "",
    family: str | None = None,
    manifest: Path | None = None,
    already_reviewed: bool = False,
    inbox: Path = Path("satellite_images"),
    quarantine_dir: Path = Path("_quarantine"),
    catalog: Path = Path("satellites.csv"),
    dry_run: bool = False,
) -> dict:
    """Adds every image under `folder`. Returns counts of what happened."""
    from scraper.families import load_families

    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"{folder} isn't a folder.")
    source = source_key(source_name)
    rows = _load_manifest(Path(manifest) if manifest else None)
    known_families = set(load_families(catalog)) if catalog.is_file() else set()
    files = master._image_files(folder)

    counts = {
        "found": len(files),
        "added": 0,
        "kept": 0,
        "rejected_before": 0,
        "already_in_master_set": 0,
        "already_added": 0,
        "no_family": 0,
        "no_license": 0,
        "not_an_image": 0,
        "needs_family_choice": 0,
        "dry_run": dry_run,
    }
    unknown_families: set[str] = set()
    now = datetime.now(timezone.utc).isoformat()

    for path in files:
        relative = path.relative_to(folder).as_posix()
        details = rows.get(relative) or rows.get(path.name) or {}
        fam = (details.get("family") or family or (relative.split("/")[0] if "/" in relative else "")).strip()
        lic = details.get("license") or license_short_name.strip()
        if not fam:
            counts["no_family"] += 1
            continue
        if not lic:
            counts["no_license"] += 1
            continue
        if license_gate.is_already_fetched(conn, source, relative):
            counts["already_added"] += 1
            continue
        if not _is_image(path):
            counts["not_an_image"] += 1
            continue
        if known_families and fam not in known_families:
            unknown_families.add(fam)
        if dry_run:
            counts["added"] += 1
            continue

        dest = _free_dest(conn, inbox / fam / path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        record = ImageRecord(
            family=fam,
            source_module=source,
            external_id=relative,
            source_url=details.get("source_url") or source_url or "",
            download_url="",
            title=path.stem,
            description="",
            author=details.get("author") or author or None,
            license_short_name=lic,
            license_url=details.get("license_url") or license_url or None,
            attribution_required=None,
            credit=details.get("credit") or credit or None,
            retrieved_at=now,
            mime="",
        )
        image_id = license_gate.persist(conn, record, license_gate.GateResult(True, None), portable_path(dest))
        verdict = master.check_arrival(conn, image_id, quarantine_dir)
        if verdict == "rejected_before":
            counts["rejected_before"] += 1
            continue
        if verdict in ("already_kept", "already_waiting"):
            counts["already_in_master_set"] += 1
            continue
        counts["added"] += 1
        if already_reviewed:
            try:
                review.decide(conn, image_id, "keep", quarantine_dir)
                counts["kept"] += 1
            except ValueError:  # the same file is waiting under another family: Review asks which
                counts["needs_family_choice"] += 1

    counts["unknown_families"] = sorted(unknown_families)
    return counts


def format_result(result: dict) -> str:
    verb = "Would add" if result["dry_run"] else "Added"
    lines = [f"{verb} {result['added']} of {result['found']} image(s)."]
    if result["kept"]:
        lines.append(f"{result['kept']} went straight into the master set (marked as already reviewed).")
    skipped = [
        (result["rejected_before"], "rejected before"),
        (result["already_in_master_set"], "already in the master set (recorded as another source)"),
        (result["already_added"], "added from this source before"),
        (result["no_family"], "no family (use a family, subfolders or a manifest)"),
        (result["no_license"], "no license"),
        (result["not_an_image"], "not readable as an image"),
        (result["needs_family_choice"], "also waiting under another family (Review asks which)"),
    ]
    for n, why in skipped:
        if n:
            lines.append(f"  {n}: {why}")
    if result["unknown_families"]:
        names = ", ".join(result["unknown_families"][:10])
        lines.append(f"Not in the satellite catalog (added anyway, check the names): {names}")
    return "\n".join(lines)
