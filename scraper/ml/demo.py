"""Demo mode: what the Demo page needs to draw the trained model deciding an
image's attributes, neuron by neuron.

Something to show people, not a tool: nothing here is saved and nothing in
the pipeline reads it. It is honest about what it shows, though. For one
image it returns the model's real numbers:

- the image's embedding (what the frozen image model turned it into), pooled
  into CELLS cells so it can be drawn as one strip;
- for every trained attribute, how hard each of those cells pushes toward or
  away from each answer (the head's weights times the embedding, summed per
  cell). Add a row's pushes to its bias and you have exactly the head's
  output for that answer, so the lines on the page are the decision;
- the probabilities, the answers picked (the same rule Auto-fill uses) and,
  where an annotator has answered, what they said.

Images come from the held-out families of the training set first -- the ones
the model never trained on, so being right there means something -- and
otherwise from any active image that has an embedding. An image set aside
for any reason is never shown.

training() adds how the model got there. It runs the training again, exactly
as the Train model page does -- same images, same starting weights, same 400
steps, which takes a second or two -- and keeps the lines as they stood at a
few dozen moments on the way, with the error on the training images and the
share right on the held-out families at each. Run on the training set the
model was trained on, it ends on the very weights that are saved, and the
page is told when it doesn't (the training set has been rebuilt since).
"""

import json
import random
import sqlite3
import threading
from pathlib import Path

from scraper.ml import heads, predict
from scraper.ml.dataset import DEFAULT_OUT_DIR, load_examples
from scraper.stages import embed

CELLS = 48  # the embedding is drawn as this many cells (16 numbers each for a 768-number embedding)
SNAPSHOTS_PER_TENFOLD = 8  # moments of a training run that are kept between step 1 and 10, 10 and 100, ...

_lock = threading.Lock()
_replayed: dict = {"key": None, "replay": None}  # the last training replay: the same for every image


def _held_out(training_set: Path) -> dict[str, dict]:
    """sha256 -> the annotator's labels, for the training set's test side."""
    if not training_set.is_file():
        return {}
    answers: dict[str, dict] = {}
    with open(training_set, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            example = json.loads(line)
            if example.get("split") == "test" and example.get("sha256"):
                answers.setdefault(example["sha256"], example.get("labels") or {})
    return answers


def _candidates(conn: sqlite3.Connection, backbone: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.id, i.family, i.content_sha256 AS sha256
           FROM images i JOIN embeddings e ON e.sha256 = i.content_sha256 AND e.model = ?
           WHERE i.status = 'active'
           ORDER BY i.id""",
        (backbone,),
    ).fetchall()


def _cells(dim: int) -> list[tuple[int, int]]:
    """(start, end) of each cell: the embedding cut into near-equal runs."""
    count = min(CELLS, dim)
    return [(dim * i // count, dim * (i + 1) // count) for i in range(count)]


def _truth(kind: str, options: list[str], labels: dict | None, name: str) -> dict | None:
    """What the annotator answered, in terms of the answers the head knows.
    None when they didn't answer this attribute."""
    if labels is None or name not in labels:
        return None
    given = labels[name]
    if kind == "single":
        return {
            "answers": [options.index(given)] if given in options else [],
            "other": [] if given in options else [given],
        }
    return {
        "answers": [k for k, option in enumerate(options) if option in given],
        "other": [answer for answer in given if answer not in options],
    }


def _snapshot_steps(total: int) -> list[int]:
    """The steps of a training run to keep. The lines change fastest at the
    start, so early steps are kept closely and later ones further apart: the
    same number between 1 and 10 as between 10 and 100, and so on to the end."""
    marks = [10**p for p in range(9) if 10**p < total] + [total]
    steps = {0, total}
    for low, high in zip(marks, marks[1:]):
        steps |= {round(low * (high / low) ** (i / SNAPSHOTS_PER_TENFOLD)) for i in range(SNAPSHOTS_PER_TENFOLD)}
    return sorted(steps)


def _choose(conn: sqlite3.Connection, out_dir: Path, preferred, image_id, after, rng):
    """The trained model, the image to show, its embedding and the
    annotator's answers for it (None when it isn't a held-out image)."""
    import torch

    model_file = predict.model_path(out_dir, preferred)
    if model_file is None:
        raise predict.NoModelError("No trained model yet -- train one on the Train model page.")
    model = predict.loaded_model(model_file)
    backbone = model["backbone"]

    rows = _candidates(conn, backbone)
    answers = _held_out(out_dir / "dataset.jsonl")
    if image_id is not None:
        pool = [row for row in rows if row["id"] == image_id]
        if not pool:
            raise LookupError("That image isn't active or has no embedding for the trained model.")
    else:
        pool = [row for row in rows if row["sha256"] in answers] or rows
        if not pool:
            raise LookupError("No active image has an embedding yet -- run Embed on the Train model page.")
        if after is not None and len(pool) > 1:
            pool = [row for row in pool if row["id"] != after]
    row = (rng or random).choice(pool)

    blob = conn.execute(
        "SELECT vector FROM embeddings WHERE sha256 = ? AND model = ?", (row["sha256"], backbone)
    ).fetchone()["vector"]
    return model_file, model, row, torch.tensor(embed.blob_to_vector(blob)), answers.get(row["sha256"])


def _picked(kind: str, probabilities: list[float]) -> list[int]:
    """The answers picked, by the rule Auto-fill uses."""
    if kind == "single":
        return [max(range(len(probabilities)), key=probabilities.__getitem__)]
    return [k for k, p in enumerate(probabilities) if p >= 0.5]


def _scene(model: dict, row: sqlite3.Row, x, labels: dict | None) -> dict:
    import torch

    cells = _cells(x.shape[0])
    attributes = []
    for name, head in model["heads"].items():
        linear, kind, options = head["linear"], head["kind"], head["options"]
        if linear.weight.shape[1] != x.shape[0]:
            continue  # trained on a different-sized embedding; nothing honest to draw
        with torch.no_grad():
            pushes = linear.weight * x  # answers x numbers: each number's share of each answer's score
            per_cell = torch.stack([pushes[:, start:end].sum(dim=1) for start, end in cells], dim=1)
            scores = per_cell.sum(dim=1) + linear.bias
            probabilities = scores.softmax(dim=0) if kind == "single" else scores.sigmoid()
        probs = probabilities.tolist()
        attributes.append(
            {
                "name": name,
                "kind": kind,
                "options": options,
                "probabilities": [round(p, 4) for p in probs],
                "picked": _picked(kind, probs),
                "bias": [round(b, 4) for b in linear.bias.tolist()],
                "pushes": [[round(v, 4) for v in answer] for answer in per_cell.tolist()],
                "truth": _truth(kind, options, labels, name),
            }
        )

    backbone = model["backbone"]
    return {
        "image": {"id": row["id"], "family": row["family"]},
        "held_out": labels is not None,
        "model": {"backbone": backbone, "label": embed.BACKBONE_LABELS.get(backbone, backbone)},
        "numbers": int(x.shape[0]),
        "cells": [round(float(x[start:end].mean()), 5) for start, end in cells],
        "attributes": attributes,
    }


def sample(
    conn: sqlite3.Connection,
    out_dir: Path = DEFAULT_OUT_DIR,
    preferred: str | None = None,
    image_id: int | None = None,
    after: int | None = None,
    rng: random.Random | None = None,
) -> dict:
    """One image and everything the model made of it. `image_id` asks for a
    particular image; otherwise one is picked at random, never `after` (the
    one just shown) when there is another to show."""
    _, model, row, x, labels = _choose(conn, out_dir, preferred, image_id, after, rng)
    return _scene(model, row, x, labels)


def _replay(conn: sqlite3.Connection, model_file: Path, model: dict, training_set: Path) -> dict:
    """The model's training, run again and kept until the model, the training
    set or the embeddings change. One at a time: it is a second or two of
    work that every image shares."""
    if not training_set.is_file():
        raise LookupError(
            "The training set this model learned from isn't here, so its training can't be replayed "
            "-- build it again on the Train model page."
        )
    backbone = model["backbone"]
    embedded = conn.execute("SELECT COUNT(*) FROM embeddings WHERE model = ?", (backbone,)).fetchone()[0]
    key = (str(model_file), model_file.stat().st_mtime, training_set.stat().st_mtime, embedded)
    with _lock:
        if _replayed["key"] != key:
            try:
                replay = heads.replay_training(
                    load_examples(training_set),
                    embed.load_embeddings(conn, backbone),
                    model["heads"],
                    _snapshot_steps(heads.EPOCHS),
                )
            except heads.TrainingError as e:
                raise LookupError(f"The training can't be replayed: {e}") from e
            _replayed.update(key=key, replay=replay)
        return _replayed["replay"]


def training(
    conn: sqlite3.Connection,
    out_dir: Path = DEFAULT_OUT_DIR,
    preferred: str | None = None,
    image_id: int | None = None,
    after: int | None = None,
    rng: random.Random | None = None,
) -> dict:
    """sample(), plus the training that led there: for each attribute, what
    the lines did to this image at each kept step of the run. Attributes
    whose training can't be run again are left out."""
    import torch

    model_file, model, row, x, labels = _choose(conn, out_dir, preferred, image_id, after, rng)
    replay = _replay(conn, model_file, model, out_dir / "dataset.jsonl")
    scene = _scene(model, row, x, labels)
    cells = _cells(x.shape[0])

    attributes = []
    for attribute in scene["attributes"]:
        run = replay.get(attribute["name"])
        if run is None or run["weights"][0].shape[1] != x.shape[0]:
            continue
        with torch.no_grad():
            pushes = torch.stack(run["weights"]) * x  # steps x answers x numbers
            per_cell = torch.stack([pushes[:, :, start:end].sum(dim=2) for start, end in cells], dim=2)
            scores = per_cell.sum(dim=2) + torch.stack(run["biases"])
            probabilities = scores.softmax(dim=1) if attribute["kind"] == "single" else scores.sigmoid()
        probs = probabilities.tolist()
        attribute["replay"] = {
            "steps": run["steps"],
            "loss": [round(v, 4) for v in run["loss"]],
            "accuracy": [None if v is None else round(v, 4) for v in run["accuracy"]],
            "baseline_accuracy": None if run["baseline_accuracy"] is None else round(run["baseline_accuracy"], 4),
            "train_examples": run["train_examples"],
            "test_examples": run["test_examples"],
            "same_as_saved": run["same_as_saved"],
            "bias": [[round(b, 4) for b in step.tolist()] for step in run["biases"]],
            "pushes": [[[round(v, 3) for v in answer] for answer in step] for step in per_cell.tolist()],
            "probabilities": [[round(p, 4) for p in step] for step in probs],
            "picked": _picked(attribute["kind"], probs[-1]),
        }
        attributes.append(attribute)
    if not attributes:
        raise LookupError("None of this model's attributes can be trained again from the training set that is here.")

    scene["attributes"] = attributes
    scene["training"] = {"total_steps": heads.EPOCHS}
    return scene
