"""Offline tests for Demo mode: the numbers the Demo page animates are the
trained model's own, images come from the held-out side first, and nothing
set aside is ever shown. Hand-made heads on tiny embeddings; no download."""

import json
import math
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
