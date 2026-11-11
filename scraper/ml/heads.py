"""Attribute heads: one small classifier per attribute, trained on frozen
image embeddings (the embed stage) against annotator labels (dataset.py).

Phase 3, step 3. Each head is a single linear layer -- with ~1,000 labeled
images, anything bigger mostly memorizes. Heads are trained and kept per
backbone, so DINOv3 and SigLIP 2 can be compared on the same test families.

- Pick-one attributes (dropdowns, Present / Not present) get a softmax over
  the answers that have at least MIN_EXAMPLES training examples. Examples
  with a rarer answer are left out of that head rather than lumped together.
- Multi-selects get one yes/no output per option that has at least
  MIN_EXAMPLES examples both with and without it.
- Training is unweighted on purpose: the probabilities stay honest about how
  common each answer is, which is what a pre-fill confidence threshold needs.

Evaluation is on the held-out families only, against a baseline that always
gives the most common training answer. "Pre-fill" rows answer the question
step 5 cares about: if only confident guesses are filled in, how many fields
get one and how often is it right?
"""

import json
import sqlite3
import threading
from pathlib import Path

from scraper.ml.dataset import DEFAULT_OUT_DIR, MIN_EXAMPLES, MULTI_TYPES, label_options, trainable_attributes
from scraper.stages import annotate, embed

EPOCHS = 400
LEARNING_RATE = 0.05
WEIGHT_DECAY = 1e-3
THRESHOLDS = (0.6, 0.8, 0.9)
SEED = 0

# The seed is process-wide and the web server trains on a thread, so two fits
# starting together must not share one draw of starting weights.
_seed_lock = threading.Lock()


class TrainingError(ValueError):
    pass


def heads_path(backbone: str, out_dir: Path = DEFAULT_OUT_DIR) -> Path:
    return out_dir / f"heads_{backbone}.pt"


# ---- Data ----


def _features(examples: list[dict], vectors: dict[str, list[float]]) -> tuple[list[dict], list[list[float]], int]:
    """Examples whose image has an embedding, with their vectors."""
    kept, rows, missing = [], [], 0
    for example in examples:
        path = Path(example["image"])
        sha = example.get("sha256") or (embed.file_sha256(path) if path.is_file() else None)
        vector = vectors.get(sha) if sha else None
        if vector is None:
            missing += 1
            continue
        kept.append(example)
        rows.append(vector)
    return kept, rows, missing


def _single_targets(examples, name, options):
    index = {option: i for i, option in enumerate(options)}
    return [(i, index[e["labels"][name]]) for i, e in enumerate(examples) if e["labels"].get(name) in index]


def _multi_targets(examples, name, options):
    return [
        (i, [1.0 if o in e["labels"][name] else 0.0 for o in options])
        for i, e in enumerate(examples)
        if name in e["labels"]
    ]


# ---- Training ----


def _fit(X, targets, kind: str, out_dim: int, watch=None):
    """Trains one head. `watch(step, linear, loss)`, when given, is shown the
    layer as it stands before each step and once more after the last one
    (step == EPOCHS); it only looks, so the result is the same with or
    without it."""
    import torch

    rows = torch.tensor([i for i, _ in targets])
    y = torch.tensor([t for _, t in targets], dtype=torch.long if kind == "single" else torch.float32)
    x = X[rows]
    with _seed_lock:  # the starting weights are the only random thing in a fit
        torch.manual_seed(SEED)
        linear = torch.nn.Linear(x.shape[1], out_dim)
    optimizer = torch.optim.AdamW(linear.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    loss_fn = torch.nn.CrossEntropyLoss() if kind == "single" else torch.nn.BCEWithLogitsLoss()
    for step in range(EPOCHS):  # full batch: a few hundred rows of 768 numbers
        optimizer.zero_grad()
        loss = loss_fn(linear(x), y)
        if watch is not None:
            watch(step, linear, loss.item())
        loss.backward()
        optimizer.step()
    linear.eval()
    if watch is not None:
        with torch.no_grad():
            watch(EPOCHS, linear, loss_fn(linear(x), y).item())
    return linear


def _probabilities(linear, X, kind: str):
    import torch

    with torch.no_grad():
        logits = linear(X)
    return logits.softmax(dim=1) if kind == "single" else logits.sigmoid()


# ---- Evaluation ----


def _auroc(scores: list[float], labels: list[int]) -> float | None:
    """Chance the model ranks a random "yes" above a random "no" (0.5 =
    guessing). None when the test set has only one of the two."""
    pairs = sorted(zip(scores, labels))
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):  # average ranks across ties
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        rank_sum += sum(label for _, label in pairs[i:j]) * (i + j + 1) / 2
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _prefill(decisions: list[tuple[float, bool]]) -> dict:
    """decisions: (confidence, was it right) -> coverage and accuracy per threshold."""
    out = {}
    for threshold in THRESHOLDS:
        confident = [right for confidence, right in decisions if confidence >= threshold]
        out[str(threshold)] = {
            "coverage": len(confident) / len(decisions) if decisions else 0.0,
            "accuracy": sum(confident) / len(confident) if confident else None,
        }
    return out


def _evaluate_single(head: dict, probs, targets, train_targets) -> dict:
    options = head["options"]
    counts = [0] * len(options)
    for _, t in train_targets:
        counts[t] += 1
    majority = counts.index(max(counts))
    predicted = [int(probs[i].argmax()) for i, _ in targets]
    truth = [t for _, t in targets]
    recalls = []
    for k in range(len(options)):
        rows = [p == t for p, t in zip(predicted, truth) if t == k]
        if rows:
            recalls.append(sum(rows) / len(rows))
    return {
        "test_examples": len(targets),
        "accuracy": sum(p == t for p, t in zip(predicted, truth)) / len(truth),
        "balanced_accuracy": sum(recalls) / len(recalls),
        "baseline_accuracy": sum(t == majority for t in truth) / len(truth),
        "baseline_answer": options[majority],
        "prefill": _prefill([(float(probs[i].max()), int(probs[i].argmax()) == t) for i, t in targets]),
    }


def _evaluate_multi(head: dict, probs, targets, train_targets) -> dict:
    per_option, decisions, baseline_right = {}, [], 0
    for k, option in enumerate(head["options"]):
        share = sum(t[k] for _, t in train_targets) / len(train_targets)
        scores = [float(probs[i][k]) for i, _ in targets]
        labels = [int(t[k]) for _, t in targets]
        per_option[option] = {
            "auroc": _auroc(scores, labels),
            "accuracy": sum((s >= 0.5) == bool(y) for s, y in zip(scores, labels)) / len(labels),
            "baseline_accuracy": sum((share >= 0.5) == bool(y) for y in labels) / len(labels),
            "test_with": sum(labels),
        }
        baseline_right += sum((share >= 0.5) == bool(y) for y in labels)
        decisions += [(max(s, 1 - s), (s >= 0.5) == bool(y)) for s, y in zip(scores, labels)]
    aurocs = [o["auroc"] for o in per_option.values() if o["auroc"] is not None]
    return {
        "test_examples": len(targets),
        "accuracy": sum(right for _, right in decisions) / len(decisions),
        "baseline_accuracy": baseline_right / len(decisions),
        "mean_auroc": sum(aurocs) / len(aurocs) if aurocs else None,
        "per_option": per_option,
        "prefill": _prefill(decisions),
    }


def _split(examples: list[dict], vectors: dict[str, list[float]]):
    """(train examples, test examples, train embeddings, test embeddings, how
    many examples have no embedding)."""
    import torch

    kept, rows, missing = _features(examples, vectors)
    train = [e for e in kept if e["split"] == "train"]
    test = [e for e in kept if e["split"] == "test"]
    if not train or not test:
        raise TrainingError(
            f"Need embedded images on both sides of the split (have {len(train)} train, {len(test)} test, "
            f"{missing} without an embedding) -- run the embed stage on the training set first."
        )
    X_train = torch.tensor([r for e, r in zip(kept, rows) if e["split"] == "train"])
    X_test = torch.tensor([r for e, r in zip(kept, rows) if e["split"] == "test"])
    return train, test, X_train, X_test, missing


def train_heads(examples: list[dict], vectors: dict[str, list[float]], schema: dict) -> tuple[dict, dict]:
    """Trains every attribute with enough data. Returns (heads, report);
    heads[name] = {"kind", "options", "linear"}."""
    train, test, X_train, X_test, missing = _split(examples, vectors)

    heads, attributes, skipped = {}, {}, {}
    for name, spec in trainable_attributes(schema).items():
        kind = "multi" if spec[0] in MULTI_TYPES else "single"
        all_options = label_options(spec)
        if kind == "single":
            counts = {o: sum(e["labels"].get(name) == o for e in train) for o in all_options}
            options = [o for o in all_options if counts[o] >= MIN_EXAMPLES]
            build = _single_targets
            enough = len(options) >= 2
        else:
            labeled = [e for e in train if name in e["labels"]]
            counts = {o: sum(o in e["labels"][name] for e in labeled) for o in all_options}
            options = [o for o in all_options if MIN_EXAMPLES <= counts[o] <= len(labeled) - MIN_EXAMPLES]
            build = _multi_targets
            enough = bool(options)
        if not enough:
            skipped[name] = (
                "not enough examples of at least two answers" if kind == "single" else "no option common enough"
            )
            continue

        train_targets, test_targets = build(train, name, options), build(test, name, options)
        if not test_targets:
            skipped[name] = "no test examples"
            continue
        linear = _fit(X_train, train_targets, kind, len(options))
        head = {"kind": kind, "options": options, "linear": linear}
        evaluate = _evaluate_single if kind == "single" else _evaluate_multi
        metrics = evaluate(head, _probabilities(linear, X_test, kind), test_targets, train_targets)
        heads[name] = head
        attributes[name] = {
            "kind": kind,
            "options": options,
            "left_out_options": [o for o in all_options if o not in options],
            "train_examples": len(train_targets),
            **metrics,
        }

    report = {
        "examples": {"train": len(train), "test": len(test), "no_embedding": missing},
        "attributes": attributes,
        "skipped_attributes": skipped,
    }
    return heads, report


# ---- Replay ----


def replay_training(examples: list[dict], vectors: dict[str, list[float]], trained: dict, at_steps) -> dict:
    """Trains heads that already exist once more, the same way, and keeps
    what happened on the way -- for the Demo page, which plays it back.
    Nothing is saved and `trained` is left as it is.

    Each head is fitted for the answers it was trained on (not the attribute
    list as it is now). {name: {"steps", "loss", "accuracy",
    "baseline_accuracy", "train_examples", "test_examples", "weights",
    "biases", "same_as_saved"}}: one entry per step in `at_steps` -- the
    error on the training images, the share right on the held-out families
    (None when none of them has an answer) and the layer's weights at that
    moment. `same_as_saved` says whether the run ended where the trained
    head is, which it does unless the training set has changed since. A head
    with nothing left to train on is left out."""
    train, test, X_train, X_test, _ = _split(examples, vectors)
    wanted = set(at_steps)
    out = {}
    for name, head in trained.items():
        kind, options = head["kind"], head["options"]
        build = _single_targets if kind == "single" else _multi_targets
        train_targets, test_targets = build(train, name, options), build(test, name, options)
        if train_targets:
            out[name] = _replay_head(head, X_train, X_test, train_targets, test_targets, wanted)
    return out


def _replay_head(head: dict, X_train, X_test, train_targets, test_targets, wanted: set) -> dict:
    import torch

    kind = head["kind"]
    evaluate = _evaluate_single if kind == "single" else _evaluate_multi
    seen = {"steps": [], "loss": [], "accuracy": [], "weights": [], "biases": []}
    baseline = []

    def watch(step, linear, loss):
        if step not in wanted:
            return
        seen["steps"].append(step)
        seen["loss"].append(loss)
        seen["weights"].append(linear.weight.detach().clone())
        seen["biases"].append(linear.bias.detach().clone())
        if not test_targets:
            seen["accuracy"].append(None)
            return
        metrics = evaluate(head, _probabilities(linear, X_test, kind), test_targets, train_targets)
        seen["accuracy"].append(metrics["accuracy"])
        baseline[:] = [metrics["baseline_accuracy"]]

    linear = _fit(X_train, train_targets, kind, len(head["options"]), watch=watch)
    saved = head["linear"]
    return {
        **seen,
        "baseline_accuracy": baseline[0] if baseline else None,
        "train_examples": len(train_targets),
        "test_examples": len(test_targets),
        "same_as_saved": bool(
            saved.weight.shape == linear.weight.shape
            and torch.allclose(saved.weight, linear.weight, atol=1e-3)
            and torch.allclose(saved.bias, linear.bias, atol=1e-3)
        ),
    }


# ---- Save / load / predict ----


def save_heads(path: Path, heads: dict, report: dict, backbone: str, schema: dict) -> None:
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "backbone": backbone,
            "schema_version": annotate.schema_version(schema),
            "heads": {
                name: {"kind": h["kind"], "options": h["options"], "state": h["linear"].state_dict()}
                for name, h in heads.items()
            },
            "report": report,
        },
        path,
    )


def load_heads(path: Path) -> dict:
    """{"backbone", "schema_version", "heads": {name: {"kind", "options", "linear"}}, "report"}."""
    import torch

    data = torch.load(path, weights_only=True)
    for head in data["heads"].values():
        weight = head["state"]["weight"]
        linear = torch.nn.Linear(weight.shape[1], weight.shape[0])
        linear.load_state_dict(head.pop("state"))
        linear.eval()
        head["linear"] = linear
    return data


def predict(heads: dict, vector: list[float]) -> dict[str, dict[str, float]]:
    """One image's embedding -> {attribute: {answer: probability}}. For a
    multi-select each option's probability is independent (yes vs no)."""
    import torch

    x = torch.tensor([vector])
    return {
        name: dict(zip(head["options"], _probabilities(head["linear"], x, head["kind"])[0].tolist()))
        for name, head in heads.items()
    }


def run_training(
    conn: sqlite3.Connection,
    schema: dict,
    training_set: Path = DEFAULT_OUT_DIR / "dataset.jsonl",
    backbone: str = embed.DEFAULT_BACKBONE,
    out_dir: Path = DEFAULT_OUT_DIR,
) -> dict:
    """Trains on a built training set with one backbone's embeddings and
    writes heads_<backbone>.pt and heads_<backbone>.json into out_dir."""
    from scraper.ml.dataset import load_examples

    if not training_set.is_file():
        raise TrainingError(f"No training set at {training_set} -- run build-training-set first.")
    vectors = embed.load_embeddings(conn, backbone)
    if not vectors:
        raise TrainingError(f"Nothing is embedded with {backbone} yet -- run the embed stage first.")
    heads, report = train_heads(load_examples(training_set), vectors, schema)
    report["backbone"] = backbone
    path = heads_path(backbone, out_dir)
    save_heads(path, heads, report, backbone, schema)
    path.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["saved_to"] = str(path)
    return report


def _pct(value) -> str:
    return "  n/a" if value is None else f"{value * 100:4.0f}%"


def format_report(report: dict) -> str:
    """The evaluation as plain text, for the CLI."""
    ex = report["examples"]
    lines = [
        f"Backbone: {report.get('backbone', '?')}  |  {ex['train']} train / {ex['test']} test images"
        + (f", {ex['no_embedding']} without an embedding" if ex["no_embedding"] else ""),
        "Scores are on held-out satellite families. Baseline = always give the most common training answer.",
        "Pre-fill = share of fields with a guess at least that confident, and how often those are right.\n",
        f"{'attribute':30} {'model':>6} {'base':>6}  {'pre-fill @0.8':>18}  {'@0.9':>16}",
    ]
    for name, a in report["attributes"].items():
        p8, p9 = a["prefill"]["0.8"], a["prefill"]["0.9"]
        lines.append(
            f"{name:30} {_pct(a['accuracy']):>6} {_pct(a['baseline_accuracy']):>6}  "
            f"{_pct(p8['coverage'])} filled,{_pct(p8['accuracy'])}  "
            f"{_pct(p9['coverage'])} filled,{_pct(p9['accuracy'])}"
        )
        if a["kind"] == "single":
            lines.append(
                f"{'':30} balanced accuracy {_pct(a['balanced_accuracy'])}, baseline says '{a['baseline_answer']}'"
            )
        else:
            ranked = ", ".join(
                f"{o} {_pct(v['auroc']).strip()}" for o, v in a["per_option"].items() if v["auroc"] is not None
            )
            lines.append(f"{'':30} ranking quality (AUROC, 50% = guessing): {ranked}")
        if a["left_out_options"]:
            lines.append(f"{'':30} too rare to learn yet: {', '.join(a['left_out_options'])}")
    for name, why in report["skipped_attributes"].items():
        lines.append(f"{name:30} not trained: {why}")
    return "\n".join(lines)
