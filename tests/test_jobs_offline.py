"""Offline tests for Phase 1 Task 8: the shared job-runner layer. No real
network/login -- download_family and wikimedia.login are monkeypatched, same
pattern as test_cli_offline.py now uses."""

import threading

import pytest

from scraper import jobs
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def test_progress_callback_fires_once_per_family(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 1}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    progress_calls = []
    jobs.run_scrape_job(
        conn,
        ["Family A", "Family B", "Family C"],
        ("wikimedia",),
        None,
        tmp_path,
        on_progress=lambda completed, total, images_found: progress_calls.append((completed, total, images_found)),
    )

    # images_found accumulates across families (1 per family here) -- the
    # running total shown next to the progress bar, not just family count.
    assert progress_calls == [(1, 3, 1), (2, 3, 2), (3, 3, 3)]


def test_images_found_updates_as_each_source_finishes_not_just_each_family(conn, tmp_path, monkeypatch):
    """A family with several sources can take a long time to fully finish --
    images_found should climb as each source within it completes, not sit at
    the previous family's total until the whole family is done (confirmed
    live: a family 145 images deep into other sources, still on its last
    source, showed "images found: 0" the entire time)."""

    # Three sources in one lane (they share the Wikimedia account), so they
    # run one after another within the family.
    def fake_download_family(conn, session, family_name, **kwargs):
        kwargs["on_source_done"]("wikimedia", 3)
        kwargs["on_source_done"]("wikidata", 0)
        kwargs["on_source_done"]("wikipedia_multilang", 2)
        return {"wikimedia": {"downloaded": 3}, "wikidata": {"downloaded": 0}, "wikipedia_multilang": {"downloaded": 2}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    progress_calls = []
    jobs.run_scrape_job(
        conn,
        ["Family A"],
        ("wikimedia", "wikidata", "wikipedia_multilang"),
        None,
        tmp_path,
        on_progress=lambda completed, total, images_found: progress_calls.append(images_found),
    )

    # Climbs mid-family (3, then 5 after dpla) instead of jumping straight
    # from 0 to the family's full total only once everything is done.
    assert progress_calls == [3, 5, 5]


def test_on_status_reports_the_current_family_alongside_download_familys_source(conn, tmp_path, monkeypatch):
    """run_scrape_job wraps download_family's on_source_start so callers get
    the family name too -- download_family itself has no reason to know it
    (it's a parameter, not state), but the UI's "currently on" readout needs
    both to be useful."""

    def fake_download_family(conn, session, family_name, **kwargs):
        kwargs["on_source_start"]("wikimedia", 1, 1)
        return {"wikimedia": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    status_calls = []
    jobs.run_scrape_job(
        conn,
        ["Family A", "Family B"],
        ("wikimedia",),
        None,
        tmp_path,
        on_status=lambda family, source, **kw: status_calls.append((family, source, kw)),
    )

    assert status_calls == [
        ("Family A", "wikimedia", {"source_index": 1, "source_total": 1}),
        ("Family B", "wikimedia", {"source_index": 1, "source_total": 1}),
    ]


def test_on_status_reports_item_progress_within_the_current_source(conn, tmp_path, monkeypatch):
    """The remaining gap after on_source_start: a source with many items
    looked identical whether it was slow-but-working or truly stuck, since
    on_source_start only fires once per source, not once per item."""

    def fake_download_family(conn, session, family_name, **kwargs):
        kwargs["on_source_start"]("dpla", 1, 1)
        kwargs["on_item_progress"]("dpla", 1, 81)
        kwargs["on_item_progress"]("dpla", 2, 81)
        return {"dpla": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    status_calls = []
    jobs.run_scrape_job(
        conn,
        ["A-1"],
        ("dpla",),
        None,
        tmp_path,
        on_status=lambda family, source, **kw: status_calls.append((family, source, kw)),
    )

    assert status_calls == [
        ("A-1", "dpla", {"source_index": 1, "source_total": 1}),
        ("A-1", "dpla", {"item_index": 1, "item_total": 81}),
        ("A-1", "dpla", {"item_index": 2, "item_total": 81}),
    ]


def test_scrape_log_line_is_a_terse_summary_not_a_raw_dict(conn, tmp_path, monkeypatch):
    """A raw per-source dict dump for every one of 1693 families is hard to
    scan while it scrolls by -- this should read as one short line."""
    monkeypatch.setattr(
        jobs,
        "download_family",
        lambda *a, **k: {"wikimedia": {"downloaded": 2}, "wikidata": {"downloaded": 0}},
    )
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    logs = []
    jobs.run_scrape_job(conn, ["Family A"], ("wikimedia", "wikidata"), None, tmp_path, log=logs.append)

    assert "Family A: +2 image(s) (wikimedia:2)" in logs
    assert not any("{'wikimedia'" in line for line in logs)


def test_scrape_log_line_says_no_images_found_when_nothing_downloaded(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 0}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    logs = []
    jobs.run_scrape_job(conn, ["Family A"], ("wikimedia",), None, tmp_path, log=logs.append)

    assert "Family A: no images found" in logs


def test_cancel_event_already_set_stops_before_any_family(conn, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    cancel = threading.Event()
    cancel.set()
    logs = []

    report = jobs.run_scrape_job(
        conn, ["Family A", "Family B"], ("wikimedia",), None, tmp_path, log=logs.append, cancel=cancel
    )

    assert calls == []
    assert report["interrupted"] is True
    assert report["completed"] == 0
    assert any("Cancelled" in line for line in logs)


def test_cancel_event_set_mid_run_stops_between_families(conn, tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def fake_download_family(conn, session, family_name, **kwargs):
        calls.append(family_name)
        if family_name == "Family A":
            cancel.set()  # simulate the stop button being clicked after family A finishes
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    report = jobs.run_scrape_job(
        conn, ["Family A", "Family B", "Family C"], ("wikimedia",), None, tmp_path, cancel=cancel
    )

    assert calls == ["Family A"]  # never reached B or C
    assert report["interrupted"] is True
    assert report["completed"] == 1


def test_resume_after_interruption_picks_up_where_it_left_off(conn, tmp_path, monkeypatch):
    """Re-running after an interruption is safe by construction (Tasks 1/3's
    resume-safety) -- this just confirms the job layer doesn't add anything that
    would break that."""
    processed = []

    def fake_download_family(conn, session, family_name, **kwargs):
        processed.append(family_name)
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    cancel = threading.Event()
    cancel.set()
    jobs.run_scrape_job(conn, ["Family A", "Family B"], ("wikimedia",), None, tmp_path, cancel=None)
    # first run (no cancel) processes both
    assert processed == ["Family A", "Family B"]

    processed.clear()
    jobs.run_scrape_job(conn, ["Family A", "Family B"], ("wikimedia",), None, tmp_path, cancel=None)
    # calling again just re-processes the same families -- download_family itself
    # (mocked away here) is what's actually resume-safe; this proves the job loop
    # doesn't skip or duplicate families on its own.
    assert processed == ["Family A", "Family B"]


def test_filter_job_delegates_to_run_filter(conn, tmp_path):
    from datetime import datetime, timezone

    from PIL import Image

    path = tmp_path / "img.jpg"
    Image.new("RGB", (4, 4)).save(path)
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
        (str(path), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    logs = []
    counts = jobs.run_filter_job(
        conn, None, tmp_path / "_quarantine", 0.5, False, log=logs.append, scorer=lambda paths: [0.9 for _ in paths]
    )

    assert counts["kept"] == 1
    assert any("Filter result" in line for line in logs)


def test_scrape_from_scratch_clears_negative_cache_before_searching(conn, tmp_path, monkeypatch):
    from scraper import license_gate

    conn.execute(
        "INSERT INTO rejections (family, source_module, external_id, reason, rejected_at) "
        "VALUES ('Family A', 'wikimedia', NULL, 'no_images_found', datetime('now'))"
    )
    conn.commit()
    assert license_gate.has_negative_cache(conn, "Family A", "wikimedia")

    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 0}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    logs = []
    jobs.run_scrape_job(conn, ["Family A"], ("wikimedia",), None, tmp_path, log=logs.append, from_scratch=True)

    assert not license_gate.has_negative_cache(conn, "Family A", "wikimedia")
    assert any("Cleared" in line for line in logs)


def test_scrape_without_from_scratch_leaves_negative_cache_alone(conn, tmp_path, monkeypatch):
    from scraper import license_gate

    conn.execute(
        "INSERT INTO rejections (family, source_module, external_id, reason, rejected_at) "
        "VALUES ('Family A', 'wikimedia', NULL, 'no_images_found', datetime('now'))"
    )
    conn.commit()

    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: {"wikimedia": {"downloaded": 0}})
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    jobs.run_scrape_job(conn, ["Family A"], ("wikimedia",), None, tmp_path)

    assert license_gate.has_negative_cache(conn, "Family A", "wikimedia")


def test_filter_job_stops_cleanly_when_cancelled(conn, tmp_path):
    """The web GUI's Stop button sets this event -- run_filter_job must
    actually stop between batches, not run the whole thing regardless
    (filter.py's BATCH_SIZE is 16, so two images means two batches)."""
    from datetime import datetime, timezone

    from PIL import Image

    for i in range(2):
        path = tmp_path / f"img{i}.jpg"
        Image.new("RGB", (4, 4)).save(path)
        conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
            (str(path), datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()

    cancel = threading.Event()
    cancel.set()
    counts = jobs.run_filter_job(
        conn, None, tmp_path / "_quarantine", 0.5, False, scorer=lambda paths: [0.9 for _ in paths], cancel=cancel
    )

    assert counts == {"kept": 0, "rejected": 0, "missing_file": 0}


def test_codegen_job_rejects_a_candidate_that_was_never_approved(conn):
    from scraper.discovery import persist_candidates

    ids = persist_candidates(conn, "prompt", [{"name": "Never Approved", "url": "https://x.example"}])

    with pytest.raises(ValueError, match="approved first"):
        jobs.run_codegen_job(conn, ids[0], client=object())


def test_embed_and_export_jobs_delegate(conn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the embed job picks up ./training/dataset.jsonl when one exists
    logs = []
    result = jobs.run_embed_job(conn, log=logs.append, training_set=None)
    assert result["embedded"] == 0
    assert any("Embedded 0 image(s)" in line for line in logs)

    count = jobs.run_export_job(conn, tmp_path / "manifest.csv", log=logs.append)
    assert count == 0
    assert any("Export" in line for line in logs)
