"""Offline tests for pre-fill: the model's guesses for every image waiting for
annotation, worked out in one batch. A small model trained on synthetic
embeddings and a fake image model stand in for the real ones; no download."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scraper import jobs
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import predict, prefill
from scraper.stages import embed
from scraper.web import app as web_app
from tests.test_ml_predict_offline import BACKBONE, DIM, SCHEMA, _train


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeBackbone:
    """Bright red images look Rectangular with a Patch antenna; anything
    else Cylindrical with a Dipole."""

    name = BACKBONE

    def __init__(self):
        self.calls = 0

    def embed(self, images):
        self.calls += 1
        return [
            ([3.0, 3.0, -3.0] if img.getpixel((0, 0))[0] > 200 else [-3.0, -3.0, 3.0]) + [0.0] * (DIM - 3)
            for img in images
        ]


def _image(conn, tmp_path: Path, name: str, color=(255, 0, 0), status="active") -> int:
    path = tmp_path / "images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=color).save(path)
    image_id = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('S', ?, ?, ?)",
        (str(path), status, datetime.now(timezone.utc).isoformat()),
    ).lastrowid
    conn.commit()
    return image_id


def _annotate(conn, image_id: int, annotator="alice", attribute="Shape"):
    conn.execute(
        "INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)"
        " VALUES (?, ?, '\"Cubical\"', ?, 'v1', 'now')",
        (image_id, attribute, annotator),
    )
    conn.commit()


def _guesses(conn, image_id: int) -> dict:
    return {
        r["attribute"]: json.loads(r["value"])
        for r in conn.execute("SELECT attribute, value FROM predictions WHERE image_id = ?", (image_id,))
    }


def test_prefill_guesses_for_images_waiting_for_annotation(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    red = _image(conn, tmp_path, "red.png")
    blue = _image(conn, tmp_path, "blue.png", color=(0, 0, 255))
    done = _image(conn, tmp_path, "done.png")
    _annotate(conn, done)
    rejected = _image(conn, tmp_path, "rejected.png", status="rejected_filter")
    reviewed_only = _image(conn, tmp_path, "noted.png")
    _annotate(conn, reviewed_only, annotator="human_review")  # Review's note doesn't count as annotated

    result = prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone(), batch_size=2)

    assert result["images"] == 3 and result["guessed"] == 3 and result["already_guessed"] == 0
    assert _guesses(conn, red) == {"Shape": "Rectangular", "Antennas": ["Patch"]}
    assert _guesses(conn, blue) == {"Shape": "Cylindrical", "Antennas": ["Dipole"]}
    assert _guesses(conn, done) == {} and _guesses(conn, rejected) == {}
    assert _guesses(conn, reviewed_only)
    assert not conn.execute(
        "SELECT COUNT(*) FROM annotations WHERE annotator = 'alice' AND image_id != ?", (done,)
    ).fetchone()[0]
    version = predict.model_version(predict.model_path(out_dir))
    assert {r[0] for r in conn.execute("SELECT DISTINCT model_version FROM predictions")} == {version}
    assert "Guessed for 3 image(s)" in prefill.format_result(result)


def test_a_second_run_skips_what_this_model_already_guessed(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    _image(conn, tmp_path, "red.png")
    prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone())
    _image(conn, tmp_path, "new.png", color=(0, 255, 0))

    backbone = FakeBackbone()
    again = prefill.run_prefill(conn, SCHEMA, out_dir, backbone=backbone)
    assert (again["guessed"], again["already_guessed"]) == (1, 1)
    assert backbone.calls == 1  # only the new image needed embedding

    redo = prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone(), redo=True)
    assert redo["guessed"] == 2
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 4  # updated, not duplicated


def test_include_annotated_and_coverage(conn, tmp_path):
    out_dir = tmp_path / "training"
    assert prefill.coverage(conn, out_dir) == {"available": False, "waiting": 0, "with_guesses": 0}
    _train(out_dir, tmp_path)
    done = _image(conn, tmp_path, "done.png")
    _annotate(conn, done)
    _image(conn, tmp_path, "waiting.png")
    assert prefill.coverage(conn, out_dir)["with_guesses"] == 0

    prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone(), include_annotated=True)
    assert _guesses(conn, done)  # useful for checking the model against real answers
    c = prefill.coverage(conn, out_dir)
    assert (c["available"], c["waiting"], c["with_guesses"]) == (True, 1, 1)


def test_missing_unreadable_and_stop(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    gone = _image(conn, tmp_path, "gone.png")
    Path(conn.execute("SELECT path FROM images WHERE id = ?", (gone,)).fetchone()[0]).unlink()
    broken = _image(conn, tmp_path, "broken.png")
    Path(conn.execute("SELECT path FROM images WHERE id = ?", (broken,)).fetchone()[0]).write_bytes(b"not an image")

    result = prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone())
    assert (result["missing"], result["unreadable"], result["guessed"]) == (1, 1, 0)
    assert "Skipped 1 missing and 1 unreadable" in prefill.format_result(result)

    _image(conn, tmp_path, "red.png")
    cancel = threading.Event()
    cancel.set()
    stopped = prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone(), cancel=cancel)
    assert stopped["cancelled"] and stopped["guessed"] == 0


def test_no_model_yet(conn, tmp_path):
    with pytest.raises(predict.NoModelError):
        prefill.run_prefill(conn, SCHEMA, tmp_path / "training", backbone=FakeBackbone())


def test_autofill_after_prefill_needs_no_image_model(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    red = _image(conn, tmp_path, "red.png")
    prefill.run_prefill(conn, SCHEMA, out_dir, backbone=FakeBackbone())

    def no_download(name):
        raise AssertionError("pre-fill already embedded this image")

    path = Path(conn.execute("SELECT path FROM images WHERE id = ?", (red,)).fetchone()[0])
    result = predict.suggest(conn, path, SCHEMA, red, out_dir, backbone_factory=no_download)
    assert result["suggestions"]["Shape"]["value"] == "Rectangular"


def test_prefill_job_endpoint_and_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "web.db"
    web_app._jobs.clear()
    with TestClient(web_app.app) as client:
        assert client.post("/api/jobs/prefill", json={}).status_code == 400  # no model yet
        assert client.get("/api/train/status").json()["prefill"]["available"] is False

        _train(Path("training"), tmp_path)
        seen = {}

        def fake_job(conn, log, model, include_annotated, redo, cancel, on_progress, attributes_path):
            seen.update(model=model, include_annotated=include_annotated)
            return {"guessed": 0}

        monkeypatch.setattr(jobs, "run_prefill_job", fake_job)
        resp = client.post("/api/jobs/prefill", json={"include_annotated": True})
        assert resp.status_code == 200 and resp.json()["job_id"]
        for _ in range(100):
            if seen:
                break
            threading.Event().wait(0.02)
        assert seen == {"model": embed.DEFAULT_BACKBONE, "include_annotated": True}
        assert client.post("/api/jobs/prefill", json={"model": "nope"}).status_code == 400
        assert 'id="card-train-prefill"' in client.get("/train").text
