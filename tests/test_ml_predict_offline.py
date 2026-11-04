"""Offline tests for Auto-fill: turning the model's probabilities into field
values, suggesting for an image (stored or freshly computed embedding), saving
the guesses, and the Annotate endpoints. A small model trained on synthetic
embeddings stands in for the real one; no download."""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import heads, predict
from scraper.stages import embed
from scraper.web import app as web_app

SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
        "Manufacturer": ["Text Box"],
    }
}
DIM = 8
BACKBONE = embed.DEFAULT_BACKBONE


def _train(out_dir: Path, tmp_path: Path):
    """Heads where dim 0 decides Shape and dims 1/2 decide Patch/Dipole."""
    rng = random.Random(11)
    examples, vectors = [], {}
    for i in range(200):
        path = tmp_path / "train_images" / f"S{i}_01.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2, 2), color=(i % 256, i // 256, 3)).save(path)
        v = [rng.gauss(0, 1) for _ in range(DIM)]
        labels = {
            "Shape": "Rectangular" if v[0] > 0 else "Cylindrical",
            "Antennas": [o for o, d in (("Patch", 1), ("Dipole", 2)) if v[d] > 0],
        }
        examples.append(
            {"image": str(path), "family": f"S{i}", "split": "test" if i % 5 == 0 else "train", "labels": labels}
        )
        vectors[embed.file_sha256(path)] = v
    trained, report = heads.train_heads(examples, vectors, SCHEMA)
    heads.save_heads(heads.heads_path(BACKBONE, out_dir), trained, report, BACKBONE, SCHEMA)


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _image_with_embedding(conn, path: Path, vector: list[float], in_db: bool = True) -> int | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=(9, 9, 9)).save(path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
        (embed.file_sha256(path), BACKBONE, DIM, embed.vector_to_blob(vector), now),
    )
    image_id = None
    if in_db:
        image_id = conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('S', ?, 'active', ?)", (str(path), now)
        ).lastrowid
    conn.commit()
    return image_id


# ---- Probabilities -> field values ----


def test_to_field_values():
    probs = {
        "Shape": {"Rectangular": 0.9, "Cylindrical": 0.1},
        "Antennas": {"Patch": 0.97, "Dipole": 0.3},
        "Retired": {"A": 1.0},  # no longer in the attribute list
    }
    out = predict.to_field_values(probs, SCHEMA, {"Shape": "single", "Antennas": "multi", "Retired": "single"})
    assert out["Shape"]["value"] == "Rectangular" and out["Shape"]["confidence"] == pytest.approx(0.9)
    assert out["Antennas"]["value"] == ["Patch"]
    assert out["Antennas"]["confidence"] == pytest.approx(0.7)  # the least sure yes/no (Dipole: 70% no)
    assert not out["Antennas"]["unsure"]
    assert set(out) == {"Shape", "Antennas"}


def test_to_field_values_edge_cases():
    out = predict.to_field_values({"Antennas": {"Patch": 0.2, "Dipole": 0.45}}, SCHEMA, {"Antennas": "multi"})
    assert out["Antennas"]["value"] == ["None"] and out["Antennas"]["unsure"]  # 55% sure Dipole isn't there
    # An option removed from the list since training: the rest is renormalized.
    schema = {"Body": {"Shape": ["Dropdown", ["Cylindrical", "Cubical"]]}}
    out = predict.to_field_values({"Shape": {"Rectangular": 0.6, "Cylindrical": 0.3, "Cubical": 0.1}}, schema)
    assert out["Shape"]["value"] == "Cylindrical" and out["Shape"]["confidence"] == pytest.approx(0.75)
    # A pick-one that became a multi-select: the head answers a different question.
    multi = {"Body": {"Shape": ["Multi-Select", ["Rectangular", "Cylindrical"]]}}
    assert (
        predict.to_field_values({"Shape": {"Rectangular": 0.9, "Cylindrical": 0.1}}, multi, {"Shape": "single"}) == {}
    )


# ---- Suggesting for an image ----


def test_suggest_uses_the_stored_embedding_and_saves_guesses(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    image_id = _image_with_embedding(conn, tmp_path / "img" / "a.jpg", [3.0, 3.0, -3.0] + [0.0] * (DIM - 3))

    def no_download(name):
        raise AssertionError("the stored embedding should have been used")

    result = predict.suggest(conn, tmp_path / "img" / "a.jpg", SCHEMA, image_id, out_dir, backbone_factory=no_download)

    s = result["suggestions"]
    assert s["Shape"]["value"] == "Rectangular" and s["Antennas"]["value"] == ["Patch"]
    assert "Manufacturer" not in s
    rows = {r["attribute"]: r for r in conn.execute("SELECT * FROM predictions")}
    assert set(rows) == {"Shape", "Antennas"}
    assert json.loads(rows["Antennas"]["value"]) == ["Patch"]
    assert rows["Shape"]["model_version"] == result["model"]["version"]
    assert result["model"]["version"].startswith(f"{BACKBONE}@")

    predict.suggest(conn, tmp_path / "img" / "a.jpg", SCHEMA, image_id, out_dir, backbone_factory=no_download)
    assert conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"] == 2  # re-run updates, no duplicates


def test_suggest_embeds_a_new_image_on_the_spot(conn, tmp_path):
    out_dir = tmp_path / "training"
    _train(out_dir, tmp_path)
    path = tmp_path / "new.png"
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(path)

    class FakeBackbone:
        def __init__(self, name):
            self.name = name

        def embed(self, images):
            return [[-3.0, -3.0, 3.0] + [0.0] * (DIM - 3) for _ in images]

    predict._loaded["backbones"].clear()
    result = predict.suggest(conn, path, SCHEMA, None, out_dir, backbone_factory=FakeBackbone)
    predict._loaded["backbones"].clear()
    assert result["suggestions"]["Shape"]["value"] == "Cylindrical"
    assert result["suggestions"]["Antennas"]["value"] == ["Dipole"]
    assert conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"] == 1  # kept for next time
    assert conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"] == 0  # not in the DB: nothing saved


def test_no_model_yet(conn, tmp_path):
    assert predict.model_info(tmp_path / "training") == {
        "available": False,
        "message": "No trained model yet -- train one on the Train model page.",
    }
    with pytest.raises(predict.NoModelError):
        predict.suggest(conn, tmp_path / "x.jpg", SCHEMA, None, tmp_path / "training")


# ---- Endpoints ----


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "web.db"
    web_app._jobs.clear()
    (tmp_path / "attributes.json").write_text(json.dumps(SCHEMA))
    with TestClient(web_app.app) as test_client:
        yield test_client


def test_endpoints_without_a_model(client, tmp_path):
    assert client.get("/api/annotate/model").json()["available"] is False
    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    image_id = _image_with_embedding(conn, tmp_path / "img" / "a.jpg", [0.0] * DIM)
    conn.close()
    assert client.post(f"/api/annotate/{image_id}/suggest").status_code == 409
    # Not shadowed by the {image_id} route: folder mode is just off here.
    assert client.post("/api/annotate/folder/suggest", json={"name": "a.jpg"}).status_code == 400


def test_endpoints_with_a_model(client, tmp_path):
    _train(Path("training"), tmp_path)
    info = client.get("/api/annotate/model").json()
    assert info["available"] and info["backbone"] == BACKBONE and info["label"] == embed.BACKBONE_LABELS[BACKBONE]

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    image_id = _image_with_embedding(conn, tmp_path / "img" / "a.jpg", [3.0, -3.0, 3.0] + [0.0] * (DIM - 3))
    conn.close()
    data = client.post(f"/api/annotate/{image_id}/suggest").json()
    assert data["suggestions"]["Shape"]["value"] == "Rectangular"
    assert data["suggestions"]["Antennas"]["value"] == ["Dipole"]
    assert client.post("/api/annotate/999/suggest").status_code == 404
