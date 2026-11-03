"""Folder mode for the annotate stage: a drop-in for the old PyQt tool's setup.

Same three settings the old Setup screen wrote to metadata.json (imageSource,
imageDestination, CSVDest) and the same behavior on submit: append one row to
the CSV, then move the image out of imageSource into imageDestination. Since
the old tool tracked progress by exactly that move, whatever is still in the
image folder is what's left -- pointing this at the old folders picks up
wherever the old tool stopped, with nothing to import.

Two deliberate differences from the old SubmitMenu.writeToCSV:
- Rows are written in the existing CSV header's column order (old column
  names like "Antennae" included), not in whatever order the attributes
  happen to be in. The old writer reused the first row's header for every
  later row, which put values in the wrong columns once attributes changed.
- An attribute with no column yet gets one: the file is rewritten once with
  the extra column (earlier rows left blank there) rather than silently
  shifting every later row.
"""

import csv
import json
import shutil
from pathlib import Path

from scraper.stages import annotate

SOURCE_CONFIG_PATH = Path("annotation_source.json")
MODES = ("database", "folder")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".avif"}

DEFAULT_CONFIG = {
    "mode": "database",
    "imageSource": "",
    "imageDestination": "",
    "CSVDest": "",
    "attributesPath": "",
}

# Current attribute name -> the old CSV's column name for it.
_OLD_COLUMN_NAMES = {new: old for old, new in annotate.LEGACY_COLUMN_ALIASES.items()}


class FolderError(ValueError):
    pass


# ---- Settings ----


def load_source_config(path: Path = SOURCE_CONFIG_PATH) -> dict:
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return dict(DEFAULT_CONFIG)
    return {key: str(data.get(key, default) or default) for key, default in DEFAULT_CONFIG.items()}


def validate_source_config(data: dict) -> dict:
    config = {key: str(data.get(key, default) or "").strip() for key, default in DEFAULT_CONFIG.items()}
    if config["mode"] not in MODES:
        raise FolderError(f"Unknown mode '{config['mode']}'.")

    if config["attributesPath"]:
        attributes = Path(config["attributesPath"]).expanduser()
        if not attributes.is_file():
            raise FolderError(f"No attribute file at {attributes}")
        try:
            annotate.load_schema(attributes)
        except annotate.SchemaError as e:
            raise FolderError(str(e)) from e

    if config["mode"] == "folder":
        source = Path(config["imageSource"]).expanduser()
        destination = Path(config["imageDestination"]).expanduser()
        csv_path = Path(config["CSVDest"]).expanduser()
        if not config["imageSource"] or not source.is_dir():
            raise FolderError(f"Image folder not found: {config['imageSource'] or '(blank)'}")
        if not config["imageDestination"]:
            raise FolderError("Set the annotated image folder.")
        if destination.resolve() == source.resolve():
            raise FolderError("The annotated image folder has to be different from the image folder.")
        if destination.exists() and not destination.is_dir():
            raise FolderError(f"Annotated image folder is a file, not a folder: {destination}")
        if not config["CSVDest"] or csv_path.is_dir():
            raise FolderError("Set the CSV file annotations are written to.")
        if not csv_path.parent.is_dir():
            raise FolderError(f"The CSV's folder doesn't exist: {csv_path.parent}")
    return config


def save_source_config(data: dict, path: Path = SOURCE_CONFIG_PATH) -> dict:
    config = validate_source_config(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    tmp.replace(path)
    return config


def read_old_tool_settings(location: Path) -> dict:
    """Settings from the old tool's own files: its metadata.json (the three
    folder paths) and, when it sits next to it, its attributes.json.
    `location` is the folder the old tool ran from, or metadata.json itself."""
    location = location.expanduser()
    metadata = location if location.is_file() else location / "metadata.json"
    if not metadata.is_file():
        raise FolderError(f"No metadata.json at {location} -- point this at the folder the old tool ran from.")
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise FolderError(f"{metadata} isn't valid JSON: {e}") from e

    attributes = metadata.parent / "attributes.json"
    return {
        "mode": "folder",
        "imageSource": str(data.get("imageSource", "")),
        "imageDestination": str(data.get("imageDestination", "")),
        "CSVDest": str(data.get("CSVDest", "")),
        "attributesPath": str(attributes) if attributes.is_file() else "",
    }


def attributes_path(config: dict, default: Path = annotate.DEFAULT_SCHEMA_PATH) -> Path:
    return Path(config["attributesPath"]).expanduser() if config.get("attributesPath") else default


# ---- Queue + images ----


def _is_image(path: Path) -> bool:
    # Hidden/system files (.DS_Store, ._ resource forks, Thumbs.db) are skipped
    # rather than moved aside the way the old tool's get_next_image did.
    return path.is_file() and not path.name.startswith(".") and path.suffix.lower() in IMAGE_EXTENSIONS


def folder_queue(config: dict, limit: int = 50) -> dict:
    source = Path(config["imageSource"]).expanduser()
    if not source.is_dir():
        raise FolderError(f"Image folder not found: {source}")
    names = sorted((p.name for p in source.iterdir() if _is_image(p)), key=str.lower)
    return {"remaining": len(names), "queue": [{"name": n, "family": ""} for n in names[:limit]]}


def image_path(config: dict, name: str) -> Path:
    """The file `name` in the image folder -- only ever a direct child of it,
    so a crafted name can't reach anything else on disk."""
    source = Path(config["imageSource"]).expanduser().resolve()
    candidate = (source / name).resolve()
    if candidate.parent != source or not _is_image(candidate):
        raise FolderError(f"No image named {name!r} in the image folder.")
    return candidate


# ---- CSV ----


def _encoding(path: Path) -> str:
    """The old tool wrote its CSV in the OS default encoding (cp1252 on the
    Windows machine). Keep appending in whatever the file already uses."""
    if not path.exists():
        return "utf-8"
    try:
        path.read_bytes().decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def _column_for(attribute: str, header: list[str]) -> str | None:
    if attribute in header:
        return attribute
    old = _OLD_COLUMN_NAMES.get(attribute)
    if old in header:
        return old
    by_lower = {h.lower(): h for h in header}
    return by_lower.get(attribute.lower())


def append_csv_row(csv_path: Path, name: str, values: dict, attribute_order: list[str]) -> None:
    encoding = _encoding(csv_path)
    rows: list[dict] = []
    header: list[str] = []
    if csv_path.exists() and csv_path.stat().st_size > 0:
        with open(csv_path, newline="", encoding=encoding) as f:
            reader = csv.DictReader(f)
            header = list(reader.fieldnames or [])
            rows = list(reader)

    if not header:
        header = ["name", *attribute_order]  # what the old tool wrote for a new file
    elif "name" not in header:
        raise FolderError(f"{csv_path} has no 'name' column -- is it the old tool's annotation CSV?")

    columns = {}
    missing = []
    for attribute in attribute_order:
        column = _column_for(attribute, header)
        if column is None:
            missing.append(attribute)
            column = attribute
        columns[attribute] = column

    row = {"name": name, **{columns[a]: annotate.csv_cell(values[a]) for a in attribute_order}}

    if missing and rows:
        # New attribute since this CSV was started: add its column once,
        # rewriting the file, instead of misaligning every row after this one.
        header = [*header, *missing]
        tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
        with open(tmp, "w", newline="", encoding=encoding) as f:
            writer = csv.DictWriter(f, fieldnames=header, restval="")
            writer.writeheader()
            writer.writerows(rows)
            writer.writerow(row)
        tmp.replace(csv_path)
        return

    header = [*header, *missing]
    write_header = not rows and (not csv_path.exists() or csv_path.stat().st_size == 0 or missing)
    mode = "w" if write_header else "a"
    if mode == "a" and not csv_path.read_bytes().endswith((b"\n", b"\r")):
        with open(csv_path, "a", newline="", encoding=encoding) as f:
            f.write("\r\n")  # a hand-edited file missing its final newline
    with open(csv_path, mode, newline="", encoding=encoding) as f:
        writer = csv.DictWriter(f, fieldnames=header, restval="")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---- Submit ----


def submit_folder_annotation(config: dict, name: str, values: dict, schema: dict, unusable: bool = False) -> dict:
    """Validates, moves the image into the annotated folder, then appends its
    CSV row -- same end state as the old tool's Submit. If the CSV write
    fails the image is moved back, so it can never be done-but-unrecorded."""
    if not schema:
        raise FolderError("There are no attributes to annotate -- add some on the Attributes page.")
    source = image_path(config, name)
    normalized = annotate.normalize_values(schema, values, unusable=unusable)

    destination_dir = Path(config["imageDestination"]).expanduser()
    destination = destination_dir / source.name
    if destination.exists():
        raise FolderError(f"{destination} already exists -- move or rename it before submitting this image.")
    destination_dir.mkdir(parents=True, exist_ok=True)

    shutil.move(str(source), str(destination))
    try:
        append_csv_row(Path(config["CSVDest"]).expanduser(), source.name, normalized, list(normalized))
    except Exception:
        shutil.move(str(destination), str(source))
        raise
    return normalized


if __name__ == "__main__":
    import sys

    config = read_old_tool_settings(Path(sys.argv[1])) if len(sys.argv) > 1 else load_source_config()
    print(json.dumps(config, indent=4))
    if config["mode"] == "folder" and Path(config["imageSource"]).expanduser().is_dir():
        print(f"{folder_queue(config, limit=0)['remaining']} images left in {config['imageSource']}")
