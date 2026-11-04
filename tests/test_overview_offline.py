"""Offline tests for the Status page's overview: recorded scrape runs, the
filter's marker, alerts, where images are in the pipeline, source health and
coverage. No network."""

import json
import threading
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper import jobs
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import overview, scrape_runs
from scraper.web import app as web_app


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _image(conn, family, source, status="active", origin="scraped", reviewed=False, filtered=False, reason=None):
    image_id = conn.execute(
        """INSERT INTO images (family, path, status, created_at, reviewed_at, filtered_at, origin, reject_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            family,
            f"{family}_{source}_{_now()}.jpg",
            status,
            _now(),
            _now() if reviewed else None,
            _now() if filtered else None,
            origin,
            reason,
        ),
    ).lastrowid
    if origin == "scraped":
        conn.execute(
            "INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)"
            " VALUES (?, ?, ?, 'u', ?)",
            (image_id, source, f"{source}-{image_id}", _now()),
        )
    conn.commit()
    return image_id


def _empty_search(conn, family, source, n=1):
    for _ in range(n):
        conn.execute(
            "INSERT INTO rejections (family, source_module, reason, rejected_at) VALUES (?, ?, 'no_images_found', ?)",
            (family, source, _now()),
        )
    conn.commit()


# ---- Scrape runs ----


def test_scrape_job_records_its_run(conn, tmp_path, monkeypatch):
    def fake_download(conn_, session, family, **kwargs):
        _image(conn_, family, "wikimedia")
        _empty_search(conn_, family, "nasa")
        return {"wikimedia": {"downloaded": 1}, "nasa": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)
    jobs.run_scrape_job(conn, ["A", "B"], ("wikimedia", "nasa"), None, tmp_path)

    (run,) = scrape_runs.recent(conn)
    assert (run["status"], run["families"], run["families_done"], run["images_added"]) == ("finished", ["A", "B"], 2, 2)
    assert run["summary"]["wikimedia"]["images"] == 2 and run["summary"]["nasa"]["empty_searches"] == 2


def test_stopped_and_failed_runs_say_so(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)
    cancel = threading.Event()
    cancel.set()
    jobs.run_scrape_job(conn, ["A"], ("wikimedia",), None, tmp_path, cancel=cancel)

    def boom(*a, **k):
        raise RuntimeError("source fell over")

    monkeypatch.setattr(jobs, "download_family", boom)
    with pytest.raises(RuntimeError):
        jobs.run_scrape_job(conn, ["A"], ("wikimedia",), None, tmp_path)
    failed, stopped = scrape_runs.recent(conn)
    assert stopped["status"] == "stopped"
    assert failed["status"] == "failed" and "source fell over" in failed["error"]


def test_a_run_that_never_ended_shows_as_not_finished(conn):
    scrape_runs.start(conn, ["A"], ["wikimedia"], "2026-01-01T00:00:00+00:00")
    assert overview._runs(conn)[0]["status"] == "didnt_finish"


# ---- The overview ----


def test_pipeline_alerts_sources_and_coverage(conn, tmp_path, monkeypatch):
    enabled = {
        "wikimedia": types.SimpleNamespace(),
        "usgs": types.SimpleNamespace(),
        "gunters_space": types.SimpleNamespace(),
        "flickr": types.SimpleNamespace(REQUIRES_API_KEY=True),
    }
    monkeypatch.setattr(overview.registry, "list_sources", lambda: enabled)
    monkeypatch.setattr(overview.secrets, "get_source_api_key", lambda name: None)

    _image(conn, "A", "wikimedia")  # new: not filtered, not reviewed
    _image(conn, "A", "wikimedia", filtered=True)  # filtered, waiting for review
    kept = _image(conn, "B", "wikimedia", filtered=True, reviewed=True)
    annotated = _image(conn, "B", "wikimedia", filtered=True, reviewed=True)
    conn.execute(
        "INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)"
        " VALUES (?, 'Shape', ?, 'alice', 'v1', ?)",
        (annotated, json.dumps("Cubical"), _now()),
    )
    _image(conn, "C", "wikimedia", status="rejected_filter", reason="not_an_image")
    _empty_search(conn, "C", "usgs", 12)
    conn.commit()
    catalog = tmp_path / "satellites.csv"
    rows = "".join(f"1,x,{f},{f}\n" for f in "ABCD")
    catalog.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n" + rows)
    (tmp_path / "training").mkdir()
    (tmp_path / "training" / "dataset.jsonl").write_text('{"a": 1}\n{"b": 2}\n')

    o = overview.build(conn, catalog, tmp_path / "training")

    assert o["pipeline"] == {
        "waiting_for_filter": 1,
        "waiting_for_review": 1,
        "kept_not_annotated": 1,
        "annotated": 1,
        "training_examples": 2,
        "set_aside": {"not_an_image": 1},
    }
    texts = " | ".join(a["text"] for a in o["alerts"])
    assert o["alerts"][0]["level"] == "bad" and "Gunter's Space Page is switched on" in o["alerts"][0]["text"]
    assert "1 scraped image(s) haven't been through the filter" in texts
    assert "usgs has never found an image (12 searches)" in texts
    assert "flickr needs an API key" in texts
    assert "1 image(s) waiting for review" in texts
    by_name = {s["source"]: s for s in o["sources"]}
    assert by_name["wikimedia"]["images"] == 5 and by_name["wikimedia"]["active"] == 4
    assert by_name["wikimedia"]["not_an_image"] == 1
    assert by_name["usgs"]["empty_share"] == 1.0
    assert o["coverage"] == {"families": 4, "with_images": 2, "searched_nothing_kept": 1, "never_searched": 1}
    assert kept


def test_nothing_to_flag(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(overview.registry, "list_sources", lambda: {"wikimedia": types.SimpleNamespace()})
    assert overview.build(conn, tmp_path / "none.csv", tmp_path / "training")["alerts"] == []


def test_filter_marks_what_it_scored(conn, tmp_path):
    from PIL import Image

    from scraper.stages.filter import run_filter

    path = tmp_path / "SAT" / "a.jpg"
    path.parent.mkdir()
    Image.new("RGB", (4, 4)).save(path)
    image_id = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('SAT', ?, 'active', 'now')", (str(path),)
    ).lastrowid
    conn.commit()
    run_filter(conn, tmp_path / "_quarantine", scorer=lambda paths: [0.9 for _ in paths], dry_run=True)
    assert conn.execute("SELECT filtered_at FROM images WHERE id = ?", (image_id,)).fetchone()[0] is None
    run_filter(conn, tmp_path / "_quarantine", scorer=lambda paths: [0.9 for _ in paths])
    assert conn.execute("SELECT filtered_at FROM images WHERE id = ?", (image_id,)).fetchone()[0] is not None


def test_overview_endpoint_and_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "web.db"
    web_app._jobs.clear()
    with TestClient(web_app.app) as client:
        data = client.get("/api/overview").json()
        assert set(data) == {"alerts", "pipeline", "runs", "sources", "coverage"}
        assert 'id="overview"' in client.get("/status").text
    assert json.dumps(data)
    assert not Path("logs").exists()  # tests keep out of logs/
