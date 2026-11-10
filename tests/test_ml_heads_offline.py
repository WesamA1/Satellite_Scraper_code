"""Offline tests for the attribute heads: they learn a signal that's really in
the embeddings, beat the always-the-common-answer baseline, leave rare answers
out, and save/load/predict consistently. Synthetic embeddings, no real model."""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import heads
from scraper.stages import embed

SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical", "Spherical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole", "Horn"]],
        "Manufacturer": ["Text Box"],
    },
    "Other": {"Mass": ["Dropdown", ["Small", "Large"]]},
}
DIM = 16


def _dataset(tmp_path: Path, n: int = 240):
    """n images whose embedding decides their labels: Shape from dim 0,
    Patch from dim 1, Dipole from dim 2. Every 5th image is in test."""
    rng = random.Random(7)
    examples, vectors = [], {}
    for i in range(n):
        path = tmp_path / "images" / f"SAT{i}_01.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), color=(i % 256, i // 256, 9)).save(path)
        v = [rng.gauss(0, 1) for _ in range(DIM)]
        labels = {
            "Shape": "Rectangular" if v[0] > 0 else "Cylindrical",
            "Antennas": [o for o, d in (("Patch", 1), ("Dipole", 2)) if v[d] > 0],
            "Mass": "Small",  # only one answer: nothing to learn
        }
        if i < 3:
            labels["Shape"] = "Spherical"  # too rare for a head
        examples.append(
            {"image": str(path), "family": f"SAT{i}", "split": "test" if i % 5 == 0 else "train", "labels": labels}
        )
        vectors[embed.file_sha256(path)] = v
    return examples, vectors


def test_heads_learn_real_signal_and_beat_the_baseline(tmp_path):
    examples, vectors = _dataset(tmp_path)
    trained, report = heads.train_heads(examples, vectors, SCHEMA)

    shape = report["attributes"]["Shape"]
    assert shape["accuracy"] > 0.9 and shape["accuracy"] > shape["baseline_accuracy"] + 0.3
    assert shape["left_out_options"] == ["Spherical"]
    assert shape["train_examples"] == sum(e["split"] == "train" for e in examples) - 2  # Sphericals 1, 2 in train

    antennas = report["attributes"]["Antennas"]
    assert antennas["options"] == ["Patch", "Dipole"]  # Horn never appears
    assert antennas["mean_auroc"] > 0.95
    assert antennas["accuracy"] > antennas["baseline_accuracy"]

    assert "Mass" in report["skipped_attributes"]
    assert "Manufacturer" not in report["attributes"] and "Manufacturer" not in report["skipped_attributes"]
    assert set(trained) == {"Shape", "Antennas"}


def test_confident_prefills_are_more_accurate(tmp_path):
    examples, vectors = _dataset(tmp_path)
    _, report = heads.train_heads(examples, vectors, SCHEMA)
    prefill = report["attributes"]["Shape"]["prefill"]
    assert prefill["0.6"]["coverage"] >= prefill["0.9"]["coverage"]
    assert prefill["0.9"]["accuracy"] >= report["attributes"]["Shape"]["accuracy"]


def test_images_without_an_embedding_are_counted(tmp_path):
    examples, vectors = _dataset(tmp_path)
    examples.append(
        {"image": str(tmp_path / "gone.png"), "family": "X", "split": "train", "labels": {"Shape": "Rectangular"}}
    )
    _, report = heads.train_heads(examples, vectors, SCHEMA)
    assert report["examples"]["no_embedding"] == 1


def test_needs_both_sides_of_the_split(tmp_path):
    examples, vectors = _dataset(tmp_path, n=40)
    with pytest.raises(heads.TrainingError):
        heads.train_heads([e for e in examples if e["split"] == "train"], vectors, SCHEMA)


def test_save_load_predict_round_trip(tmp_path):
    examples, vectors = _dataset(tmp_path)
    trained, report = heads.train_heads(examples, vectors, SCHEMA)
    path = tmp_path / "heads_fake.pt"
    heads.save_heads(path, trained, report, "fake", SCHEMA)

    loaded = heads.load_heads(path)
    assert loaded["backbone"] == "fake" and loaded["report"]["attributes"]["Shape"]["options"] == [
        "Rectangular",
        "Cylindrical",
    ]
    vector = [2.0, 2.0, -2.0] + [0.0] * (DIM - 3)
    before, after = heads.predict(trained, vector), heads.predict(loaded["heads"], vector)
    for name in before:
        assert after[name] == pytest.approx(before[name])
    assert after["Shape"]["Rectangular"] > 0.9
    assert sum(after["Shape"].values()) == pytest.approx(1.0)
    assert after["Antennas"]["Patch"] > 0.9 and after["Antennas"]["Dipole"] < 0.1


@pytest.mark.parametrize(
    "scores,labels,expected",
    [
        ([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], 1.0),
        ([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1], 0.0),
        ([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1], 0.5),
        ([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1], 0.75),
        ([0.3, 0.6], [1, 1], None),
    ],
)
def test_auroc(scores, labels, expected):
    assert heads._auroc(scores, labels) == (pytest.approx(expected) if expected is not None else None)


def test_run_training_end_to_end(tmp_path):
    examples, vectors = _dataset(tmp_path)
    training_set = tmp_path / "training" / "dataset.jsonl"
    training_set.parent.mkdir()
    training_set.write_text("".join(json.dumps(e) + "\n" for e in examples))
    conn = get_connection(tmp_path / "test.db")
    apply_migrations(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, 'fake', ?, ?, ?)",
        [(sha, DIM, embed.vector_to_blob(v), now) for sha, v in vectors.items()],
    )
    conn.commit()

    report = heads.run_training(conn, SCHEMA, training_set, "fake", tmp_path / "training")

    assert Path(report["saved_to"]).is_file()
    assert json.loads(Path(report["saved_to"]).with_suffix(".json").read_text())["backbone"] == "fake"
    text = heads.format_report(report)
    assert "Shape" in text and "Mass" in text and "too rare to learn yet: Spherical" in text

    with pytest.raises(heads.TrainingError):
        heads.run_training(conn, SCHEMA, training_set, "nothing-embedded", tmp_path / "training")
    conn.close()


def test_train_command_runs_every_step_from_the_data_folder(tmp_path, monkeypatch):
    """`train` on a fresh machine: data/ in, trained heads out. A fake image
    model stands in for DINOv3 (its "embedding" is the image's color)."""
    from typer.testing import CliRunner

    from scraper import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "attributes.json").write_text(json.dumps({"Body": {"Shape": SCHEMA["Body"]["Shape"]}}))
    rng = random.Random(5)
    images = tmp_path / "data" / "images"
    images.mkdir(parents=True)
    lines = []
    for i in range(150):
        red = rng.randint(0, 255)
        Image.new("RGB", (4, 4), color=(red, i % 256, i // 256)).save(images / f"SAT{i}_01.png")
        lines.append(f"SAT{i}_01.png,{'Rectangular' if red > 127 else 'Cylindrical'}")
    (tmp_path / "data" / "annotations.csv").write_text("name,Shape\r\n" + "\r\n".join(lines) + "\r\n")

    class FakeBackbone:
        def __init__(self, name):
            self.name, self.device_label = name, "cpu"

        def embed(self, batch):
            return [[(img.getpixel((0, 0))[0] / 255) - 0.5, 0.1] for img in batch]

    monkeypatch.setattr(embed, "HFBackbone", FakeBackbone)
    result = CliRunner().invoke(cli.app, ["--db-path", str(tmp_path / "test.db"), "train"])

    assert result.exit_code == 0, result.output
    assert "Using data/" in result.output and "3/3" in result.output
    assert (tmp_path / "training" / "heads_dinov3-vitb16.pt").is_file()
    report = json.loads((tmp_path / "training" / "heads_dinov3-vitb16.json").read_text())
    assert report["attributes"]["Shape"]["accuracy"] > 0.8


# ---- Replay (the Demo page's Training view) ----


def test_watching_a_fit_does_not_change_it(tmp_path):
    examples, vectors = _dataset(tmp_path)
    trained, _ = heads.train_heads(examples, vectors, SCHEMA)

    replay = heads.replay_training(examples, vectors, trained, [0, 1, 50, heads.EPOCHS])

    assert set(replay) == {"Shape", "Antennas"}
    for name, run in replay.items():
        assert run["same_as_saved"] is True
        assert run["steps"] == [0, 1, 50, heads.EPOCHS]
        assert run["loss"][-1] < run["loss"][0]
        # The last thing seen is the trained layer itself, to the last decimal.
        assert run["weights"][-1].tolist() == trained[name]["linear"].weight.tolist()
        assert run["biases"][-1].tolist() == trained[name]["linear"].bias.tolist()
        assert run["weights"][0].tolist() != run["weights"][-1].tolist()
    shape = replay["Shape"]
    assert shape["train_examples"] == sum(e["split"] == "train" for e in examples) - 2
    assert shape["accuracy"][-1] > 0.9 and shape["accuracy"][-1] > shape["baseline_accuracy"] + 0.3


def test_replay_notices_a_changed_training_set(tmp_path):
    examples, vectors = _dataset(tmp_path)
    trained, _ = heads.train_heads(examples, vectors, SCHEMA)
    for example in examples[10:120]:
        example["labels"]["Shape"] = "Cylindrical"

    replay = heads.replay_training(examples, vectors, trained, [0, heads.EPOCHS])

    assert replay["Shape"]["same_as_saved"] is False
    assert replay["Antennas"]["same_as_saved"] is True


def test_replay_without_held_out_answers_has_no_accuracy(tmp_path):
    examples, vectors = _dataset(tmp_path)
    trained, _ = heads.train_heads(examples, vectors, SCHEMA)
    for example in examples:
        if example["split"] == "test":
            del example["labels"]["Shape"]

    shape = heads.replay_training(examples, vectors, trained, [0, heads.EPOCHS])["Shape"]

    assert shape["accuracy"] == [None, None] and shape["baseline_accuracy"] is None
    assert shape["test_examples"] == 0
