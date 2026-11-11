"""Offline tests for Demo mode: the numbers the Demo page animates are the
trained model's own, images come from the held-out side first, and nothing
set aside is ever shown. The Training view's replay is the real training run
again, and ends on the model that is saved. Hand-made heads on tiny
embeddings; no download."""

import json
import math
import os
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
import torch
from fastapi.testclient import TestClient

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import demo, heads, predict
from scraper.stages import embed
from scraper.web import app as web_app

SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
    }
}
DIM = 8
BACKBONE = embed.DEFAULT_BACKBONE


def _linear(weight: list[list[float]], bias: list[float]) -> torch.nn.Linear:
    layer = torch.nn.Linear(len(weight[0]), len(weight))
    with torch.no_grad():
        layer.weight.copy_(torch.tensor(weight))
        layer.bias.copy_(torch.tensor(bias))
    return layer


def _save_model(out_dir: Path) -> dict:
    """Shape reads number 0 (positive = Rectangular); Patch reads number 1,
    Dipole number 2."""
    trained = {
        "Shape": {
            "kind": "single",
            "options": ["Rectangular", "Cylindrical"],
            "linear": _linear([[4.0, 0, 0, 0, 0, 0, 0, 0], [-4.0, 0, 0, 0, 0, 0, 0, 0]], [0.1, -0.1]),
        },
        "Antennas": {
            "kind": "multi",
            "options": ["Patch", "Dipole"],
            "linear": _linear([[0, 5.0, 0, 0, 0, 0, 0, 0], [0, 0, 5.0, 0, 0, 0, 0, 0]], [0.2, -0.3]),
        },
    }
    heads.save_heads(heads.heads_path(BACKBONE, out_dir), trained, {}, BACKBONE, SCHEMA)
    return trained


def _image(conn, sha: str, vector: list[float], family: str = "S", status: str = "active") -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
        (sha, BACKBONE, len(vector), embed.vector_to_blob(vector), now),
    )
    image_id = conn.execute(
        "INSERT INTO images (family, path, status, content_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
        (family, f"{sha}.png", status, sha, now),
    ).lastrowid
    conn.commit()
    return image_id


def _training_set(out_dir: Path, examples: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dataset.jsonl").write_text("".join(json.dumps(e) + "\n" for e in examples), encoding="utf-8")


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


VECTOR = [0.6, 0.5, -0.4, 0.1, 0.0, 0.2, -0.1, 0.3]

# ---- The numbers are the model's own ----


def test_sample_reports_the_models_own_probabilities(conn, tmp_path):
    out = tmp_path / "training"
    trained = _save_model(out)
    image_id = _image(conn, "a" * 64, VECTOR, family="AMAZONAS")

    got = demo.sample(conn, out_dir=out)

    assert got["image"] == {"id": image_id, "family": "AMAZONAS"}
    assert got["model"] == {"backbone": BACKBONE, "label": embed.BACKBONE_LABELS[BACKBONE]}
    assert got["numbers"] == DIM
    assert [a["name"] for a in got["attributes"]] == ["Shape", "Antennas"]
    expected = heads.predict(trained, VECTOR)
    for attribute in got["attributes"]:
        want = [expected[attribute["name"]][option] for option in attribute["options"]]
        assert attribute["probabilities"] == pytest.approx(want, abs=1e-3)


def test_pushes_plus_bias_are_the_decision(conn, tmp_path):
    """The lines the page draws add up to the head's output: nothing invented."""
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)

    shape, antennas = demo.sample(conn, out_dir=out)["attributes"]

    scores = [sum(row) + bias for row, bias in zip(shape["pushes"], shape["bias"])]
    total = sum(math.exp(s) for s in scores)
    assert [math.exp(s) / total for s in scores] == pytest.approx(shape["probabilities"], abs=1e-3)
    scores = [sum(row) + bias for row, bias in zip(antennas["pushes"], antennas["bias"])]
    assert [1 / (1 + math.exp(-s)) for s in scores] == pytest.approx(antennas["probabilities"], abs=1e-3)


def test_cells_are_the_embedding_and_each_push_sits_under_its_cell(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)

    got = demo.sample(conn, out_dir=out)

    assert got["cells"] == pytest.approx(VECTOR, abs=1e-4)  # 8 numbers, 8 cells: one each
    shape = got["attributes"][0]
    assert shape["pushes"][0] == pytest.approx([2.4, 0, 0, 0, 0, 0, 0, 0], abs=1e-3)  # 4.0 x 0.6, from number 0
    assert shape["pushes"][1] == pytest.approx([-2.4, 0, 0, 0, 0, 0, 0, 0], abs=1e-3)


def test_a_long_embedding_is_pooled_into_cells(conn, tmp_path):
    cells = demo._cells(768)
    assert len(cells) == demo.CELLS
    assert cells[0] == (0, 16) and cells[-1] == (752, 768)
    assert all(end == nxt for (_, end), (nxt, _) in zip(cells, cells[1:]))  # no number skipped or counted twice


def test_picks_follow_the_same_rule_as_auto_fill(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)  # number 0 > 0, number 1 > 0, number 2 < 0

    shape, antennas = demo.sample(conn, out_dir=out)["attributes"]

    assert shape["kind"] == "single" and shape["picked"] == [0]  # Rectangular: the likeliest
    assert antennas["kind"] == "multi" and antennas["picked"] == [0]  # Patch only: Dipole is under half


def test_a_multi_select_can_pick_nothing(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, [0.6, -0.5, -0.4, 0, 0, 0, 0, 0])

    antennas = demo.sample(conn, out_dir=out)["attributes"][1]

    assert antennas["picked"] == []


# ---- What the annotator said ----


def test_held_out_image_carries_the_annotators_answers(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)
    _training_set(
        out, [{"sha256": "a" * 64, "split": "test", "labels": {"Shape": "Cylindrical", "Antennas": ["Patch", "Horn"]}}]
    )

    got = demo.sample(conn, out_dir=out)

    assert got["held_out"] is True
    shape, antennas = got["attributes"]
    assert shape["truth"] == {"answers": [1], "other": []}  # the model said Rectangular: a miss, shown as one
    assert antennas["truth"] == {"answers": [0], "other": ["Horn"]}  # Horn isn't an answer the model knows


def test_an_answer_the_model_never_learned_is_reported_as_other(conn, tmp_path):
    assert demo._truth("single", ["Rectangular", "Cylindrical"], {"Shape": "Spherical"}, "Shape") == {
        "answers": [],
        "other": ["Spherical"],
    }
    assert demo._truth("single", ["Rectangular"], {"Mass": "Small"}, "Shape") is None  # not answered
    assert demo._truth("multi", ["Patch"], None, "Antennas") is None  # no annotator at all


def test_image_outside_the_training_set_has_no_truth(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)

    got = demo.sample(conn, out_dir=out)

    assert got["held_out"] is False
    assert all(attribute["truth"] is None for attribute in got["attributes"])


# ---- Which image ----


def test_held_out_images_are_shown_first(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    for letter in "abcdef":
        _image(conn, letter * 64, VECTOR)
    held_out = _image(conn, "9" * 64, VECTOR)
    _training_set(
        out,
        [
            {"sha256": "9" * 64, "split": "test", "labels": {"Shape": "Rectangular"}},
            {"sha256": "a" * 64, "split": "train", "labels": {"Shape": "Rectangular"}},  # trained on: not a fair test
        ],
    )

    shown = {demo.sample(conn, out_dir=out, rng=random.Random(seed))["image"]["id"] for seed in range(12)}

    assert shown == {held_out}


def test_next_never_repeats_the_image_just_shown(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    first = _image(conn, "a" * 64, VECTOR)
    second = _image(conn, "b" * 64, VECTOR)

    shown = {demo.sample(conn, out_dir=out, after=first, rng=random.Random(seed))["image"]["id"] for seed in range(12)}

    assert shown == {second}


def test_the_only_image_is_shown_again(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    only = _image(conn, "a" * 64, VECTOR)

    assert demo.sample(conn, out_dir=out, after=only)["image"]["id"] == only


def test_set_aside_images_are_never_shown(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    active = _image(conn, "a" * 64, VECTOR)
    rejected = _image(conn, "b" * 64, VECTOR, status="rejected_filter")
    _training_set(out, [{"sha256": "b" * 64, "split": "test", "labels": {"Shape": "Rectangular"}}])

    shown = {demo.sample(conn, out_dir=out, rng=random.Random(seed))["image"]["id"] for seed in range(12)}

    assert shown == {active}
    with pytest.raises(LookupError):
        demo.sample(conn, out_dir=out, image_id=rejected)


def test_asking_for_one_image_returns_it(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)
    wanted = _image(conn, "b" * 64, VECTOR)

    assert demo.sample(conn, out_dir=out, image_id=wanted)["image"]["id"] == wanted


def test_no_model_and_no_embedded_image_say_so(conn, tmp_path):
    out = tmp_path / "training"
    with pytest.raises(predict.NoModelError):
        demo.sample(conn, out_dir=out)
    _save_model(out)
    with pytest.raises(LookupError, match="embedding"):
        demo.sample(conn, out_dir=out)


def test_head_trained_on_another_sized_embedding_is_left_out(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR[:4])  # 4 numbers; the heads expect 8

    assert demo.sample(conn, out_dir=out)["attributes"] == []


def test_sampling_saves_nothing(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)
    before = conn.total_changes

    demo.sample(conn, out_dir=out)

    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


# ---- The training, played back ----


def _trained(conn, out_dir: Path, n: int = 80) -> list[dict]:
    """n embedded images whose embedding decides their labels (Shape from
    number 0, Patch from number 1, Dipole from number 2), a training set
    made of them with every 5th image held out, and the model trained on it."""
    rng = random.Random(3)
    examples = []
    for i in range(n):
        sha = f"{i:064x}"
        v = [rng.gauss(0, 1) for _ in range(DIM)]
        _image(conn, sha, v, family=f"SAT{i}")
        examples.append(
            {
                "image": f"{sha}.png",
                "sha256": sha,
                "family": f"SAT{i}",
                "split": "test" if i % 5 == 0 else "train",
                "labels": {
                    "Shape": "Rectangular" if v[0] > 0 else "Cylindrical",
                    "Antennas": [o for o, d in (("Patch", 1), ("Dipole", 2)) if v[d] > 0],
                },
            }
        )
    _training_set(out_dir, examples)
    trained, report = heads.train_heads(examples, embed.load_embeddings(conn, BACKBONE), SCHEMA)
    heads.save_heads(heads.heads_path(BACKBONE, out_dir), trained, report, BACKBONE, SCHEMA)
    return examples


def test_snapshot_steps_keep_the_start_closely_and_the_end():
    steps = demo._snapshot_steps(400)

    assert steps[0] == 0 and steps[-1] == 400
    assert steps == sorted(set(steps))
    assert {1, 2, 3, 10, 100} <= set(steps)
    assert len(steps) < 40  # a few dozen moments, not all 400
    early = [step for step in steps if step <= 10]
    late = [step for step in steps if step >= 100]
    assert len(early) >= len(late) - 1  # as many moments kept in the first 10 steps as in the last 300
    assert demo._snapshot_steps(5) == [0, 1, 2, 3, 4, 5]
    assert demo._snapshot_steps(1) == [0, 1]


def test_training_replay_ends_on_the_saved_model(conn, tmp_path):
    out = tmp_path / "training"
    _trained(conn, out)

    got = demo.training(conn, out_dir=out, rng=random.Random(0))

    assert got["training"] == {"total_steps": heads.EPOCHS}
    assert got["held_out"] is True
    assert [a["name"] for a in got["attributes"]] == ["Shape", "Antennas"]
    for attribute in got["attributes"]:
        replay = attribute["replay"]
        assert replay["same_as_saved"] is True
        assert replay["steps"] == demo._snapshot_steps(heads.EPOCHS)
        assert replay["train_examples"] == 64 and replay["test_examples"] == 16
        # The last moment of the run is the model the Deciding view shows.
        assert replay["probabilities"][-1] == pytest.approx(attribute["probabilities"], abs=1e-3)
        for row, want in zip(replay["pushes"][-1], attribute["pushes"], strict=True):
            assert row == pytest.approx(want, abs=2e-3)
        assert replay["picked"] == attribute["picked"]
        for series in ("loss", "accuracy", "bias", "pushes", "probabilities"):
            assert len(replay[series]) == len(replay["steps"])


def test_replayed_lines_are_the_decision_at_every_step(conn, tmp_path):
    """At each kept step the lines add up to that step's percentages."""
    out = tmp_path / "training"
    _trained(conn, out)

    shape, antennas = demo.training(conn, out_dir=out, rng=random.Random(0))["attributes"]

    for pushes, bias, probabilities in zip(*(shape["replay"][k] for k in ("pushes", "bias", "probabilities"))):
        scores = [sum(row) + b for row, b in zip(pushes, bias)]
        total = sum(math.exp(v) for v in scores)
        assert [math.exp(v) / total for v in scores] == pytest.approx(probabilities, abs=5e-3)
    for pushes, bias, probabilities in zip(*(antennas["replay"][k] for k in ("pushes", "bias", "probabilities"))):
        scores = [sum(row) + b for row, b in zip(pushes, bias)]
        assert [1 / (1 + math.exp(-v)) for v in scores] == pytest.approx(probabilities, abs=5e-3)


def test_replay_shows_the_model_learning(conn, tmp_path):
    out = tmp_path / "training"
    _trained(conn, out)

    shape = demo.training(conn, out_dir=out, rng=random.Random(0))["attributes"][0]["replay"]

    assert shape["loss"][-1] < shape["loss"][0] / 3  # the error on the training images falls
    assert shape["accuracy"][-1] > 0.85 and shape["accuracy"][-1] > shape["baseline_accuracy"]
    assert shape["pushes"][0] != shape["pushes"][-1]  # the lines it started with aren't the ones it ended on


def test_replay_says_when_the_training_set_has_changed(conn, tmp_path):
    out = tmp_path / "training"
    examples = _trained(conn, out)
    for example in examples[:40]:  # relabelled since the model was trained
        example["labels"]["Shape"] = "Cylindrical" if example["labels"]["Shape"] == "Rectangular" else "Rectangular"
    _training_set(out, examples)
    os.utime(out / "dataset.jsonl", (1, 1))  # a different file time whatever the clock's resolution

    shape, antennas = demo.training(conn, out_dir=out, rng=random.Random(0))["attributes"]

    assert shape["replay"]["same_as_saved"] is False
    assert antennas["replay"]["same_as_saved"] is True  # its labels are as they were


def test_attribute_that_cant_be_trained_again_is_left_out(conn, tmp_path):
    out = tmp_path / "training"
    examples = _trained(conn, out)
    for example in examples:
        del example["labels"]["Antennas"]
    _training_set(out, examples)
    os.utime(out / "dataset.jsonl", (1, 1))

    got = demo.training(conn, out_dir=out, rng=random.Random(0))

    assert [a["name"] for a in got["attributes"]] == ["Shape"]


def test_replay_is_run_once_and_again_only_when_something_changes(conn, tmp_path, monkeypatch):
    out = tmp_path / "training"
    _trained(conn, out)
    runs = []
    real = heads.replay_training
    monkeypatch.setattr(heads, "replay_training", lambda *args: runs.append(1) or real(*args))

    first = demo.training(conn, out_dir=out, rng=random.Random(0))
    second = demo.training(conn, out_dir=out, rng=random.Random(1))  # another image, the same training
    assert len(runs) == 1
    assert first["image"]["id"] != second["image"]["id"]
    assert first["attributes"][0]["replay"]["loss"] == second["attributes"][0]["replay"]["loss"]

    os.utime(out / "dataset.jsonl", (1, 1))  # the training set was built again
    demo.training(conn, out_dir=out, rng=random.Random(0))
    assert len(runs) == 2


def test_training_without_its_training_set_says_so(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)

    with pytest.raises(LookupError, match="training set"):
        demo.training(conn, out_dir=out)
    with pytest.raises(predict.NoModelError):
        demo.training(conn, out_dir=tmp_path / "nothing")


def test_training_set_with_nothing_embedded_says_so(conn, tmp_path):
    out = tmp_path / "training"
    _save_model(out)
    _image(conn, "a" * 64, VECTOR)
    _training_set(out, [{"image": "b.png", "sha256": "b" * 64, "split": "train", "labels": {"Shape": "Rectangular"}}])

    with pytest.raises(LookupError, match="can't be replayed"):
        demo.training(conn, out_dir=out)


def test_training_replay_saves_nothing(conn, tmp_path):
    out = tmp_path / "training"
    _trained(conn, out)
    model_file = heads.heads_path(BACKBONE, out)
    before, saved = conn.total_changes, model_file.read_bytes()

    demo.training(conn, out_dir=out)

    assert conn.total_changes == before
    assert model_file.read_bytes() == saved


# ---- The page and its endpoint ----


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    web_app._jobs.clear()
    (tmp_path / "attributes.json").write_text(json.dumps(SCHEMA))
    with TestClient(web_app.app) as test_client:
        yield test_client


def test_demo_page_renders_with_its_nav_link(client):
    resp = client.get("/demo")

    assert resp.status_code == 200
    assert 'id="demo-stage"' in resp.text
    assert 'data-view="training"' in resp.text
    assert "js/demo.js" in resp.text
    assert 'href="/demo"' in client.get("/train").text  # reachable from every page's sidebar


def test_demo_endpoint_without_a_model_is_a_409(client):
    resp = client.get("/api/demo/sample")

    assert resp.status_code == 409
    assert "Train model" in resp.json()["detail"]


def test_demo_endpoint_returns_a_sample_and_404s_an_unknown_image(client, tmp_path):
    _save_model(tmp_path / "training")
    conn = get_connection(tmp_path / "test.db")
    apply_migrations(conn)
    image_id = _image(conn, "a" * 64, VECTOR, family="AMAZONAS")
    conn.close()

    resp = client.get("/api/demo/sample")

    assert resp.status_code == 200
    body = resp.json()
    assert body["image"] == {"id": image_id, "family": "AMAZONAS"}
    assert body["attributes"][0]["options"] == ["Rectangular", "Cylindrical"]
    assert client.get("/api/demo/sample", params={"image_id": image_id + 99}).status_code == 404


def test_training_endpoint_replays_and_says_why_it_cant(client, tmp_path):
    assert client.get("/api/demo/training").status_code == 409  # no model yet

    conn = get_connection(tmp_path / "test.db")
    apply_migrations(conn)
    _trained(conn, tmp_path / "training")
    conn.close()

    resp = client.get("/api/demo/training")

    assert resp.status_code == 200
    body = resp.json()
    assert body["training"] == {"total_steps": heads.EPOCHS}
    assert body["attributes"][0]["replay"]["same_as_saved"] is True
    same = client.get("/api/demo/training", params={"image_id": body["image"]["id"]}).json()
    assert same["image"] == body["image"]  # changing view keeps the image
    assert client.get("/api/demo/training", params={"image_id": 10**6}).status_code == 404

    (tmp_path / "training" / "dataset.jsonl").unlink()
    gone = client.get("/api/demo/training")
    assert gone.status_code == 404 and "training set" in gone.json()["detail"]
    assert client.get("/api/demo/sample").status_code == 200  # the Deciding view still works
