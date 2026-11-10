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
"""

import json
import random
import sqlite3
from pathlib import Path

from scraper.ml import predict
from scraper.ml.dataset import DEFAULT_OUT_DIR
from scraper.stages import embed

CELLS = 48  # the embedding is drawn as this many cells (16 numbers each for a 768-number embedding)


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
    x = torch.tensor(embed.blob_to_vector(blob))
    cells = _cells(x.shape[0])
    labels = answers.get(row["sha256"])

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
        if kind == "single":
            picked = [max(range(len(probs)), key=probs.__getitem__)]
        else:
            picked = [k for k, p in enumerate(probs) if p >= 0.5]
        attributes.append(
            {
                "name": name,
                "kind": kind,
                "options": options,
                "probabilities": [round(p, 4) for p in probs],
                "picked": picked,
                "bias": [round(b, 4) for b in linear.bias.tolist()],
                "pushes": [[round(v, 4) for v in answer] for answer in per_cell.tolist()],
                "truth": _truth(kind, options, labels, name),
            }
        )

    return {
        "image": {"id": row["id"], "family": row["family"]},
        "held_out": labels is not None,
        "model": {"backbone": backbone, "label": embed.BACKBONE_LABELS.get(backbone, backbone)},
        "numbers": int(x.shape[0]),
        "cells": [round(float(x[start:end].mean()), 5) for start, end in cells],
        "attributes": attributes,
    }
