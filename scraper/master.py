"""The master set: every kept image, once, in one home -- data/images/<family>/
-- with the ledger (scraper/ledger.py) remembering every decision.

How images get there:
- A scrape (or "add images" from an outside source) puts new files in the
  inbox, satellite_images/<family>/. Each one is checked against the ledger
  as it arrives (check_arrival): rejected before -> set aside; already in the
  master set -> recorded as another source, no second copy.
- Review's Keep moves the file into data/images/<family>/ (move_into_home)
  and records it as kept. Rejects go to quarantine and are remembered.
- Rejected files that were in data/ move to data/_rejected/<family>/, not
  _quarantine/ -- that one is emptied by a wipe, and data/ is the team's
  copy of record.
"""

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scraper import ledger
from scraper.ml.dataset import portable_path

REJECTED_DIR = "_rejected"  # under data/

# Who decided, from the reason a rejection carries.
_DECIDED_BY = {
    "filter": "filter",
    "not_an_image": "download check",
    "off_limits_source": "off-limits cleanup",
    "duplicate": "review",
}


def free_path(dest: Path) -> Path:
    candidate, n = dest, 2
    while candidate.exists():
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    return candidate


class _Resolver:
    """Path.resolve() for many files in few folders. resolve() checks every
    part of a path on disk, which adds up over thousands of images; this
    resolves each folder once and reuses it for every file in it. (A file
    that is itself a link isn't followed -- images here never are.)"""

    def __init__(self) -> None:
        self._folders: dict[str, Path] = {}

    def __call__(self, path: Path | str) -> Path:
        folder, name = os.path.split(os.fspath(path))
        resolved = self._folders.get(folder)
        if resolved is None:
            resolved = self._folders[folder] = Path(folder or ".").resolve()
        return resolved / name


def image_hashes(conn: sqlite3.Connection, image_id: int) -> tuple[str | None, str | None]:
    """(SHA-256, perceptual hash) of an image's file, worked out and stored
    on the image the first time they're needed."""
    from scraper.stages.dedupe import compute_phash
    from scraper.stages.embed import file_sha256

    row = conn.execute("SELECT path, content_sha256, perceptual_hash FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        return None, None
    sha, phash = row["content_sha256"], row["perceptual_hash"]
    path = Path(row["path"])
    if (sha is None or phash is None) and path.is_file():
        sha = sha or file_sha256(path)
        phash = phash or compute_phash(path)
        conn.execute("UPDATE images SET content_sha256 = ?, perceptual_hash = ? WHERE id = ?", (sha, phash, image_id))
        conn.commit()
    return sha, phash


def is_in_data(conn: sqlite3.Connection, path: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(ledger.data_dir(conn).resolve())
        return True
    except ValueError:
        return False


def is_in_home(conn: sqlite3.Connection, path: Path | str) -> bool:
    try:
        Path(path).resolve().relative_to(ledger.home(conn).resolve())
        return True
    except ValueError:
        return False


def rejected_destination(conn: sqlite3.Connection, family: str, path: Path, quarantine_dir: Path) -> Path:
    """Where a rejected file goes: data/_rejected/<family>/ for a file in
    data/, otherwise the quarantine folder."""
    base = ledger.data_dir(conn) / REJECTED_DIR if is_in_data(conn, path) else quarantine_dir
    return free_path(base / family / path.name)


def remember_rejection(conn: sqlite3.Connection, image_id: int, reason: str, by: str | None = None) -> None:
    from scraper.stages.review import REJECT_REASONS

    sha, phash = image_hashes(conn, image_id)
    if sha is None:
        return
    family = conn.execute("SELECT family FROM images WHERE id = ?", (image_id,)).fetchone()["family"]
    who = by or ("review" if reason in REJECT_REASONS else _DECIDED_BY.get(reason, reason))
    ledger.reject(conn, sha, family, reason, who, phash)
    ledger.add_source(conn, sha, ledger.source_from_db(conn, image_id))


def remember_identical_copy(conn: sqlite3.Connection, image_id: int) -> None:
    """A byte-identical copy set aside: its source is one more source for the
    one image (the ledger entry's decision doesn't change)."""
    sha, _ = image_hashes(conn, image_id)
    if sha:
        ledger.add_source(conn, sha, ledger.source_from_db(conn, image_id))


def move_into_home(conn: sqlite3.Connection, image_id: int) -> Path:
    """Moves a kept image's file into data/images/<family>/ (if it isn't
    there already) and points the image at it. Returns the path."""
    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    path = Path(row["path"])
    target_dir = ledger.home(conn) / row["family"]
    if not path.is_file() or path.resolve().parent == target_dir.resolve():
        return path
    dest = free_path(target_dir / path.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    new_path = portable_path(dest)
    conn.execute("UPDATE images SET path = ? WHERE id = ?", (new_path, image_id))
    conn.commit()
    return Path(new_path)


def remember_keep(conn: sqlite3.Connection, image_id: int, by: str = "review") -> None:
    sha, phash = image_hashes(conn, image_id)
    if sha is None:
        return
    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    in_home = is_in_home(conn, row["path"])
    ledger.keep(conn, sha, row["family"], ledger.relative_to_data(conn, row["path"]) if in_home else None, by, phash)
    ledger.add_source(conn, sha, ledger.source_from_db(conn, image_id))


def keep(conn: sqlite3.Connection, image_id: int, by: str = "review") -> Path:
    """Into the master set: move the file home and remember it as kept."""
    path = move_into_home(conn, image_id)
    remember_keep(conn, image_id, by)
    return path


def put_back(conn: sqlite3.Connection, image_id: int, family: str | None = None, by: str = "put back") -> Path:
    """Undoes a set-aside: an image in data/_rejected/ goes back into the
    master set (data/images/<family>/), active again, and the ledger
    remembers it as kept -- so the same picture arriving later counts as
    already in the set, not as rejected before. `family` files it under
    another family than the one on record."""
    row = conn.execute("SELECT family, path, status FROM images WHERE id = ?", (image_id,)).fetchone()
    if row is None:
        raise ValueError(f"No image with id {image_id}")
    if row["status"] == "active":
        raise ValueError(f"Image {image_id} isn't set aside.")
    path = Path(row["path"])
    if not path.is_file():
        raise ValueError(f"Image {image_id}'s file is missing: {path}")
    if not is_in_data(conn, path):
        raise ValueError(f"Image {image_id} wasn't set aside from the master set (its file isn't in data/).")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE images SET status = 'active', reject_reason = NULL, family = ?,
                  reviewed_at = COALESCE(reviewed_at, ?) WHERE id = ?""",
        (family or row["family"], now, image_id),
    )
    conn.commit()
    return keep(conn, image_id, by)


def check_arrival(conn: sqlite3.Connection, image_id: int, quarantine_dir: Path) -> str:
    """A just-downloaded (or just-added) image against the ledger. Returns
    the ledger's verdict (ledger.check_new); "rejected_before" and
    "already_kept" images are set aside here, with their source recorded."""
    from scraper.stages import review

    sha, phash = image_hashes(conn, image_id)
    if sha is None:
        return "new"
    row = conn.execute("SELECT family, path FROM images WHERE id = ?", (image_id,)).fetchone()
    verdict = ledger.check_new(
        conn, sha, row["family"], phash, Path(row["path"]).name, ledger.source_from_db(conn, image_id)
    )
    if verdict == "rejected_before":
        review.quarantine_image(conn, image_id, "rejected_filter", quarantine_dir, "rejected_before")
    elif verdict == "already_kept":
        review.quarantine_image(conn, image_id, "rejected_duplicate", quarantine_dir, "already_in_master_set")
    elif verdict == "waiting":
        # The same file is already waiting for review: under the same family,
        # one copy is enough. Under another family, Review asks which.
        same = [
            r["id"]
            for r in conn.execute(
                """SELECT id FROM images WHERE content_sha256 = ? AND family = ? AND status = 'active' AND id != ?""",
                (sha, row["family"], image_id),
            )
        ]
        if same:
            kept = review.resolve_identical_copies(conn, same + [image_id], quarantine_dir)
            if kept != image_id:
                return "already_waiting"
    return verdict


def _image_files(folder: Path) -> list[Path]:
    from scraper.stages.annotate_folder import IMAGE_EXTENSIONS

    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def _rows_by_file(conn: sqlite3.Connection, resolve: _Resolver) -> dict[Path, sqlite3.Row]:
    rows = {}
    for row in conn.execute("SELECT id, family, path, status, content_sha256 FROM images"):
        rows[resolve(row["path"])] = row
    return rows


def organize_home(conn: sqlite3.Connection, dry_run: bool = True) -> dict:
    """Sorts data/images/ into one folder per family: every image file not
    already in its family's folder moves there. The family is the
    database's when the file is in it, otherwise the one in its file name
    (<family>[_<source>]_<number>). Images, ledger paths and file names
    otherwise stay the same, so annotations still match. A dry run only
    counts."""
    from scraper.ml.dataset import family_from_name

    home_dir = ledger.home(conn)
    home_resolved = home_dir.resolve()
    resolve = _Resolver()
    rows = _rows_by_file(conn, resolve)
    files = _image_files(home_dir)
    moves = []
    for path in files:
        resolved = resolve(path)
        row = rows.get(resolved)
        family = row["family"] if row is not None else family_from_name(path.name)
        if resolved.parent != home_resolved / family:
            moves.append((path, family, row))
    result = {"files": len(files), "to_move": len(moves), "families": len({m[1] for m in moves})}
    result["dry_run"] = dry_run
    if dry_run:
        return result
    for path, family, row in moves:
        dest = free_path(home_dir / family / path.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        if row is not None:
            conn.execute("UPDATE images SET path = ? WHERE id = ?", (portable_path(dest), row["id"]))
            if row["content_sha256"]:
                ledger.set_family(conn, row["content_sha256"], row["family"], ledger.relative_to_data(conn, dest))
    conn.commit()
    result["moved"] = len(moves)
    return result


# ---- The sorting check ----

ISSUE_LABELS = {
    "wrong_folder": "In data/images/ but not in its family's folder",
    "outside_home": "Kept in Review but not in data/images/ yet",
    "two_families": "The same image kept under two families",
    "stored_twice": "The same image stored twice in one family",
    "untracked": "Files in data/images/ the database doesn't know",
    "not_in_ledger": "Images the ledger hasn't recorded yet",
    "missing_file": "Images whose file is missing",
    "license_unrecorded": "Kept images with no license on record",
}
FIXABLE = {"wrong_folder", "outside_home", "stored_twice", "untracked", "not_in_ledger"}
MAX_LISTED = 50


def check_home(conn: sqlite3.Connection) -> dict:
    """Everything that keeps the master set from being one copy of each
    image, filed under the right family, with its decision on record.
    Returns {kind: {"count", "items" (up to MAX_LISTED), "fixable"}}."""
    home_dir = ledger.home(conn)
    home_resolved = home_dir.resolve()
    resolve = _Resolver()
    active = conn.execute(
        "SELECT id, family, path, reviewed_at, content_sha256, origin FROM images WHERE status = 'active'"
    ).fetchall()
    known_files = {resolve(r["path"]) for r in conn.execute("SELECT path FROM images")}
    ledger_states = {r["sha256"]: r["state"] for r in conn.execute("SELECT sha256, state FROM ledger.ledger_entries")}
    issues: dict[str, list[dict]] = {kind: [] for kind in ISSUE_LABELS}

    by_sha: dict[str, list[sqlite3.Row]] = {}
    for row in active:
        path = Path(row["path"])
        item = {"id": row["id"], "family": row["family"], "path": row["path"]}
        if not path.is_file():
            issues["missing_file"].append(item)
            continue
        resolved = resolve(path)
        if resolved.is_relative_to(home_resolved):
            if resolved.parent != home_resolved / row["family"]:
                issues["wrong_folder"].append(item)
        elif row["reviewed_at"] is not None:
            issues["outside_home"].append(item)
        if row["content_sha256"]:
            by_sha.setdefault(row["content_sha256"], []).append(row)
            state = ledger_states.get(row["content_sha256"])
            if state is None or (row["reviewed_at"] is not None and state != "kept"):
                issues["not_in_ledger"].append(item)
        else:
            issues["not_in_ledger"].append(item)

    for sha, copies in by_sha.items():
        if len(copies) < 2:
            continue
        families = sorted({c["family"] for c in copies})
        item = {
            "sha256": sha,
            "ids": [c["id"] for c in copies],
            "families": families,
            "paths": [c["path"] for c in copies],
        }
        issues["two_families" if len(families) > 1 else "stored_twice"].append(item)

    for path in _image_files(home_dir):
        if resolve(path) not in known_files:
            issues["untracked"].append({"path": portable_path(path)})

    for row in conn.execute(
        f"""SELECT e.sha256, e.family, e.path FROM ledger.ledger_entries e WHERE e.state = 'kept' AND NOT EXISTS (
             SELECT 1 FROM ledger.ledger_sources s WHERE s.sha256 = e.sha256 AND {ledger.HAS_LICENSE})
           ORDER BY e.family"""
    ):
        issues["license_unrecorded"].append({"sha256": row["sha256"], "family": row["family"], "path": row["path"]})

    return {
        kind: {
            "label": ISSUE_LABELS[kind],
            "count": len(items),
            "items": items[:MAX_LISTED],
            "fixable": kind in FIXABLE,
        }
        for kind, items in issues.items()
    }


def sync_ledger(conn: sqlite3.Connection, by: str = "sync") -> dict:
    """Records every image in the database in the ledger, as it stands:
    kept (reviewed), waiting (not reviewed yet) or rejected (with its
    reason). Identical copies set aside only add their source. Used to seed
    a fresh ledger, and to fix "not in the ledger"."""
    counts = {"kept": 0, "waiting": 0, "rejected": 0, "copies": 0, "no_file": 0}
    rows = conn.execute("SELECT id, family, path, status, reviewed_at, reject_reason, content_sha256 FROM images")
    for row in rows.fetchall():
        sha, phash = image_hashes(conn, row["id"])
        if sha is None:
            counts["no_file"] += 1
            continue
        source = ledger.source_from_db(conn, row["id"])
        if row["status"] == "active":
            if row["reviewed_at"] is not None:
                in_home = is_in_home(conn, row["path"])
                path = ledger.relative_to_data(conn, row["path"]) if in_home else None
                ledger.keep(conn, sha, row["family"], path, by, phash)
                counts["kept"] += 1
            else:
                ledger.check_new(conn, sha, row["family"], phash, Path(row["path"]).name, source)
                counts["waiting"] += 1
            ledger.add_source(conn, sha, source)
            continue
        identical_active = conn.execute(
            "SELECT 1 FROM images WHERE content_sha256 = ? AND status = 'active' AND id != ?", (sha, row["id"])
        ).fetchone()
        if identical_active:
            # The copy in the set decides: this one only adds its source
            # (an identical copy set aside, or a rejection since put back).
            ledger.add_source(conn, sha, source)
            counts["copies"] += 1
        elif row["reject_reason"] in ("rejected_before", "already_in_master_set"):
            ledger.add_source(conn, sha, source)
        else:
            ledger.reject(conn, sha, row["family"], row["reject_reason"] or "unrecorded", by, phash)
            ledger.add_source(conn, sha, source)
            counts["rejected"] += 1
    return counts


def fix(conn: sqlite3.Connection, kind: str, quarantine_dir: Path = Path("_quarantine")) -> dict:
    """Fixes every problem of one kind from check_home. Nothing is deleted:
    files are moved, and extra copies set aside."""
    from scraper.stages import dedupe

    if kind not in FIXABLE:
        raise ValueError(f"'{kind}' can't be fixed automatically.")
    if kind == "wrong_folder":
        return organize_home(conn, dry_run=False)
    if kind == "outside_home":
        moved = 0
        rows = conn.execute("SELECT id, path FROM images WHERE status = 'active' AND reviewed_at IS NOT NULL")
        for row in rows.fetchall():
            if Path(row["path"]).is_file() and not is_in_home(conn, row["path"]):
                keep(conn, row["id"], by="sorting check")
                moved += 1
        return {"moved": moved}
    if kind == "stored_twice":
        return dedupe.remove_identical_copies(conn, quarantine_dir=quarantine_dir)
    if kind == "untracked":
        from scraper.stages import annotate, import_data

        return import_data.import_data_folder(
            conn, annotate.load_schema(annotate.DEFAULT_SCHEMA_PATH), ledger.data_dir(conn), quarantine_dir
        )
    return sync_ledger(conn, by="sorting check")
