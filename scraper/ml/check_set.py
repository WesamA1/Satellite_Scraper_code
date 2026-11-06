"""A hand-labeled check set for image screening.

To compare screening models fairly (OWLv2 now, a vision-language model
later) they need to be scored against what a person says, not against
someone eyeballing a model's own picks. A check set is a fixed random sample
of active images with two human answers each:

- satellites: how many separate satellites are in the image -- "0", "1"
  or "2+" (the dataset wants exactly one);
- recognizable: for a single satellite, would someone recognize it ("yes"),
  or is it half-built, too small or too blurry to tell ("no").

It lives in data/check_sets/ as a CSV keyed by each image's SHA-256, so it
travels with the data folder to whichever machine compares models, and a
moved image still lines up. Creating one never overwrites existing labels.
"""

import csv
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scraper.ml.dataset import DEFAULT_DATA_DIR, portable_path
from scraper.stages.embed import file_sha256

DEFAULT_PATH = DEFAULT_DATA_DIR / "check_sets" / "screening.csv"
DEFAULT_SIZE = 200
FIELDS = ["sha256", "image", "satellites", "recognizable", "labeled_by", "labeled_at"]
COUNTS = ("0", "1", "2+")
RECOGNIZABLE = ("yes", "no")


class CheckSetError(ValueError):
    pass


def load(path: Path = DEFAULT_PATH) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{field: row.get(field, "") for field in FIELDS} for row in csv.DictReader(f)]


def _save(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _readable(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def create(conn: sqlite3.Connection, path: Path = DEFAULT_PATH, size: int = DEFAULT_SIZE, seed: int = 0) -> dict:
    """A random sample of `size` active, readable images (one per distinct
    file). Refuses to replace a check set that already exists."""
    if path.exists():
        raise CheckSetError(f"{path} already exists -- labels are never overwritten. Delete it to start over.")
    if not 1 <= size <= 2000:
        raise CheckSetError("A check set has between 1 and 2,000 images.")
    candidates = [Path(row["path"]) for row in conn.execute("SELECT path FROM images WHERE status = 'active'")]
    random.Random(seed).shuffle(candidates)

    rows, seen = [], set()
    for image in candidates:
        if len(rows) == size:
            break
        if not image.is_file() or not _readable(image):
            continue
        sha = file_sha256(image)
        if sha in seen:
            continue
        seen.add(sha)
        rows.append({**dict.fromkeys(FIELDS, ""), "sha256": sha, "image": portable_path(image)})
    if not rows:
        raise CheckSetError("No readable active images to sample from.")
    _save(rows, path)
    return status(path)


def status(path: Path = DEFAULT_PATH) -> dict:
    rows = load(path)
    return {
        "exists": bool(rows),
        "path": path.as_posix(),
        "total": len(rows),
        "labeled": sum(1 for r in rows if r["satellites"]),
        "items": rows,
    }


def label(
    sha256: str, satellites: str, recognizable: str = "", labeled_by: str = "", path: Path = DEFAULT_PATH
) -> dict:
    """Records (or corrects) one image's answers. `recognizable` is only
    asked when there's exactly one satellite."""
    if satellites not in COUNTS:
        raise CheckSetError(f"satellites has to be one of {', '.join(COUNTS)}.")
    if satellites == "1" and recognizable not in RECOGNIZABLE:
        raise CheckSetError("Say whether the satellite is recognizable (yes or no).")
    rows = load(path)
    row = next((r for r in rows if r["sha256"] == sha256), None)
    if row is None:
        raise CheckSetError("That image isn't in the check set.")
    row.update(
        satellites=satellites,
        recognizable=recognizable if satellites == "1" else "",
        labeled_by=(labeled_by or "").strip()[:64],
        labeled_at=datetime.now(timezone.utc).isoformat(),
    )
    _save(rows, path)
    return {key: value for key, value in status(path).items() if key != "items"}


def image_path(sha256: str, path: Path = DEFAULT_PATH) -> Path:
    """The file for a check-set image -- only ever one listed in the set."""
    row = next((r for r in load(path) if r["sha256"] == sha256), None)
    if row is None or not Path(row["image"]).is_file():
        raise CheckSetError("No such image in the check set.")
    return Path(row["image"])
