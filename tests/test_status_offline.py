"""Offline tests for Phase 1 Task 7: status."""

from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages.status import compute_status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _add_image(conn, family, source_module, external_id):
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, f"{family}/{external_id}.jpg", _now()),
    )
    image_id = cur.lastrowid
    conn.execute(
        """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
           VALUES (?, ?, ?, 'https://example.org', ?)""",
        (image_id, source_module, external_id, _now()),
    )
    conn.commit()


def _add_no_images_found(conn, family, source_module):
    conn.execute(
        """INSERT INTO rejections (family, source_module, external_id, reason, rejected_at)
           VALUES (?, ?, NULL, 'no_images_found', ?)""",
        (family, source_module, _now()),
    )
    conn.commit()


def test_family_with_both_sources_untried_is_pending(conn):
    rows = compute_status(conn, ["Untouched Family"])
    assert rows[0]["state"] == "pending"
    assert rows[0]["image_count"] == 0
    assert set(rows[0]["untried_sources"]) == {"wikimedia", "nasa"}


def test_family_with_images_on_both_sources_is_done(conn):
    _add_image(conn, "Covered Family", "wikimedia", "ext1")
    _add_image(conn, "Covered Family", "nasa", "ext2")

    rows = compute_status(conn, ["Covered Family"])
    assert rows[0]["state"] == "done"
    assert rows[0]["image_count"] == 2
    assert rows[0]["untried_sources"] == []


def test_family_fully_tried_with_zero_images_is_failed(conn):
    _add_no_images_found(conn, "Empty Family", "wikimedia")
    _add_no_images_found(conn, "Empty Family", "nasa")

    rows = compute_status(conn, ["Empty Family"])
    assert rows[0]["state"] == "failed"
    assert rows[0]["image_count"] == 0


def test_family_partially_tried_is_still_pending(conn):
    _add_image(conn, "Partial Family", "wikimedia", "ext1")
    # nasa never tried

    rows = compute_status(conn, ["Partial Family"])
    assert rows[0]["state"] == "pending"
    assert rows[0]["image_count"] == 1
    assert rows[0]["untried_sources"] == ["nasa"]
