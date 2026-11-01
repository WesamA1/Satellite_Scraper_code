"""Offline tests for Phase 1 Task 4: persistent rate limits.

No real sleeping -- a fake sleep_fn just records what it was asked to wait for.
"""

import pytest

from scraper import rate_limiter
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = get_connection(path)
    apply_migrations(conn)
    conn.close()
    return path


def test_host_of_extracts_netloc():
    assert rate_limiter.host_of("https://commons.wikimedia.org/w/api.php?x=1") == "commons.wikimedia.org"


def test_no_cooldown_means_no_wait(db_path):
    conn = get_connection(db_path)
    sleeps = []

    waited = rate_limiter.wait_for_cooldown(conn, "example.org", sleep_fn=sleeps.append)

    assert waited == 0.0
    assert sleeps == []


def test_record_then_wait_sleeps_the_remaining_time(db_path):
    conn = get_connection(db_path)
    sleeps = []

    rate_limiter.record_cooldown(conn, "example.org", retry_after_seconds=90)
    waited = rate_limiter.wait_for_cooldown(conn, "example.org", sleep_fn=sleeps.append)

    assert len(sleeps) == 1
    assert 89 <= sleeps[0] <= 90  # small clock skew between record and wait
    assert 89 <= waited <= 90


def test_cooldown_survives_a_simulated_restart(db_path):
    """The acceptance criterion: stopping and restarting during a throttle window
    does not send requests to that host until the cooldown ends. Simulated here
    by opening a brand new connection to the same DB file."""
    writer_conn = get_connection(db_path)
    rate_limiter.record_cooldown(writer_conn, "commons.wikimedia.org", retry_after_seconds=120)
    writer_conn.close()

    # A fresh process would open a new connection -- this is that restart.
    reader_conn = get_connection(db_path)
    sleeps = []
    waited = rate_limiter.wait_for_cooldown(reader_conn, "commons.wikimedia.org", sleep_fn=sleeps.append)

    assert len(sleeps) == 1
    assert 119 <= waited <= 120


def test_unrelated_host_is_unaffected(db_path):
    conn = get_connection(db_path)
    rate_limiter.record_cooldown(conn, "commons.wikimedia.org", retry_after_seconds=120)

    sleeps = []
    waited = rate_limiter.wait_for_cooldown(conn, "images-api.nasa.gov", sleep_fn=sleeps.append)

    assert waited == 0.0
    assert sleeps == []


def test_record_cooldown_upserts_the_same_host(db_path):
    conn = get_connection(db_path)
    rate_limiter.record_cooldown(conn, "commons.wikimedia.org", retry_after_seconds=30)
    rate_limiter.record_cooldown(conn, "commons.wikimedia.org", retry_after_seconds=200)

    row_count = conn.execute("SELECT COUNT(*) AS n FROM rate_limits WHERE host = 'commons.wikimedia.org'").fetchone()[
        "n"
    ]
    assert row_count == 1

    last_retry_after = conn.execute(
        "SELECT last_retry_after FROM rate_limits WHERE host = 'commons.wikimedia.org'"
    ).fetchone()["last_retry_after"]
    assert last_retry_after == 200
