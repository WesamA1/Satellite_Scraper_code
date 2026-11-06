"""Pre-fill: the model's guesses for every image waiting for annotation, worked
out ahead of time in one batch instead of one image at a time on the Annotate
page.

It embeds the images that aren't embedded yet (in batches, the slow part),
runs the trained heads on each, and saves the guesses to the predictions
table, the same rows Auto-fill writes. With "Auto-fill every image" ticked on
the Annotate page, each image then opens with its guesses already filled in,
without waiting for the image model.

Nothing here saves an annotation. A person still checks every field and
submits; the guesses are only a starting point.
"""

import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from scraper.ml import heads as heads_module
from scraper.ml import predict
from scraper.ml.dataset import DEFAULT_OUT_DIR
from scraper.stages import embed

# Same rule as the Annotate queue: anyone's annotation counts (imported
# old-tool ones included); Review's own notes and variant picks don't.
_ANNOTATED = """EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = images.id
                        AND a.annotator NOT IN ('human_review') AND a.attribute != 'variant')"""


def targets(conn: sqlite3.Connection, include_annotated: bool = False) -> list[sqlite3.Row]:
    """Active images to guess for: by default only the ones nobody has
    annotated yet."""
    where = "status = 'active'" + ("" if include_annotated else f" AND NOT {_ANNOTATED}")
    return conn.execute(f"SELECT id, path, content_sha256 FROM images WHERE {where} ORDER BY id").fetchall()


def _guessed(conn: sqlite3.Connection, version: str) -> set[int]:
    return {
        r["image_id"]
        for r in conn.execute("SELECT DISTINCT image_id FROM predictions WHERE model_version = ?", (version,))
    }


def coverage(conn: sqlite3.Connection, out_dir: Path = DEFAULT_OUT_DIR, preferred: str | None = None) -> dict:
    """How many images waiting for annotation have guesses from the current
    model, for the Train page."""
    model_file = predict.model_path(out_dir, preferred)
    waiting = [r["id"] for r in targets(conn)]
    if model_file is None:
        return {"available": False, "waiting": len(waiting), "with_guesses": 0}
    version = predict.model_version(model_file)
    guessed = _guessed(conn, version)
    return {
        "available": True,
        "version": version,
        "waiting": len(waiting),
        "with_guesses": sum(1 for image_id in waiting if image_id in guessed),
    }


def run_prefill(
    conn: sqlite3.Connection,
    schema: dict,
    out_dir: Path = DEFAULT_OUT_DIR,
    preferred: str | None = None,
    include_annotated: bool = False,
    redo: bool = False,
    backbone=None,
    batch_size: int = embed.DEFAULT_BATCH_SIZE,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    log: Callable[[str], None] = lambda message: None,
) -> dict:
    """Guesses for every target image that doesn't have them from this model
    yet (`redo` guesses again anyway). Stoppable between batches; a stopped
    run keeps what it saved and picks up from there next time."""
    model_file = predict.model_path(out_dir, preferred)
    if model_file is None:
        raise predict.NoModelError("No trained model yet -- train one on the Train model page.")
    model = heads_module.load_heads(model_file)
    version = predict.model_version(model_file)
    kinds = {name: head["kind"] for name, head in model["heads"].items()}
    backbone = backbone or embed._LazyBackbone(model["backbone"])

    rows = targets(conn, include_annotated)
    already = set() if redo else _guessed(conn, version)
    todo = [r for r in rows if r["id"] not in already]
    counts = {
        "model_version": version,
        "images": len(rows),
        "already_guessed": len(rows) - len(todo),
        "guessed": 0,
        "fields": 0,
        "unsure_fields": 0,
        "missing": 0,
        "unreadable": 0,
        "cancelled": False,
        "device": None,
    }
    log(f"{len(todo)} image(s) to guess for with {version} ({counts['already_guessed']} already done).")

    done = 0
    for start in range(0, len(todo), batch_size):
        if cancel is not None and cancel.is_set():
            counts["cancelled"] = True
            break
        batch = todo[start : start + batch_size]
        paths = {r["id"]: Path(r["path"]) for r in batch}
        present = {image_id: path for image_id, path in paths.items() if path.is_file()}
        counts["missing"] += len(paths) - len(present)
        embed.embed_files(conn, present.values(), backbone, batch_size)

        shas = {image_id: embed.file_sha256(path) for image_id, path in present.items()}
        marks = ",".join("?" * len(shas))
        vectors = {
            r["sha256"]: embed.blob_to_vector(r["vector"])
            for r in conn.execute(
                f"SELECT sha256, vector FROM embeddings WHERE model = ? AND sha256 IN ({marks})",
                (model["backbone"], *shas.values()),
            )
        }
        for image_id, sha in shas.items():
            vector = vectors.get(sha)
            if vector is None:
                counts["unreadable"] += 1
                continue
            suggestions = predict.to_field_values(heads_module.predict(model["heads"], vector), schema, kinds)
            if suggestions:
                predict.save_predictions(conn, image_id, suggestions, version)
            counts["guessed"] += 1
            counts["fields"] += len(suggestions)
            counts["unsure_fields"] += sum(1 for s in suggestions.values() if s["unsure"])

        done += len(batch)
        if on_progress:
            on_progress(done, len(todo))

    loaded = getattr(backbone, "loaded", backbone)
    counts["device"] = getattr(loaded, "device_label", None)
    return counts


def format_result(result: dict) -> str:
    device = f" on {result['device']}" if result.get("device") else ""
    per_image = result["fields"] / result["guessed"] if result["guessed"] else 0
    lines = [
        f"Guessed for {result['guessed']} image(s) with {result['model_version']}{device}"
        f"{' (stopped early)' if result['cancelled'] else ''}; "
        f"{result['already_guessed']} already had guesses from this model."
    ]
    if result["guessed"]:
        lines.append(
            f"About {per_image:.1f} field(s) filled in per image; {result['unsure_fields']} of "
            f"{result['fields']} guesses are marked unsure (below {int(predict.UNSURE_BELOW * 100)}%)."
        )
    if result["missing"] or result["unreadable"]:
        lines.append(f"Skipped {result['missing']} missing and {result['unreadable']} unreadable file(s).")
    return "\n".join(lines)


if __name__ == "__main__":
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations
    from scraper.stages import annotate

    db = get_connection(Path("scraper.db"))
    apply_migrations(db)
    print(coverage(db))
    print(format_result(run_prefill(db, annotate.load_schema(annotate.DEFAULT_SCHEMA_PATH), log=print)))
