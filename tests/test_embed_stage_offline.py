"""Offline test for Phase 1 Task 1's embed stage stub."""

from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages.embed import run_embed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def test_embed_stub_counts_active_images_and_writes_nothing(conn):
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', 'p1.jpg', 'active', ?)",
        (_now(),),
    )
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', 'p2.jpg', 'rejected_filter', ?)",
        (_now(),),
    )
    conn.commit()

    count = run_embed(conn)

    assert count == 1
    predictions = conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"]
    assert predictions == 0
