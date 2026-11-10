"""The trained screener: a second opinion for the filter stage, learned from
this project's own Review decisions.

The filter's prompts are the same for everyone and miss junk they have no
words for. This is one small linear classifier on the filter's own CLIP image
features: kept images against images a person rejected as "Not a satellite".
The filter sets an image aside if the prompts or this screener reject it, so
screening is never weaker than the prompts alone.

Measured on real scrapes before building it: on junk like it had seen, it
caught 48 of 56 junk images where the prompts caught 30. On a family it had
never seen junk from, it caught 4. So it has to earn its place:

- It stays off ("learning") until there are MIN_REJECTS rejections from
  MIN_FAMILIES families.
- Every retrain is checked on families it wasn't trained on. It only turns on
  if, on those, it catches at least MIN_JUNK_CAUGHT percent of the junk and
  sets aside at most MAX_KEPT_LOST percent of kept images.
- It never learns from the filter's own rejects, only from people. An image
  brought back with "Keep after all" counts as kept the next time it trains.

It retrains by itself when screening starts and RETRAIN_AFTER or more
decisions have been made since the last time. Training takes seconds: the
image features are cached in the embeddings table.
"""

import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from scraper.ml.dataset import DEFAULT_OUT_DIR
from scraper.stages import embed

EMBEDDING_MODEL = "clip-vit-b32"  # the filter's CLIP image features, in the embeddings table
LEARNS_FROM = "not_a_satellite"  # the one Review reject reason that means "junk"

MIN_REJECTS = 100
MIN_FAMILIES = 5
MIN_JUNK_CAUGHT = 25.0  # percent of rejected images, on families held out of training
MAX_KEPT_LOST = 2.0  # percent of kept images, on families held out of training
KEPT_LOSS_TARGET = 0.5  # the cut-off is set where this percent of the kept images it trained on fall below it
RETRAIN_AFTER = 25  # decisions since the last training
FOLDS = 5

FEATURE_SCALE = 20.0  # unit-length features give tiny dot products; this keeps the fit well-conditioned
L2 = 0.01

LogFn = Callable[[str], None]


def screener_path(out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    return out_dir / "screener.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Screener:
    """A trained screener, ready to score image features. A margin below 0
    means "set aside"."""

    def __init__(self, weights: list[float], bias: float):
        self.weights = weights
        self.bias = bias

    def margins(self, features: list) -> list[Optional[float]]:
        """One margin per image; None where there are no features (an
        unreadable file) or they are from a different model."""
        out: list[Optional[float]] = []
        for vector in features:
            if vector is None or len(vector) != len(self.weights):
                out.append(None)
            else:
                out.append(sum(w * x for w, x in zip(self.weights, vector)) + self.bias)
        return out


# ---- What it learns from ----


def training_rows(conn: sqlite3.Connection) -> list[dict]:
    """One row per distinct image file: kept ones (label 1) and ones a person
    rejected as not a satellite (label 0). A file that is both counts as kept.
    A file that was ever set aside as coming from an off-limits source is left
    out entirely, kept copy or not: nothing is trained on those."""
    off_limits = {
        row["content_sha256"]
        for row in conn.execute(
            "SELECT content_sha256 FROM images WHERE reject_reason = 'off_limits_source' AND content_sha256 IS NOT NULL"
        )
    }
    rows, seen = [], set(off_limits)
    queries = (
        (1, "status = 'active' AND reviewed_at IS NOT NULL"),
        (0, f"reject_reason = '{LEARNS_FROM}'"),
    )
    for label, where in queries:
        for row in conn.execute(
            f"SELECT content_sha256 AS sha, family, path FROM images WHERE {where}"
            " AND content_sha256 IS NOT NULL ORDER BY id"
        ):
            if row["sha"] in seen:
                continue
            seen.add(row["sha"])
            rows.append({"sha": row["sha"], "family": row["family"], "path": row["path"], "label": label})
    return rows


def _tally(rows: list[dict]) -> dict:
    rejected = [row for row in rows if row["label"] == 0]
    return {
        "kept": len(rows) - len(rejected),
        "rejected": len(rejected),
        "rejected_families": len({row["family"] for row in rejected}),
    }


def _enough(tally: dict) -> bool:
    return tally["rejected"] >= MIN_REJECTS and tally["rejected_families"] >= MIN_FAMILIES and tally["kept"] > 0


def _read(out_dir: Path) -> Optional[dict]:
    try:
        return json.loads(screener_path(out_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save(result: dict, out_dir: Path) -> None:
    path = screener_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---- Fitting ----


def _fit(X, y):
    """Weighted logistic regression (both classes count the same in total).
    Returns (weights, bias) as tensors, with FEATURE_SCALE folded into the
    weights so a score is just weights . features + bias."""
    import torch

    w = torch.zeros(X.shape[1], requires_grad=True)
    b = torch.zeros(1, requires_grad=True)
    positive_weight = (y == 0).sum() / (y == 1).sum()
    sample_weight = torch.where(y == 1, positive_weight, torch.ones_like(y))
    sample_weight = sample_weight / sample_weight.sum()
    optimizer = torch.optim.LBFGS([w, b], max_iter=200, line_search_fn="strong_wolfe")

    def closure():
        optimizer.zero_grad()
        logits = FEATURE_SCALE * (X @ w) + b
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, y, reduction="none")
        loss = (loss * sample_weight).sum() + L2 * (w**2).sum()
        loss.backward()
        return loss

    optimizer.step(closure)
    return (FEATURE_SCALE * w).detach(), b.detach()


def _cutoff(kept_scores: list[float]) -> float:
    """The score KEPT_LOSS_TARGET percent of these kept images fall below."""
    ordered = sorted(kept_scores)
    return ordered[min(len(ordered) - 1, int(len(ordered) * KEPT_LOSS_TARGET / 100))]


def _fit_with_cutoff(X, y):
    w, b = _fit(X, y)
    scores = X @ w + b
    return w, float(b) - _cutoff(scores[y == 1].tolist())


def _fold_of(rows: list[dict]) -> tuple[dict[str, int], int]:
    """Whole families go into one fold each, so the check is always on
    families the screener didn't train on. Families with rejections are dealt
    out first, biggest first, so every fold has some junk to be tested on."""
    rejected = Counter(row["family"] for row in rows if row["label"] == 0)
    folds = min(FOLDS, len(rejected))
    fold = {}
    for i, (family, _count) in enumerate(sorted(rejected.items(), key=lambda item: (-item[1], item[0]))):
        fold[family] = i % folds
    for i, family in enumerate(sorted({row["family"] for row in rows} - set(fold))):
        fold[family] = i % folds
    return fold, folds


def _check(rows: list[dict], X, y) -> dict:
    """Trains on some families and tests on the others, in turn: how much of
    the junk does it catch, and how many kept images does it wrongly set
    aside, on families it has never seen?"""
    import torch

    fold, folds = _fold_of(rows)
    row_fold = torch.tensor([fold[row["family"]] for row in rows])
    caught = lost = 0
    for held_out in range(folds):
        train, test = row_fold != held_out, row_fold == held_out
        if not ((y[train] == 0).any() and (y[train] == 1).any()):
            continue
        w, bias = _fit_with_cutoff(X[train], y[train])
        margins = X[test] @ w + bias
        caught += int(((margins < 0) & (y[test] == 0)).sum())
        lost += int(((margins < 0) & (y[test] == 1)).sum())
    junk_total, kept_total = int((y == 0).sum()), int((y == 1).sum())
    return {
        "folds": folds,
        "junk_total": junk_total,
        "junk_caught": caught,
        "junk_caught_pct": round(100 * caught / junk_total, 1),
        "kept_total": kept_total,
        "kept_lost": lost,
        "kept_lost_pct": round(100 * lost / kept_total, 1),
    }


# ---- Training ----


def train(
    conn: sqlite3.Connection,
    embedder=None,
    out_dir: Path = DEFAULT_OUT_DIR,
    log: Optional[LogFn] = None,
    cancel: Optional[threading.Event] = None,
) -> Optional[dict]:
    """Trains, checks and saves the screener. Returns what was saved, or None
    when there aren't enough decisions yet (nothing is saved) or it was
    cancelled.

    `embedder` computes features for images that don't have them yet: it
    needs .name == EMBEDDING_MODEL and .embed(list of PIL images). Without
    one, only images with cached features are used.
    """
    import torch

    log = log or (lambda message: None)
    rows = training_rows(conn)
    if not _enough(_tally(rows)):
        return None

    have = {row["sha256"] for row in conn.execute("SELECT sha256 FROM embeddings WHERE model = ?", (EMBEDDING_MODEL,))}
    missing = [Path(row["path"]) for row in rows if row["sha"] not in have]
    if missing and embedder is not None:
        log(f"Trained screener: reading {len(missing)} images it hasn't seen yet...")
        if embed.embed_files(conn, missing, embedder, cancel=cancel)["cancelled"]:
            return None
    vectors = embed.load_embeddings(conn, EMBEDDING_MODEL)
    rows = [row for row in rows if row["sha"] in vectors]
    tally = _tally(rows)
    if not _enough(tally):
        return None

    X = torch.tensor([vectors[row["sha"]] for row in rows], dtype=torch.float32)
    y = torch.tensor([float(row["label"]) for row in rows])
    check = _check(rows, X, y)
    passed = check["junk_caught_pct"] >= MIN_JUNK_CAUGHT and check["kept_lost_pct"] <= MAX_KEPT_LOST
    w, bias = _fit_with_cutoff(X, y)

    result = {
        "state": "on" if passed else "off",
        "trained_at": _now(),
        "embedding_model": EMBEDDING_MODEL,
        **tally,
        "check": check,
        "weights": [round(value, 6) for value in w.tolist()],
        "bias": bias,
    }
    _save(result, out_dir)
    log(
        f"Trained screener: trained on {tally['kept']} kept and {tally['rejected']} rejected images. "
        f"On families it hadn't seen, it caught {check['junk_caught_pct']}% of the junk and set aside "
        f"{check['kept_lost_pct']}% of kept images, so it is {'on' if passed else 'off'}."
    )
    return result


def prepare(
    conn: sqlite3.Connection,
    embedder=None,
    out_dir: Path = DEFAULT_OUT_DIR,
    retrain: bool = True,
    log: Optional[LogFn] = None,
    cancel: Optional[threading.Event] = None,
) -> Optional[Screener]:
    """Called when screening starts: retrains if enough decisions have been
    made since the last time, then returns the screener if it is on."""
    tally = _tally(training_rows(conn))
    if not _enough(tally):
        return None
    saved = _read(out_dir)
    due = (
        saved is None
        or saved.get("embedding_model") != EMBEDDING_MODEL
        or abs(tally["kept"] - saved.get("kept", 0)) + abs(tally["rejected"] - saved.get("rejected", 0))
        >= RETRAIN_AFTER
    )
    if due and retrain and embedder is not None:
        saved = train(conn, embedder, out_dir, log, cancel) or saved
    if not saved or saved.get("state") != "on" or saved.get("embedding_model") != EMBEDDING_MODEL:
        return None
    return Screener(saved["weights"], saved["bias"])


def status(conn: sqlite3.Connection, out_dir: Path = DEFAULT_OUT_DIR) -> dict:
    """Where the screener stands, for the Review page. Loads no model.

    state: "learning" (not enough decisions yet), "untrained" (enough, trains
    when screening next runs), "on", or "off" (trained, failed its check).
    """
    tally = _tally(training_rows(conn))
    saved = _read(out_dir)
    if not _enough(tally):
        state = "learning"
    elif saved is None or saved.get("embedding_model") != EMBEDDING_MODEL:
        state = "untrained"
    else:
        state = saved.get("state", "off")
    out = {
        "state": state,
        **tally,
        "needs": {
            "rejected": MIN_REJECTS,
            "rejected_families": MIN_FAMILIES,
            "junk_caught_pct": MIN_JUNK_CAUGHT,
            "kept_lost_pct": MAX_KEPT_LOST,
        },
    }
    if saved and state in ("on", "off"):
        out["trained_at"] = saved.get("trained_at")
        out["trained_on"] = {"kept": saved.get("kept"), "rejected": saved.get("rejected")}
        out["check"] = saved.get("check")
    return out


def describe(info: dict) -> str:
    """status() in one sentence, for the command line and logs."""
    if info["state"] == "learning":
        return (
            f'Learning: {info["rejected"]} of {info["needs"]["rejected"]} "Not a satellite" rejections so far, '
            f"from {info['rejected_families']} of {info['needs']['rejected_families']} families."
        )
    if info["state"] == "untrained":
        return "Ready to train: it trains the next time screening runs."
    check = info.get("check") or {}
    verdict = "On" if info["state"] == "on" else "Off (it didn't pass its check)"
    return (
        f"{verdict}: trained on {info['trained_on']['kept']} kept and {info['trained_on']['rejected']} rejected "
        f"images. On families it hadn't seen, it caught {check.get('junk_caught_pct')}% of the junk and set aside "
        f"{check.get('kept_lost_pct')}% of kept images."
    )


if __name__ == "__main__":
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    db = get_connection(Path("scraper.db"))
    apply_migrations(db)
    print(describe(status(db)))
