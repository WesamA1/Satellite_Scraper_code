"""What the Train page shows and remembers: the last-used training-set
settings, and the latest results of each step, all read from the training/
folder (gitignored) and the embeddings table."""

import json
import sqlite3
from pathlib import Path

from scraper.ml import heads
from scraper.ml.dataset import DEFAULT_DATA_DIR, DEFAULT_OUT_DIR, DatasetError, data_sources
from scraper.stages import annotate, embed

SETTINGS_PATH = DEFAULT_OUT_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "csvs": [{"annotator": "", "path": ""}],
    "image_dirs": [],
    "include_database": True,
    "model": embed.DEFAULT_BACKBONE,
}


def _first_run_settings(data_dir: Path = DEFAULT_DATA_DIR) -> dict:
    """Before anything is saved: start from the data folder, if there is one."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        csvs, image_dirs = data_sources(data_dir)
    except DatasetError:
        return settings
    if csvs:
        settings["csvs"] = [
            {"annotator": "" if name == annotate.LEGACY_ANNOTATOR else name, "path": path.as_posix()}
            for name, path in csvs
        ]
    settings["image_dirs"] = [d.as_posix() for d in image_dirs]
    return settings


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _first_run_settings()
    settings = {**DEFAULT_SETTINGS, **{k: v for k, v in data.items() if k in DEFAULT_SETTINGS}}
    if settings["model"] not in embed.BACKBONES:
        settings["model"] = embed.DEFAULT_BACKBONE
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: settings[k] for k in DEFAULT_SETTINGS if k in settings}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**load_settings(path), **clean}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def csv_args(csvs: list[dict]) -> list[str]:
    """The page's CSV rows -> build-training-set's --csv values (NAME=PATH)."""
    out = []
    for row in csvs:
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        name = str(row.get("annotator") or "").strip()
        out.append(f"{name}={path}" if name else path)
    return out


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def status(conn: sqlite3.Connection, out_dir: Path = DEFAULT_OUT_DIR) -> dict:
    embedded = {
        row["model"]: row["n"] for row in conn.execute("SELECT model, COUNT(*) AS n FROM embeddings GROUP BY model")
    }
    return {
        "settings": load_settings(out_dir / "settings.json"),
        "backbones": embed.BACKBONE_LABELS,
        "dataset": _read_json(out_dir / "report.json"),
        "embedded": {name: embedded.get(name, 0) for name in embed.BACKBONES},
        "heads": {name: _read_json(heads.heads_path(name, out_dir).with_suffix(".json")) for name in embed.BACKBONES},
    }
