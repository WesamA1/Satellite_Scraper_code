"""Offline tests for the Phase 1 Task 2 SQLite schema."""

import sqlite3
from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations

EXPECTED_TABLES = {
    "schema_version",
    "images",
    "sources",
    "licenses",
    "predictions",
    "annotations",
    "rate_limits",
    "rejections",
    "candidate_sources",
    "embeddings",
    "discovery_queue",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _insert_gated_image(conn, family="GPS Block IIF", external_id="File:example.jpg"):
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, f"satellite_images/{family}/example.jpg", _now()),
    )
    image_id = cur.lastrowid
    conn.execute(
        """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
           VALUES (?, 'wikimedia', ?, 'https://commons.wikimedia.org/wiki/File:example.jpg', ?)""",
        (image_id, external_id, _now()),
    )
    conn.execute(
        """INSERT INTO licenses (image_id, license_short_name, verified_at)
           VALUES (?, 'CC BY-SA 4.0', ?)""",
        (image_id, _now()),
    )
    conn.commit()
    return image_id


def test_migrations_apply_cleanly(conn):
    version = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert version == 5


def test_all_tables_exist(conn):
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= tables


def test_insert_image_source_license_succeeds(conn):
    image_id = _insert_gated_image(conn)
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    assert row["status"] == "active"


def test_duplicate_external_id_is_rejected(conn):
    """UNIQUE(source_module, external_id) enforces resume-safety: never re-fetch the same item."""
    _insert_gated_image(conn, external_id="File:dup.jpg")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_gated_image(conn, family="Different Family", external_id="File:dup.jpg")


def test_no_license_rejection_leaves_no_image_record(conn):
    """CLAUDE.md: 'no license metadata, no record' -- a no_license rejection creates no
    images/sources/licenses rows, only a rejections entry."""
    conn.execute(
        """INSERT INTO rejections (family, source_module, external_id, reason, rejected_at)
           VALUES ('GPS Block IIF', 'wikimedia', 'File:nolicense.jpg', 'no_license', ?)""",
        (_now(),),
    )
    conn.commit()

    orphans = conn.execute(
        """SELECT images.* FROM images
           LEFT JOIN licenses ON licenses.image_id = images.id
           WHERE licenses.id IS NULL"""
    ).fetchall()
    assert orphans == []

    rejection = conn.execute("SELECT * FROM rejections WHERE reason = 'no_license'").fetchone()
    assert rejection["external_id"] == "File:nolicense.jpg"


def test_empty_search_negative_cache(conn):
    conn.execute(
        """INSERT INTO rejections (family, source_module, reason, rejected_at)
           VALUES ('Obscure Family', 'wikimedia', 'no_images_found', ?)""",
        (_now(),),
    )
    conn.commit()

    cached = conn.execute(
        """SELECT * FROM rejections
           WHERE family = 'Obscure Family' AND source_module = 'wikimedia'
             AND reason = 'no_images_found'"""
    ).fetchone()
    assert cached is not None
    assert cached["external_id"] is None
