"""Offline tests for the screening check set: sampling, labels, never
overwriting, and the labeling page's endpoints."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import check_set
from scraper.web import app as web_app


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    conn = get_connection(tmp_path / "scraper.db")
    apply_migrations(conn)
    for i in range(6):
        path = tmp_path / "satellite_images" / "SAT" / f"SAT_{i}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color=(i * 40, 0, 0)).save(path)
        conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('SAT', ?, 'active', 'now')", (str(path),)
        )
    copy = tmp_path / "satellite_images" / "SAT" / "copy_of_0.jpg"
    copy.write_bytes((tmp_path / "satellite_images" / "SAT" / "SAT_0.jpg").read_bytes())
    broken = tmp_path / "satellite_images" / "SAT" / "broken.jpg"
    broken.write_bytes(b"not an image")
    for path in (copy, broken):
        conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('SAT', ?, 'active', 'now')", (str(path),)
        )
    conn.commit()
    yield tmp_path, conn
    conn.close()


def test_create_samples_distinct_readable_images(project):
    root, conn = project
    result = check_set.create(conn, size=50)
    assert result["total"] == 6 and result["labeled"] == 0  # 8 rows: one copy and one broken file left out
    assert all(item["image"].startswith("satellite_images/") for item in result["items"])
    assert (root / "data" / "check_sets" / "screening.csv").is_file()


def test_never_overwrites(project):
    _, conn = project
    check_set.create(conn, size=3)
    with pytest.raises(check_set.CheckSetError, match="already exists"):
        check_set.create(conn, size=3)


def test_label_and_correct(project):
    _, conn = project
    first = check_set.create(conn, size=3)["items"][0]["sha256"]
    progress = check_set.label(first, "1", "no", "alice")
    assert progress["labeled"] == 1 and progress["total"] == 3
    check_set.label(first, "2+", "yes", "alice")  # a correction; recognizable only applies to one satellite
    row = next(r for r in check_set.load() if r["sha256"] == first)
    assert (row["satellites"], row["recognizable"], row["labeled_by"]) == ("2+", "", "alice")


@pytest.mark.parametrize("satellites,recognizable", [("3", ""), ("1", ""), ("1", "maybe")])
def test_label_rejects_bad_answers(project, satellites, recognizable):
    _, conn = project
    first = check_set.create(conn, size=2)["items"][0]["sha256"]
    with pytest.raises(check_set.CheckSetError):
        check_set.label(first, satellites, recognizable)


def test_only_listed_images_are_served(project):
    _, conn = project
    check_set.create(conn, size=2)
    with pytest.raises(check_set.CheckSetError):
        check_set.image_path("0" * 64)


def test_endpoints(project):
    root, _ = project
    web_app.DB_PATH = root / "scraper.db"
    web_app._jobs.clear()
    with TestClient(web_app.app) as client:
        assert client.get("/check-set").status_code == 200
        assert client.get("/api/check-set").json()["exists"] is False
        created = client.post("/api/check-set/create", json={"size": 4}).json()
        assert created["total"] == 4
        sha = created["items"][0]["sha256"]
        assert client.get("/api/check-set/image", params={"sha256": sha}).status_code == 200
        resp = client.post("/api/check-set/label", json={"sha256": sha, "satellites": "1", "recognizable": "yes"})
        assert resp.json()["labeled"] == 1
        assert client.post("/api/check-set/label", json={"sha256": sha, "satellites": "1"}).status_code == 400
        assert client.post("/api/check-set/create", json={"size": 4}).status_code == 400
        assert 'href="/check-set"' in client.get("/train").text
    assert json.loads(json.dumps(check_set.status()))["labeled"] == 1
    assert Path("data/check_sets/screening.csv").is_file()
