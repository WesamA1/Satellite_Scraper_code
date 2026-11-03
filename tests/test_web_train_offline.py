"""Offline tests for the Train model page: status, remembered settings, and
the training-set / train-heads jobs. Synthetic embeddings, no real model."""

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import workspace
from scraper.stages import embed
from scraper.web import app as web_app

SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
    }
}
HEADER = "name,Shape,Antennae\r\n"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    web_app._jobs.clear()
    (tmp_path / "attributes.json").write_text(json.dumps(SCHEMA))
    with TestClient(web_app.app) as test_client:
        yield test_client


def _drain(client, job_id, timeout=20):
    events = []
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
        start = time.time()
        for line in resp.iter_lines():
            if time.time() - start > timeout:
                break
            if line and line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
                if events[-1]["type"] in ("done", "error"):
                    break
    return events


def _old_tool(tmp_path: Path, n: int = 60):
    """An old-tool CSV + image folder where each image's (fake) embedding
    decides its labels."""
    rng = random.Random(3)
    images = tmp_path / "imagesAnnotated"
    images.mkdir()
    lines, vectors = [], {}
    for i in range(n):
        path = images / f"SAT{i}_01.png"
        Image.new("RGB", (2, 2), color=(i, 0, 0)).save(path)
        v = [rng.gauss(0, 1) for _ in range(8)]
        shape = "Rectangular" if v[0] > 0 else "Cylindrical"
        antennas = [o for o, d in (("Patch", 1), ("Dipole", 2)) if v[d] > 0] or ["None"]
        lines.append(f'{path.name},{shape},"{antennas}"')
        vectors[embed.file_sha256(path)] = v
    csv_path = tmp_path / "annotatedInfo.csv"
    csv_path.write_text(HEADER + "\r\n".join(lines) + "\r\n")
    return csv_path, images, vectors


def test_train_page_renders_and_is_in_the_nav(client):
    resp = client.get("/train")
    assert resp.status_code == 200
    assert 'href="/train"' in resp.text and "Train model" in resp.text


def test_status_starts_empty(client):
    data = client.get("/api/train/status").json()
    assert data["dataset"] is None
    assert data["heads"] == {name: None for name in embed.BACKBONES}
    assert data["embedded"] == {name: 0 for name in embed.BACKBONES}
    assert data["settings"]["model"] == embed.DEFAULT_BACKBONE


def test_build_training_set_job_remembers_settings(client, tmp_path):
    csv_path, images, _ = _old_tool(tmp_path)
    resp = client.post(
        "/api/jobs/training-set",
        json={
            "csvs": [{"annotator": "", "path": str(csv_path)}],
            "image_dirs": [str(images)],
            "include_database": True,
        },
    )
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done", events
    result = events[-1]["result"]
    assert result["examples"]["train"] + result["examples"]["test"] == 60

    data = client.get("/api/train/status").json()
    assert data["dataset"]["examples"] == result["examples"]
    assert data["settings"]["csvs"] == [{"annotator": "", "path": str(csv_path)}]
    assert data["settings"]["image_dirs"] == [str(images)]


def test_build_needs_something_to_build_from(client):
    resp = client.post("/api/jobs/training-set", json={"csvs": [{"path": " "}], "include_database": False})
    assert resp.status_code == 400


def test_bad_csv_path_fails_the_job_with_a_message(client, tmp_path):
    resp = client.post("/api/jobs/training-set", json={"csvs": [{"path": str(tmp_path / "missing.csv")}]})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "error" and "No CSV" in events[-1]["message"]


def test_train_heads_job_end_to_end(client, tmp_path):
    csv_path, images, vectors = _old_tool(tmp_path, n=120)
    resp = client.post("/api/jobs/training-set", json={"csvs": [{"path": str(csv_path)}], "image_dirs": [str(images)]})
    assert _drain(client, resp.json()["job_id"])[-1]["type"] == "done"

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, 8, ?, ?)",
        [(sha, embed.DEFAULT_BACKBONE, embed.vector_to_blob(v), now) for sha, v in vectors.items()],
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/jobs/train-heads", json={"model": embed.DEFAULT_BACKBONE})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done", events
    assert set(events[-1]["result"]["attributes"]) == {"Shape", "Antennas"}

    data = client.get("/api/train/status").json()
    assert data["embedded"][embed.DEFAULT_BACKBONE] == 120
    assert data["heads"][embed.DEFAULT_BACKBONE]["attributes"]["Shape"]["accuracy"] > 0.7


def test_train_heads_without_a_training_set_fails_clearly(client):
    resp = client.post("/api/jobs/train-heads", json={"model": embed.DEFAULT_BACKBONE})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "error" and "build-training-set" in events[-1]["message"]


@pytest.mark.parametrize("url", ["/api/jobs/train-heads", "/api/jobs/embed"])
def test_unknown_model_is_rejected(client, url):
    assert client.post(url, json={"model": "resnet-from-somewhere"}).status_code == 400


def test_only_one_training_step_at_a_time(client):
    web_app._jobs["x"] = {"target": "train", "status": "running", "label": "Train model", "finished_at": None}
    assert client.post("/api/jobs/train-heads", json={}).status_code == 409


def test_csv_args():
    assert workspace.csv_args([{"annotator": "alice", "path": "a.csv"}, {"path": "b.csv"}, {"path": "  "}]) == [
        "alice=a.csv",
        "b.csv",
    ]
