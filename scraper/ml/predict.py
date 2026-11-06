"""The model's first-pass guesses for an image, for the Annotate page's
Auto-fill. Phase 3, steps 4-5.

suggest() embeds the image (or reuses its stored embedding), runs the
trained heads, and turns the probabilities into values the Annotate page
can drop straight into its fields -- for the attribute list as it is now,
so editing attributes after training doesn't break anything: an attribute
or option the model never learned is simply left for the annotator.

Nothing here saves an annotation. Guesses go to the predictions table
(one row per attribute, tagged with the model version), so how often
annotators keep or change them can be measured later; a person still
checks every field and submits.
"""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from scraper.ml import heads as heads_module
from scraper.ml.dataset import DEFAULT_OUT_DIR, RADIO_LABELS
from scraper.stages import annotate, embed

# Below this, a guess is filled in but flagged as unsure on the page.
UNSURE_BELOW = 0.6

_lock = threading.Lock()
_loaded: dict = {"key": None, "model": None, "backbones": {}}


class NoModelError(RuntimeError):
    pass


def model_path(out_dir: Path = DEFAULT_OUT_DIR, preferred: str | None = None) -> Path | None:
    """The trained heads to use: the preferred image model's if it exists,
    otherwise whichever was trained most recently."""
    candidates = [heads_module.heads_path(name, out_dir) for name in embed.BACKBONES]
    existing = [p for p in candidates if p.is_file()]
    if preferred and heads_module.heads_path(preferred, out_dir).is_file():
        return heads_module.heads_path(preferred, out_dir)
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def model_info(out_dir: Path = DEFAULT_OUT_DIR, preferred: str | None = None) -> dict:
    path = model_path(out_dir, preferred)
    if path is None:
        return {"available": False, "message": "No trained model yet -- train one on the Train model page."}
    backbone = path.stem.removeprefix("heads_")
    trained = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return {
        "available": True,
        "backbone": backbone,
        "label": embed.BACKBONE_LABELS.get(backbone, backbone),
        "trained_at": trained,
        "version": model_version(path),
    }


def model_version(path: Path) -> str:
    """e.g. dinov3-vitb16@3f2a9c1b7d4e: the image model plus a hash of the
    trained heads, so guesses from different trainings never mix."""
    return f"{path.stem.removeprefix('heads_')}@{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}"


def _model(path: Path) -> dict:
    """Loaded heads, cached until the file changes (a retrain)."""
    key = (str(path), path.stat().st_mtime)
    if _loaded["key"] != key:
        data = heads_module.load_heads(path)
        data["version"] = model_version(path)
        _loaded.update(key=key, model=data)
    return _loaded["model"]


def _embedding(conn: sqlite3.Connection, path: Path, backbone: str, backbone_factory) -> list[float]:
    sha = embed.file_sha256(path)
    row = conn.execute("SELECT vector FROM embeddings WHERE sha256 = ? AND model = ?", (sha, backbone)).fetchone()
    if row is not None:
        return embed.blob_to_vector(row["vector"])
    if backbone not in _loaded["backbones"]:
        _loaded["backbones"][backbone] = backbone_factory(backbone)  # loads (first time: downloads) the model
    embed.embed_files(conn, [path], _loaded["backbones"][backbone])
    row = conn.execute("SELECT vector FROM embeddings WHERE sha256 = ? AND model = ?", (sha, backbone)).fetchone()
    if row is None:
        raise ValueError(f"Couldn't read {path.name} as an image.")
    return embed.blob_to_vector(row["vector"])


def to_field_values(
    probabilities: dict[str, dict[str, float]], schema: dict, kinds: dict[str, str] | None = None
) -> dict[str, dict]:
    """Per-attribute probabilities -> {attribute: {"value", "confidence",
    "unsure", "probabilities"}} in the shape the Annotate page stores.
    Pick-one: the likeliest answer still in the attribute list. Multi-select:
    every option at 50% or more ("None" when there are none); its confidence
    is the least sure of those yes/no calls. `kinds` (head name -> "single" /
    "multi") skips attributes whose type has changed since training."""
    attributes = annotate.flat_attributes(schema)
    out = {}
    for name, probs in probabilities.items():
        spec = attributes.get(name)
        if spec is None:
            continue
        kind = spec[0]
        if kinds and kinds.get(name) != ("multi" if kind == "Multi-Select" else "single"):
            continue  # e.g. a pick-one that's a multi-select now: the head answers a different question
        valid = (
            list(RADIO_LABELS) if kind in ("Check Button", "Single-Select") else list(spec[1] if len(spec) > 1 else [])
        )
        known = {o: p for o, p in probs.items() if o in valid}
        if not known:
            continue
        if kind == "Multi-Select":
            selected = [o for o in valid if known.get(o, 0.0) >= 0.5]
            value = selected or ["None"]
            confidence = min(max(p, 1 - p) for p in known.values())
        elif kind in annotate.OPTION_TYPES or kind in ("Check Button", "Single-Select"):
            total = sum(known.values()) or 1.0  # renormalize if options were removed since training
            value, best = max(known.items(), key=lambda kv: kv[1])
            confidence = best / total
        else:
            continue
        out[name] = {
            "value": value,
            "confidence": round(confidence, 4),
            "unsure": confidence < UNSURE_BELOW,
            "probabilities": {o: round(p, 4) for o, p in known.items()},
        }
    return out


def suggest(
    conn: sqlite3.Connection,
    path: Path,
    schema: dict,
    image_id: int | None = None,
    out_dir: Path = DEFAULT_OUT_DIR,
    preferred: str | None = None,
    backbone_factory=None,
) -> dict:
    """The model's guesses for the image at `path`. Saved to the predictions
    table when the image is in the database (image_id)."""
    model_file = model_path(out_dir, preferred)
    if model_file is None:
        raise NoModelError("No trained model yet -- train one on the Train model page.")
    with _lock:  # one model load / embedding at a time; the web server handles requests on threads
        model = _model(model_file)
        vector = _embedding(conn, path, model["backbone"], backbone_factory or embed.HFBackbone)
        kinds = {name: head["kind"] for name, head in model["heads"].items()}
        suggestions = to_field_values(heads_module.predict(model["heads"], vector), schema, kinds)

    if image_id is not None and suggestions:
        save_predictions(conn, image_id, suggestions, model["version"])
    return {"model": {"backbone": model["backbone"], "version": model["version"]}, "suggestions": suggestions}


def save_predictions(conn: sqlite3.Connection, image_id: int, suggestions: dict[str, dict], version: str) -> None:
    """One row per attribute; re-guessing with the same model updates it."""
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.executemany(
            """INSERT INTO predictions (image_id, attribute, value, confidence, model_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(image_id, attribute, model_version)
               DO UPDATE SET value = excluded.value, confidence = excluded.confidence,
                             created_at = excluded.created_at""",
            [
                (image_id, name, json.dumps(s["value"]), s["confidence"], version, now)
                for name, s in suggestions.items()
            ],
        )
