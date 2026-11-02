"""Offline tests for the web GUI. All network-free -- download_family and
wikimedia.login are monkeypatched exactly like test_cli_offline.py, since the
web app runs the same scraper.jobs functions underneath."""

import csv
import queue
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper import codegen, jobs
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.web import app as web_app

CANDIDATES_DIR = Path(codegen.__file__).parent / "sources" / "candidates"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    # _jobs is a module-level dict that otherwise persists for the whole
    # process -- correct for the real app, but across tests it leaks a
    # "running" entry from one test into the next, which the concurrency
    # guard (_reject_if_already_running) would otherwise spuriously trip on.
    web_app._jobs.clear()
    with TestClient(web_app.app) as test_client:
        yield test_client


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    path = tmp_path / "satellites.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["NORAD_CAT_ID", "OBJECT_NAME", "FAMILY_NAME", "VARIANT_NAME"])
        for i in range(3):
            writer.writerow([i, f"SAT {i}", f"Family {i}", f"Family {i}"])
    return path


def _drain(client, job_id, timeout=5):
    events = []
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
        start = time.time()
        for line in resp.iter_lines():
            if time.time() - start > timeout:
                break
            if not line or not line.startswith("data:"):
                continue
            import json

            events.append(json.loads(line[len("data:") :].strip()))
            if events[-1]["type"] in ("done", "error"):
                break
    return events


def test_root_serves_standalone_splash(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "GROUND STATION" in resp.text
    assert "REV. 3.0" in resp.text
    assert 'href="/scrape"' in resp.text  # the "Enter the pipeline" CTA
    assert "sidebar-nav" not in resp.text  # splash is standalone, not the app shell


def test_scrape_job_streams_log_progress_and_done(client, catalog, monkeypatch):
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 1}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "Family 0,Family 1", "source": "wikimedia"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["families"] == ["Family 0", "Family 1"]

    events = _drain(client, job_id)
    types = [e["type"] for e in events]
    assert "progress" in types
    assert types[-1] == "done"

    # The running images-found total shown next to the progress bar --
    # 1 image/family here, so it should climb 1, then 2.
    progress_events = [e for e in events if e["type"] == "progress"]
    assert [e["images_found"] for e in progress_events] == [1, 2]


def test_scrape_job_streams_status_events_for_the_current_source(client, catalog, monkeypatch):
    def fake_download_family(conn, session, family_name, **kwargs):
        kwargs["on_source_start"]("wikimedia", 1, 2)
        kwargs["on_source_start"]("nasa", 2, 2)
        return {"wikimedia": {"downloaded": 0}, "nasa": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "Family 0", "source": "wikimedia,nasa"})
    events = _drain(client, resp.json()["job_id"])

    status_events = [e for e in events if e["type"] == "status"]
    assert status_events == [
        {"type": "status", "family": "Family 0", "source": "wikimedia", "source_index": 1, "source_total": 2},
        {"type": "status", "family": "Family 0", "source": "nasa", "source_index": 2, "source_total": 2},
    ]


def test_scrape_job_streams_item_progress_within_a_source(client, catalog, monkeypatch):
    def fake_download_family(conn, session, family_name, **kwargs):
        kwargs["on_source_start"]("dpla", 1, 1)
        kwargs["on_item_progress"]("dpla", 1, 81)
        kwargs["on_item_progress"]("dpla", 2, 81)
        return {"dpla": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "A-1", "source": "dpla"})
    events = _drain(client, resp.json()["job_id"])

    status_events = [e for e in events if e["type"] == "status"]
    assert status_events == [
        {"type": "status", "family": "A-1", "source": "dpla", "source_index": 1, "source_total": 1},
        {"type": "status", "family": "A-1", "source": "dpla", "item_index": 1, "item_total": 81},
        {"type": "status", "family": "A-1", "source": "dpla", "item_index": 2, "item_total": 81},
    ]


def test_status_snapshot_keeps_source_info_through_item_only_ticks(client):
    """Confirmed live: reattaching mid-family showed "(0 / 0)" for the
    source count even though the job had been reporting e.g. "(3 / 8)" all
    along. Item-only status ticks (far more frequent than source-start
    ticks) carry no source_index/source_total at all -- replacing the whole
    last-known status with each new event, instead of merging onto it, loses
    that the moment the first item tick for a source arrives."""
    entry = {"queue": queue.Queue(), "last_events": {}}

    web_app._push(entry, {"type": "status", "family": "A-1", "source": "dpla", "source_index": 3, "source_total": 8})
    web_app._push(entry, {"type": "status", "family": "A-1", "source": "dpla", "item_index": 1, "item_total": 81})
    web_app._push(entry, {"type": "status", "family": "A-1", "source": "dpla", "item_index": 20, "item_total": 81})

    assert entry["last_events"]["status"] == {
        "type": "status",
        "family": "A-1",
        "source": "dpla",
        "source_index": 3,
        "source_total": 8,
        "item_index": 20,
        "item_total": 81,
    }

    # A new source starting drops the previous source's leftover item
    # count -- it doesn't apply to the new source and hasn't ticked yet.
    web_app._push(entry, {"type": "status", "family": "A-1", "source": "nasa", "source_index": 4, "source_total": 8})

    assert entry["last_events"]["status"] == {
        "type": "status",
        "family": "A-1",
        "source": "nasa",
        "source_index": 4,
        "source_total": 8,
    }


def test_reattaching_to_a_quiet_job_shows_the_last_known_progress_and_status(client):
    """Reattaching (the activity strip's View link, or just navigating back
    to a page with a job still running on it) opens a fresh connection to
    the job's stream. If the job has gone quiet since the last consumer
    caught up -- e.g. mid-backoff pause -- the queue is empty, and without
    a snapshot the new connection would sit blank until the next tick."""
    import threading

    entry = {
        "queue": queue.Queue(),
        "cancel": threading.Event(),
        "status": "running",
        "label": "Scrape",
        "target": "scrape",
        "finished_at": None,
    }
    job_id = "reattach-test-job"
    web_app._jobs[job_id] = entry

    # A previous consumer already drained these before disconnecting --
    # last_events stays set, but the queue itself is empty, same as a job
    # that's been quietly paused for a while.
    web_app._push(entry, {"type": "status", "family": "Family 0", "source": "wikimedia"})
    web_app._push(entry, {"type": "progress", "completed": 1, "total": 3, "images_found": 2})
    while not entry["queue"].empty():
        entry["queue"].get_nowait()

    def finish_soon():
        time.sleep(0.2)
        web_app._push(entry, {"type": "done", "result": {}})

    threading.Thread(target=finish_soon, daemon=True).start()
    events = _drain(client, job_id, timeout=3)

    assert events[0] == {"type": "progress", "completed": 1, "total": 3, "images_found": 2}
    assert events[1] == {"type": "status", "family": "Family 0", "source": "wikimedia"}
    assert events[-1]["type"] == "done"


def test_scrape_from_scratch_clears_negative_cache(client, catalog, monkeypatch):
    from scraper import license_gate
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO rejections (family, source_module, external_id, reason, rejected_at) "
        "VALUES ('Family 0', 'wikimedia', NULL, 'no_images_found', datetime('now'))"
    )
    conn.commit()
    assert license_gate.has_negative_cache(conn, "Family 0", "wikimedia")
    conn.close()

    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 0}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "Family 0", "source": "wikimedia", "from_scratch": True})
    _drain(client, resp.json()["job_id"])

    conn = get_connection(web_app.DB_PATH)
    assert not license_gate.has_negative_cache(conn, "Family 0", "wikimedia")
    conn.close()


def test_retry_failed_searches_endpoint_clears_cache_without_starting_a_job(client, catalog):
    from scraper import license_gate
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO rejections (family, source_module, external_id, reason, rejected_at) "
        "VALUES ('Family 0', 'wikimedia', NULL, 'no_images_found', datetime('now'))"
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/retry-failed-searches", json={"family": "Family 0"})
    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1, "families": 1}

    conn = get_connection(web_app.DB_PATH)
    assert not license_gate.has_negative_cache(conn, "Family 0", "wikimedia")
    conn.close()
    assert web_app._jobs == {}  # no job was started -- this is a synchronous DB write only


def test_scrape_job_respects_profile_family_limit(client, catalog, monkeypatch):
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 1}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"profile": "smoke", "catalog": str(catalog), "source": "wikimedia"})
    families = resp.json()["families"]
    assert len(families) == 3  # fixture catalog only has 3 -- below the smoke cap of 5


def test_cancel_endpoint_stops_the_job(client, catalog, monkeypatch):
    import threading

    started = threading.Event()

    def fake_download_family(conn, session, family_name, **kwargs):
        started.set()
        # Gives the main thread a window to actually call /cancel before this
        # thread races on to the next family -- with no delay at all, a fake
        # this cheap can blow through all 3 families before the main thread
        # (woken by started.wait()) gets scheduled again.
        time.sleep(0.05)
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "Family 0,Family 1,Family 2", "source": "wikimedia"})
    job_id = resp.json()["job_id"]
    started.wait(timeout=2)
    client.post(f"/api/jobs/{job_id}/cancel")

    events = _drain(client, job_id)
    done_event = next(e for e in events if e["type"] == "done")
    assert done_event["result"]["interrupted"] is True


def test_second_concurrent_scrape_is_rejected(client, catalog, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocking_download_family(conn, session, family_name, **kwargs):
        started.set()
        release.wait(timeout=5)
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", blocking_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    first = client.post("/api/jobs/scrape", json={"family": "Family 0", "source": "wikimedia"})
    assert first.status_code == 200
    started.wait(timeout=2)

    second = client.post("/api/jobs/scrape", json={"family": "Family 1", "source": "wikimedia"})
    assert second.status_code == 409

    release.set()
    _drain(client, first.json()["job_id"])

    # Once the first job has finished, a new scrape is allowed again.
    third = client.post("/api/jobs/scrape", json={"family": "Family 1", "source": "wikimedia"})
    assert third.status_code == 200
    _drain(client, third.json()["job_id"])


def test_unknown_job_id_returns_404(client):
    resp = client.get("/api/jobs/does-not-exist/stream")
    assert resp.status_code == 404
    resp = client.post("/api/jobs/does-not-exist/cancel")
    assert resp.status_code == 404


def test_filter_job_dry_run(client, monkeypatch):
    from datetime import datetime, timezone

    from PIL import Image

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations
    from scraper.stages import filter as filter_module

    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: lambda paths: [0.1 for _ in paths])

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    img_path = web_app.DB_PATH.parent / "img.jpg"
    Image.new("RGB", (4, 4)).save(img_path)
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
        (str(img_path), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/jobs/filter", json={"dry_run": True, "min_score": 0.99})
    job_id = resp.json()["job_id"]
    events = _drain(client, job_id)
    assert events[-1]["type"] == "done"
    assert img_path.exists()  # dry-run never moves anything


def test_filter_job_streams_progress_events(client, monkeypatch):
    from datetime import datetime, timezone

    from PIL import Image

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations
    from scraper.stages import filter as filter_module

    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: lambda paths: [0.9 for _ in paths])

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    for i in range(3):
        img_path = web_app.DB_PATH.parent / f"img{i}.jpg"
        Image.new("RGB", (4, 4)).save(img_path)
        conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
            (str(img_path), datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    conn.close()

    resp = client.post("/api/jobs/filter", json={})
    events = _drain(client, resp.json()["job_id"])

    progress_events = [e for e in events if e["type"] == "progress"]
    assert progress_events == [{"type": "progress", "completed": 3, "total": 3}]


def test_second_concurrent_filter_is_rejected(client, monkeypatch):
    import threading

    from scraper.stages import filter as filter_module

    release = threading.Event()

    def blocking_scorer(paths):
        release.wait(timeout=5)
        return [0.1 for _ in paths]

    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: blocking_scorer)

    first = client.post("/api/jobs/filter", json={"dry_run": True})
    assert first.status_code == 200

    second = client.post("/api/jobs/filter", json={"dry_run": True})
    assert second.status_code == 409

    release.set()
    _drain(client, first.json()["job_id"])


def test_finished_jobs_are_pruned_once_stale(client):
    """_jobs otherwise grows for the life of the server process -- a finished
    entry older than the TTL gets swept the next time a job starts."""
    resp = client.post("/api/jobs/embed")
    job_id = resp.json()["job_id"]
    _drain(client, job_id)
    assert job_id in web_app._jobs

    web_app._jobs[job_id]["finished_at"] -= web_app.FINISHED_JOB_TTL_SECONDS + 1

    client.post("/api/jobs/embed")  # starting any new job sweeps stale entries

    assert job_id not in web_app._jobs


def test_embed_and_export_jobs(client):
    resp = client.post("/api/jobs/embed")
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"
    assert events[-1]["result"] == 0

    resp = client.post("/api/jobs/export", json={"out_path": "export/manifest.csv"})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"


def _insert_active_image(client, family="Family 0"):
    from datetime import datetime, timezone

    from PIL import Image

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    img_path = web_app.DB_PATH.parent / f"{family}_test.jpg"
    Image.new("RGB", (8, 8), color="blue").save(img_path)
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, str(img_path), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id, img_path


def test_dedupe_job_hashes_active_images(client):
    _insert_active_image(client)
    resp = client.post("/api/jobs/dedupe", json={})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["hashed"] == 1


def test_review_queue_and_decide_flow(client, catalog):
    image_id, img_path = _insert_active_image(client, family="Family 0")

    queue = client.get("/api/review/queue").json()["queue"]
    assert len(queue) == 1
    assert queue[0]["id"] == image_id

    resp = client.get(f"/api/images/{image_id}/file")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")

    resp = client.post(f"/api/review/{image_id}/decide", json={"decision": "keep"})
    assert resp.status_code == 200
    assert img_path.exists()  # kept in place

    # Reviewed images drop out of the queue.
    assert client.get("/api/review/queue").json()["queue"] == []


def test_review_decide_reject_moves_file_to_quarantine(client):
    image_id, img_path = _insert_active_image(client, family="Family 0")

    resp = client.post(f"/api/review/{image_id}/decide", json={"decision": "reject"})
    assert resp.status_code == 200
    assert not img_path.exists()

    resp = client.get(f"/api/images/{image_id}/file")
    assert resp.status_code == 200  # still servable from its new quarantine path


def test_review_decide_rejects_unknown_decision(client):
    image_id, _ = _insert_active_image(client)
    resp = client.post(f"/api/review/{image_id}/decide", json={"decision": "nope"})
    assert resp.status_code == 400


def test_review_variants_endpoint(client, catalog):
    resp = client.get("/api/review/variants", params={"family": "Family 0", "catalog": str(catalog)})
    assert resp.status_code == 200
    assert resp.json()["variants"] == []  # fixture catalog's VARIANT_NAME equals FAMILY_NAME


def test_review_set_and_read_back_variant(client):
    image_id, _ = _insert_active_image(client)
    resp = client.post(f"/api/review/{image_id}/variant", json={"variant": "STARLINK V1.5"})
    assert resp.status_code == 200

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations
    from scraper.stages import review as review_module

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    assert review_module.get_variant(conn, image_id) == "STARLINK V1.5"
    conn.close()


def test_image_file_endpoint_404s_for_unknown_id(client):
    resp = client.get("/api/images/999999/file")
    assert resp.status_code == 404


def test_status_endpoint_returns_json(client, catalog):
    resp = client.get("/api/status", params={"catalog": str(catalog)})
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 3
    assert all(row["state"] == "pending" for row in rows)


def test_status_endpoint_counts_images_from_a_promoted_source_not_just_builtins(client, catalog, monkeypatch):
    import types

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations
    from scraper.sources import registry as registry_module

    fake_module = types.SimpleNamespace(SOURCE_MODULE="fixture_promoted")
    monkeypatch.setattr(registry_module, "list_sources", lambda: {"fixture_promoted": fake_module})

    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    family = "Family 0"  # matches the `catalog` fixture's generated rows
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', datetime('now'))",
        (family, "x.jpg"),
    )
    conn.execute(
        """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
           VALUES (?, 'fixture_promoted', 'ext1', 'https://x.example', datetime('now'))""",
        (cur.lastrowid,),
    )
    conn.commit()
    conn.close()

    resp = client.get("/api/status", params={"catalog": str(catalog)})
    rows = {row["family"]: row for row in resp.json()["rows"]}

    # Before this fix, compute_status() only ever checked wikimedia/nasa, so a
    # family scraped exclusively via a promoted source stayed "pending"
    # forever with an undercounted image total.
    assert rows[family]["image_count"] == 1
    assert rows[family]["state"] == "done"


def test_release_notes_page_handles_a_missing_changelog_gracefully(client):
    resp = client.get("/release-notes")
    assert resp.status_code == 200
    assert "No release notes yet." in resp.text


def test_release_notes_page_renders_the_real_changelog(client):
    Path("CHANGELOG.md").write_text("# Changelog\n\n## 2026-11-01\n\n### Added\n- A real new feature.\n")

    resp = client.get("/release-notes")

    assert resp.status_code == 200
    assert "2026-11-01" in resp.text
    assert "Added" in resp.text
    assert "A real new feature." in resp.text


def test_doctor_endpoint_returns_json(client):
    resp = client.get("/api/doctor")
    assert resp.status_code == 200
    results = resp.json()["results"]
    names = {r["name"] for r in results}
    assert "gpu" in names
    assert "disk_space" in names


def test_discover_job_persists_candidates(client, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [
            {"name": "Openverse", "url": "https://openverse.org", "license_verifiable": True, "notes": "ok"}
        ],
    )

    resp = client.post("/api/jobs/discover", json={"prompt": "satellite imagery"})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"

    candidates = client.get("/api/candidates").json()["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["name"] == "Openverse"
    assert candidates[0]["status"] == "pending"


def test_add_manual_candidate(client):
    resp = client.post(
        "/api/candidates/manual",
        json={
            "url": "https://example.org/archive",
            "license_model": "CC BY 4.0",
            "notes": "Per-image license field in their API response.",
            "license_verifiable": True,
        },
    )
    assert resp.status_code == 200
    candidate_id = resp.json()["candidate_id"]

    candidates = client.get("/api/candidates").json()["candidates"]
    assert len(candidates) == 1
    row = candidates[0]
    assert row["id"] == candidate_id
    assert row["name"] == "example.org"  # defaulted from the URL, no name given
    assert row["status"] == "pending"  # same gate as an auto-discovered candidate
    assert row["license_verifiable"] == 1
    assert row["prompt"] == "manual entry"


def test_add_manual_candidate_requires_a_url(client):
    resp = client.post("/api/candidates/manual", json={"url": "  "})
    assert resp.status_code == 400


def test_load_seed_candidates_endpoint(client):
    resp = client.post("/api/candidates/seed")
    assert resp.status_code == 200
    added = resp.json()["added"]
    assert added > 0

    candidates = client.get("/api/candidates").json()["candidates"]
    assert len(candidates) == added
    assert all(c["status"] == "pending" for c in candidates)

    # Calling it again adds nothing new -- same URLs are already present.
    resp = client.post("/api/candidates/seed")
    assert resp.json()["added"] == 0


def test_approve_and_reject_candidate(client, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [
            {"name": "A", "url": "https://a.example"},
            {"name": "B", "url": "https://b.example"},
        ],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidates = client.get("/api/candidates").json()["candidates"]
    a_id = next(c["id"] for c in candidates if c["name"] == "A")
    b_id = next(c["id"] for c in candidates if c["name"] == "B")

    client.post(f"/api/candidates/{a_id}/approve")
    client.post(f"/api/candidates/{b_id}/reject")

    candidates = {c["id"]: c for c in client.get("/api/candidates").json()["candidates"]}
    assert candidates[a_id]["status"] == "approved"
    assert candidates[b_id]["status"] == "rejected"


def test_pending_filter_excludes_reviewed_candidates(client, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "A", "url": "https://a.example"}],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidate_id = client.get("/api/candidates").json()["candidates"][0]["id"]

    client.post(f"/api/candidates/{candidate_id}/approve")

    pending = client.get("/api/candidates", params={"pending": True}).json()["candidates"]
    assert pending == []


FAKE_GENERATED_MODULE = (
    "SOURCE_MODULE = 'fixture_web_candidate'\n"
    "def make_session(dev=False): pass\n"
    "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
    "def relevant_results(raw, query, domain_keywords=None): return []\n"
    "def extract_record(raw_item, query, retrieved_at): return None\n"
    "def download_image(session, conn, url, dest_path, sleep_fn=None): return False\n"
)


def test_codegen_job_writes_code_and_serves_it(client, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "Fixture Web Candidate", "url": "https://x.example"}],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidate_id = client.get("/api/candidates").json()["candidates"][0]["id"]
    client.post(f"/api/candidates/{candidate_id}/approve")

    monkeypatch.setattr(jobs.codegen, "generate_source_module", lambda candidate, client=None: FAKE_GENERATED_MODULE)

    resp = client.post("/api/jobs/codegen", json={"candidate_id": candidate_id})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"

    try:
        code_resp = client.get(f"/api/candidates/{candidate_id}/code")
        assert code_resp.status_code == 200
        assert code_resp.json()["code"] == FAKE_GENERATED_MODULE

        candidates = client.get("/api/candidates").json()["candidates"]
        assert candidates[0]["status"] == "code_generated"
    finally:
        (CANDIDATES_DIR / "fixture_web_candidate.py").unlink(missing_ok=True)


def test_get_code_404s_before_generation(client, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "No Code Yet", "url": "https://x.example"}],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidate_id = client.get("/api/candidates").json()["candidates"][0]["id"]

    resp = client.get(f"/api/candidates/{candidate_id}/code")
    assert resp.status_code == 404


def test_promote_endpoint_rejects_incomplete_module(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "Incomplete Web", "url": "https://x.example"}],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidate_id = client.get("/api/candidates").json()["candidates"][0]["id"]
    client.post(f"/api/candidates/{candidate_id}/approve")

    incomplete_source = "SOURCE_MODULE = 'incomplete_web'\ndef make_session(dev=False): pass\n"
    monkeypatch.setattr(jobs.codegen, "generate_source_module", lambda candidate, client=None: incomplete_source)
    resp = client.post("/api/jobs/codegen", json={"candidate_id": candidate_id})
    _drain(client, resp.json()["job_id"])

    try:
        resp = client.post(f"/api/candidates/{candidate_id}/promote")
        assert resp.status_code == 400
        assert "missing" in resp.json()["detail"]
    finally:
        (CANDIDATES_DIR / "incomplete_web.py").unlink(missing_ok=True)


def test_promote_endpoint_succeeds_for_a_complete_module(client, monkeypatch, tmp_path):
    import sys

    from scraper import promotion as promotion_module

    enabled_path = tmp_path / "enabled_sources.yaml"
    enabled_path.write_text("[]")
    monkeypatch.setattr(promotion_module, "ENABLED_SOURCES_PATH", enabled_path)

    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "Complete Web", "url": "https://x.example"}],
    )
    resp = client.post("/api/jobs/discover", json={"prompt": "x"})
    _drain(client, resp.json()["job_id"])
    candidate_id = client.get("/api/candidates").json()["candidates"][0]["id"]
    client.post(f"/api/candidates/{candidate_id}/approve")

    complete_source = (
        "SOURCE_MODULE = 'complete_web'\n"
        "def make_session(dev=False): pass\n"
        "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
        "def relevant_results(raw, query, domain_keywords=None): return []\n"
        "def extract_record(raw_item, query, retrieved_at): return None\n"
        "def download_image(session, conn, url, dest_path, sleep_fn=None): return False\n"
    )
    monkeypatch.setattr(jobs.codegen, "generate_source_module", lambda candidate, client=None: complete_source)
    resp = client.post("/api/jobs/codegen", json={"candidate_id": candidate_id})
    _drain(client, resp.json()["job_id"])

    try:
        resp = client.post(f"/api/candidates/{candidate_id}/promote")
        assert resp.status_code == 200
        assert resp.json()["promoted_to"].endswith("complete_web.py")

        candidates = client.get("/api/candidates").json()["candidates"]
        assert candidates[0]["status"] == "promoted"
    finally:
        (CANDIDATES_DIR / "complete_web.py").unlink(missing_ok=True)
        (promotion_module.SOURCES_DIR / "complete_web.py").unlink(missing_ok=True)
        sys.modules.pop("scraper.sources.complete_web", None)


def test_list_registered_sources(client):
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    assert set(resp.json()["sources"]) >= {"wikimedia", "nasa"}


def test_scrape_page_lists_every_registered_source_not_just_the_builtins(client, monkeypatch):
    import types

    from scraper.sources import registry as registry_module

    fake_module = types.SimpleNamespace(SOURCE_MODULE="fixture_promoted")
    monkeypatch.setattr(
        registry_module,
        "list_sources",
        lambda: {"wikimedia": fake_module, "nasa": fake_module, "fixture_promoted": fake_module},
    )

    resp = client.get("/scrape")

    assert resp.status_code == 200
    assert 'value="fixture_promoted"' in resp.text


def test_scrape_page_auto_checks_only_sources_that_are_actually_ready(client, monkeypatch):
    import types

    from scraper.sources import registry as registry_module

    monkeypatch.delenv("SOURCE_FIXTURE_PROMOTED_API_KEY", raising=False)
    no_key_needed = types.SimpleNamespace(SOURCE_MODULE="wikimedia")
    needs_key_missing = types.SimpleNamespace(SOURCE_MODULE="fixture_promoted", REQUIRES_API_KEY=True)
    monkeypatch.setattr(
        registry_module,
        "list_sources",
        lambda: {"wikimedia": no_key_needed, "fixture_promoted": needs_key_missing},
    )

    resp = client.get("/scrape")

    assert resp.status_code == 200
    # wikimedia needs no key -- pre-checked; fixture_promoted needs one that
    # isn't configured -- listed, but left unchecked, and flagged in the pill
    assert 'value="wikimedia" checked' in resp.text
    assert 'value="fixture_promoted" checked' not in resp.text
    assert "needs key" in resp.text


def test_scrape_page_auto_checks_a_source_whose_key_is_configured(client, monkeypatch):
    import types

    from scraper.sources import registry as registry_module

    monkeypatch.setenv("SOURCE_FIXTURE_PROMOTED_API_KEY", "fake-key-value")
    needs_key_present = types.SimpleNamespace(SOURCE_MODULE="fixture_promoted", REQUIRES_API_KEY=True)
    monkeypatch.setattr(registry_module, "list_sources", lambda: {"fixture_promoted": needs_key_present})

    resp = client.get("/scrape")

    assert 'value="fixture_promoted" checked' in resp.text


@pytest.mark.parametrize(
    "path,active_text",
    [
        ("/scrape", "Scrape"),
        ("/filter", "Filter"),
        ("/embed", "Embed"),
        ("/export", "Export"),
        ("/status", "Status"),
        ("/doctor", "Doctor"),
        ("/discovery", "Discovery"),
        ("/api-keys", "API Keys"),
        ("/review", "Review"),
        ("/annotate", "Annotate"),
        ("/attributes", "Attributes"),
    ],
)
def test_every_page_route_renders(client, path, active_text):
    resp = client.get(path)
    assert resp.status_code == 200
    assert active_text in resp.text


def test_brand_logo_links_back_to_splash(client):
    resp = client.get("/scrape")
    assert 'class="brand" href="/"' in resp.text


def test_pipeline_stage_band_only_on_pipeline_pages(client):
    assert "stage-band" in client.get("/scrape").text
    assert "stage-band" in client.get("/filter").text
    assert "stage-band" not in client.get("/status").text
    assert "stage-band" not in client.get("/discovery").text


def test_jobs_active_reports_running_jobs_and_pending_candidates(client, catalog, monkeypatch):
    import threading

    started = threading.Event()
    release = threading.Event()

    def fake_download_family(conn, session, family_name, **kwargs):
        started.set()
        release.wait(timeout=5)
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    resp = client.post("/api/jobs/scrape", json={"family": "Family 0", "source": "wikimedia"})
    job_id = resp.json()["job_id"]
    started.wait(timeout=2)

    active = client.get("/api/jobs/active").json()
    assert any(j["job_id"] == job_id and j["target"] == "scrape" for j in active["jobs"])

    release.set()
    _drain(client, job_id)

    active = client.get("/api/jobs/active").json()
    assert all(j["job_id"] != job_id for j in active["jobs"])


def test_auto_discovery_batch_walks_the_queue(client, catalog, monkeypatch):
    seen_prompts = []

    def fake_find(prompt, client=None, max_uses=5):
        seen_prompts.append(prompt)
        return [{"name": "A", "url": "https://a.example", "license_verifiable": True}]

    monkeypatch.setattr(jobs.discovery, "find_candidate_sites", fake_find)

    resp = client.post("/api/jobs/discover/auto", json={"batch_size": 2, "catalog": str(catalog)})
    events = _drain(client, resp.json()["job_id"])
    assert events[-1]["type"] == "done"
    assert events[-1]["result"] == {"searched": 2, "failed": 0, "candidates_found": 2}
    assert len(seen_prompts) == 2

    status = client.get("/api/discovery/queue-status", params={"catalog": str(catalog)}).json()
    assert status == {"total": 3, "searched": 2, "remaining": 1}

    candidates = client.get("/api/candidates").json()["candidates"]
    assert len(candidates) == 2


def test_auto_discovery_batch_streams_progress_events(client, catalog, monkeypatch):
    monkeypatch.setattr(
        jobs.discovery,
        "find_candidate_sites",
        lambda prompt, client=None, max_uses=5: [{"name": "A", "url": "https://a.example"}],
    )

    resp = client.post("/api/jobs/discover/auto", json={"batch_size": 3, "catalog": str(catalog)})
    events = _drain(client, resp.json()["job_id"])

    progress_events = [e for e in events if e["type"] == "progress"]
    assert [(e["completed"], e["total"]) for e in progress_events] == [(1, 3), (2, 3), (3, 3)]


def test_auto_discovery_batch_continues_past_one_family_erroring(client, catalog, monkeypatch):
    def flaky_find(prompt, client=None, max_uses=5):
        if "Family 0" in prompt:
            raise RuntimeError("boom")
        return [{"name": "ok", "url": "https://ok.example"}]

    monkeypatch.setattr(jobs.discovery, "find_candidate_sites", flaky_find)

    resp = client.post("/api/jobs/discover/auto", json={"batch_size": 3, "catalog": str(catalog)})
    events = _drain(client, resp.json()["job_id"])

    assert events[-1]["type"] == "done"  # one family failing doesn't abort the batch
    assert events[-1]["result"] == {"searched": 2, "failed": 1, "candidates_found": 2}
    log_messages = " ".join(e["message"] for e in events if e["type"] == "log")
    assert "Family 0: failed" in log_messages

    # The failed family was never marked searched, so it's still in the queue
    # to retry -- not silently recorded as "searched, found nothing".
    status = client.get("/api/discovery/queue-status", params={"catalog": str(catalog)}).json()
    assert status == {"total": 3, "searched": 2, "remaining": 1}


def test_api_keys_write_rejected_without_localhost(client):
    # The shared `client` fixture's TestClient reports a fake "testclient" peer
    # address (not 127.0.0.1), so it exercises exactly the non-localhost path.
    resp = client.post("/api/keys", json={"key": "ANTHROPIC_API_KEY", "value": "sk-test"})
    assert resp.status_code == 403


def test_api_keys_write_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    with TestClient(web_app.app, client=("127.0.0.1", 12345)) as local_client:
        resp = local_client.post("/api/keys", json={"key": "NOT_A_REAL_KEY", "value": "x"})
        assert resp.status_code == 400


def test_api_keys_write_succeeds_from_localhost_and_never_echoes_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    with TestClient(web_app.app, client=("127.0.0.1", 12345)) as local_client:
        resp = local_client.post("/api/keys", json={"key": "ANTHROPIC_API_KEY", "value": "sk-super-secret"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "saved"}
        assert "sk-super-secret" not in resp.text

        env_contents = (tmp_path / ".env").read_text()
        assert "sk-super-secret" in env_contents  # written to .env, just never echoed over HTTP

        doctor_resp = local_client.get("/api/doctor")
        results = {r["name"]: r for r in doctor_resp.json()["results"]}
        assert results["credential:ANTHROPIC_API_KEY"]["status"] == "pass"


def test_wipe_rejected_without_localhost(client):
    resp = client.post("/api/wipe", json={"confirm": web_app.WIPE_CONFIRM_PHRASE})
    assert resp.status_code == 403


def test_wipe_rejects_a_wrong_confirm_phrase_and_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    restarted = []
    monkeypatch.setattr(web_app, "_restart_process", lambda: restarted.append(1))
    monkeypatch.setattr(web_app, "RESTART_DELAY_SECONDS", 0)

    with TestClient(web_app.app, client=("127.0.0.1", 12345)) as local_client:
        conn = get_connection(web_app.DB_PATH)
        apply_migrations(conn)
        conn.execute("INSERT INTO images (family, path, status, created_at) VALUES ('A-1', 'x.jpg', 'active', '2026')")
        conn.commit()
        conn.close()

        resp = local_client.post("/api/wipe", json={"confirm": "not the phrase"})
        assert resp.status_code == 400

        conn = get_connection(web_app.DB_PATH)
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
        conn.close()
    assert restarted == []


def test_wipe_clears_data_cancels_running_jobs_and_restarts_from_localhost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    restarted = threading.Event()
    monkeypatch.setattr(web_app, "_restart_process", restarted.set)
    monkeypatch.setattr(web_app, "RESTART_DELAY_SECONDS", 0)

    with TestClient(web_app.app, client=("127.0.0.1", 12345)) as local_client:
        conn = get_connection(web_app.DB_PATH)
        apply_migrations(conn)
        conn.execute("INSERT INTO images (family, path, status, created_at) VALUES ('A-1', 'x.jpg', 'active', '2026')")
        # Discovery progress, left alone by the wipe -- confirms it survives.
        conn.execute("INSERT INTO discovery_queue (family, searched_at, candidate_count) VALUES ('A-1', NULL, 0)")
        conn.commit()
        conn.close()

        image_dir = tmp_path / "satellite_images" / "A-1"
        image_dir.mkdir(parents=True)
        (image_dir / "A-1_wikimedia_01.jpg").write_bytes(b"fake")

        # A still-running job that respects cancel almost immediately --
        # confirms the endpoint actually waits for it rather than wiping
        # out from under it.
        job_id, entry = web_app._new_job("Scrape", "scrape")
        stopped = threading.Event()

        def fake_job():
            entry["cancel"].wait(timeout=5)
            entry["status"] = "done"
            stopped.set()

        threading.Thread(target=fake_job, daemon=True).start()

        resp = local_client.post("/api/wipe", json={"confirm": web_app.WIPE_CONFIRM_PHRASE})
        assert resp.status_code == 200
        data = resp.json()
        assert data["table_counts"]["images"] == 1
        assert stopped.is_set()  # the job was actually cancelled, not left running

        conn = get_connection(web_app.DB_PATH)
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM discovery_queue").fetchone()[0] == 1
        conn.close()
        assert not (tmp_path / "satellite_images").exists()

    assert restarted.wait(timeout=2)


ANNOTATE_SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
    },
    "Other": {"Mass": ["Dropdown", ["Small", "Large"]]},
}


def _put_schema(client, schema=ANNOTATE_SCHEMA):
    resp = client.put("/api/annotate/schema", json=schema)
    assert resp.status_code == 200
    return resp.json()["version"]


def _mark_reviewed(image_id):
    conn = get_connection(web_app.DB_PATH)
    apply_migrations(conn)
    conn.execute("UPDATE images SET reviewed_at = '2026-11-02T00:00:00+00:00' WHERE id = ?", (image_id,))
    conn.commit()
    conn.close()


def test_annotate_pages_use_the_wide_layout(client):
    assert "panels-wide" in client.get("/annotate").text
    assert "panels-wide" in client.get("/attributes").text
    assert "panels-wide" not in client.get("/review").text


def test_annotation_schema_starts_empty_then_round_trips(client):
    body = client.get("/api/annotate/schema").json()
    assert body["schema"] == {}
    assert "Multi-Select" in body["types"]

    version = _put_schema(client)
    body = client.get("/api/annotate/schema").json()
    assert body["schema"] == ANNOTATE_SCHEMA
    assert body["version"] == version
    assert Path("attributes.json").exists()  # cwd is tmp_path in this fixture


def test_annotation_schema_rejects_invalid_and_keeps_the_old_file(client):
    _put_schema(client)
    resp = client.put("/api/annotate/schema", json={"Body": {"Shape": ["Dropdown", []]}})
    assert resp.status_code == 400
    assert client.get("/api/annotate/schema").json()["schema"] == ANNOTATE_SCHEMA


def test_annotate_queue_submit_and_export_flow(client):
    version = _put_schema(client)
    image_id, _ = _insert_active_image(client, family="Family 0")

    # Not reviewed yet, so only visible with reviewed_only off.
    assert client.get("/api/annotate/queue", params={"annotator": "wesam"}).json()["remaining"] == 0
    _mark_reviewed(image_id)
    queue = client.get("/api/annotate/queue", params={"annotator": "wesam"}).json()
    assert queue["remaining"] == 1
    assert queue["queue"][0]["id"] == image_id

    resp = client.post(
        f"/api/annotate/{image_id}",
        json={
            "annotator": "wesam",
            "values": {"Shape": "Hexagonal", "Antennas": [], "Mass": "Small"},
            "schema_version": version,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["values"]["Antennas"] == ["None"]
    assert client.get("/api/annotate/queue", params={"annotator": "wesam"}).json()["remaining"] == 0
    assert client.get("/api/annotate/stats").json() == {"images": 1, "annotators": 1}

    resp = client.post("/api/annotate/export", json={"out_path": "out/annotations.csv"})
    assert resp.status_code == 200
    assert resp.json()["rows"] == 1
    with open("out/annotations.csv", newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["Shape"] == "Hexagonal"
    assert row["family"] == "Family 0"


def test_annotate_submit_reports_field_errors(client):
    version = _put_schema(client)
    image_id, _ = _insert_active_image(client)
    resp = client.post(
        f"/api/annotate/{image_id}",
        json={"annotator": "wesam", "values": {"Shape": "Rectangular"}, "schema_version": version},
    )
    assert resp.status_code == 422
    assert set(resp.json()["detail"]["errors"]) == {"Mass"}


def test_annotate_submit_unusable(client):
    version = _put_schema(client)
    image_id, _ = _insert_active_image(client)
    resp = client.post(
        f"/api/annotate/{image_id}",
        json={"annotator": "wesam", "values": {}, "unusable": True, "schema_version": version},
    )
    assert resp.status_code == 200
    assert set(resp.json()["values"].values()) == {"UNUSABLE"}


def test_annotate_submit_conflicts_when_schema_changed(client):
    old_version = _put_schema(client)
    _put_schema(client, {**ANNOTATE_SCHEMA, "New": {"Color": ["Text Box"]}})
    image_id, _ = _insert_active_image(client)
    resp = client.post(
        f"/api/annotate/{image_id}",
        json={"annotator": "wesam", "values": {}, "unusable": True, "schema_version": old_version},
    )
    assert resp.status_code == 409


def test_annotate_submit_rejects_reserved_annotator_and_unknown_image(client):
    _put_schema(client)
    image_id, _ = _insert_active_image(client)
    resp = client.post(f"/api/annotate/{image_id}", json={"annotator": "human_review", "unusable": True})
    assert resp.status_code == 400
    resp = client.post("/api/annotate/999999", json={"annotator": "wesam", "unusable": True})
    assert resp.status_code == 400


def test_annotate_import_endpoint_dry_run_then_real(client):
    _put_schema(client)
    image_id, img_path = _insert_active_image(client)
    old_csv = Path("annotatedInfo.csv")
    with open(old_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "Shape", "Antennae", "Mass"])
        writer.writerow([img_path.name, "Cylindrical", "['Dipole']", "Large"])
        writer.writerow(["missing.jpg", "Cylindrical", "['Dipole']", "Large"])

    body = {"csv_path": str(old_csv)}
    dry = client.post("/api/annotate/import", json=body).json()  # dry run is the default
    assert dry["dry_run"] is True
    assert dry["imported"] == 1
    assert dry["unmatched"] == 1
    assert client.get("/api/annotate/stats").json()["images"] == 0

    real = client.post("/api/annotate/import", json={**body, "dry_run": False}).json()
    assert real["imported"] == 1
    assert Path(real["review_path"]).exists()
    assert client.get("/api/annotate/stats").json()["images"] == 1
    _mark_reviewed(image_id)
    assert client.get("/api/annotate/queue", params={"annotator": "wesam"}).json()["remaining"] == 0
    params = {"annotator": "wesam", "skip_annotated_by_others": "false"}
    assert client.get("/api/annotate/queue", params=params).json()["remaining"] == 1


def test_annotate_import_endpoint_reports_bad_path(client):
    _put_schema(client)
    resp = client.post("/api/annotate/import", json={"csv_path": "nope.csv"})
    assert resp.status_code == 400
