"""Offline tests for Phase 1 Task 7: the run report (per-run delta, not a
cumulative total)."""

from datetime import datetime, timedelta, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages.run_report import build_run_report


def _iso(dt) -> str:
    return dt.isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _add_image(conn, source_module, external_id, retrieved_at):
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
        (f"{external_id}.jpg", retrieved_at),
    )
    image_id = cur.lastrowid
    conn.execute(
        """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
           VALUES (?, ?, ?, 'https://example.org', ?)""",
        (image_id, source_module, external_id, retrieved_at),
    )
    conn.commit()


def _add_rejection(conn, reason, rejected_at):
    conn.execute(
        """INSERT INTO rejections (family, source_module, reason, rejected_at)
           VALUES ('F', 'wikimedia', ?, ?)""",
        (reason, rejected_at),
    )
    conn.commit()


def test_report_excludes_rows_from_before_the_run_started(conn):
    before = datetime.now(timezone.utc) - timedelta(hours=1)
    _add_image(conn, "wikimedia", "old-item", _iso(before))
    _add_rejection(conn, "no_license", _iso(before))

    since = datetime.now(timezone.utc)
    _add_image(conn, "wikimedia", "new-item", _iso(since + timedelta(seconds=1)))
    _add_rejection(conn, "no_license", _iso(since + timedelta(seconds=1)))

    report = build_run_report(conn, _iso(since))

    assert report["images_by_source"] == {"wikimedia": 1}
    assert report["rejections_by_reason"] == {"no_license": 1}


def test_report_is_empty_when_nothing_happened_this_run(conn):
    since = datetime.now(timezone.utc)
    report = build_run_report(conn, _iso(since))
    assert report == {"images_by_source": {}, "rejections_by_reason": {}}
