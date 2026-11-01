"""Offline tests for scraper/wipe.py: the GUI's "wipe images and database
for a clean scrape" action. No real HTTP, no real process restart -- that
part is covered separately where the web endpoint itself is tested.
"""

from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.wipe import wipe_images_and_data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _seed_one_image(conn, family="A-1"):
    now = _now()
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, f"satellite_images/{family}/{family}_wikimedia_01.jpg", now),
    )
    image_id = cur.lastrowid
    conn.execute(
        "INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at) "
        "VALUES (?, 'wikimedia', 'File:x.jpg', 'https://example.org', ?)",
        (image_id, now),
    )
    conn.execute(
        "INSERT INTO licenses (image_id, license_short_name, verified_at) VALUES (?, 'CC0', ?)",
        (image_id, now),
    )
    conn.execute(
        "INSERT INTO predictions (image_id, attribute, value, model_version, created_at) "
        "VALUES (?, 'shape', 'box', 'v1', ?)",
        (image_id, now),
    )
    conn.execute(
        "INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at) "
        "VALUES (?, 'shape', 'box', 'alice', 'g1', ?)",
        (image_id, now),
    )
    conn.execute(
        "INSERT INTO rejections (family, source_module, reason, rejected_at) VALUES (?, 'dpla', 'no_images_found', ?)",
        (family, now),
    )
    conn.execute("INSERT INTO rate_limits (host, cooldown_until, updated_at) VALUES ('example.org', NULL, ?)", (now,))
    conn.commit()


def _seed_discovery_state(conn):
    now = _now()
    conn.execute(
        "INSERT INTO candidate_sources (prompt, name, url, status, discovered_at) "
        "VALUES ('satellite archives', 'Example Archive', 'https://example.org', 'pending', ?)",
        (now,),
    )
    conn.execute("INSERT INTO discovery_queue (family, searched_at, candidate_count) VALUES ('A-1', ?, 0)", (now,))
    conn.commit()


def test_clears_every_image_derived_table(conn, tmp_path):
    _seed_one_image(conn)
    wipe_images_and_data(conn, tmp_path / "satellite_images", tmp_path / "_quarantine")

    for table in ("images", "sources", "licenses", "predictions", "annotations", "rejections", "rate_limits"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_leaves_discovery_progress_untouched(conn, tmp_path):
    _seed_one_image(conn)
    _seed_discovery_state(conn)
    wipe_images_and_data(conn, tmp_path / "satellite_images", tmp_path / "_quarantine")

    assert conn.execute("SELECT COUNT(*) FROM candidate_sources").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM discovery_queue").fetchone()[0] == 1


def test_removes_the_image_and_quarantine_directories(conn, tmp_path):
    image_dir = tmp_path / "satellite_images"
    quarantine_dir = tmp_path / "_quarantine"
    (image_dir / "A-1").mkdir(parents=True)
    (image_dir / "A-1" / "A-1_wikimedia_01.jpg").write_bytes(b"fake jpeg bytes")
    (quarantine_dir / "A-1").mkdir(parents=True)
    (quarantine_dir / "A-1" / "A-1_dpla_04.jsp").write_bytes(b"<html>not an image</html>")

    wipe_images_and_data(conn, image_dir, quarantine_dir)

    assert not image_dir.exists()
    assert not quarantine_dir.exists()


def test_tolerates_directories_that_dont_exist_yet(conn, tmp_path):
    result = wipe_images_and_data(conn, tmp_path / "never_created", tmp_path / "also_never_created")
    assert result["removed_dirs"] == []


def test_returns_how_many_rows_each_table_had_before_clearing(conn, tmp_path):
    _seed_one_image(conn)
    result = wipe_images_and_data(conn, tmp_path / "satellite_images", tmp_path / "_quarantine")
    assert result["table_counts"]["images"] == 1
    assert result["table_counts"]["sources"] == 1
