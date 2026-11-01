"""Phase 1 Task 4: persistent, per-host rate limit state.

Replaces the in-memory time.sleep() backoff in scraper/sources/*.py with a
DB-backed cooldown so a stop/restart during a throttle window still respects it.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


def host_of(url: str) -> str:
    return urlparse(url).netloc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_cooldown_until(conn: sqlite3.Connection, host: str) -> datetime | None:
    row = conn.execute("SELECT cooldown_until FROM rate_limits WHERE host = ?", (host,)).fetchone()
    if row and row["cooldown_until"]:
        return datetime.fromisoformat(row["cooldown_until"])
    return None


def wait_for_cooldown(conn: sqlite3.Connection, host: str, sleep_fn=time.sleep) -> float:
    """Sleep out any active cooldown for host. Returns seconds waited (0 if none)."""
    cooldown = get_cooldown_until(conn, host)
    if cooldown is None:
        return 0.0
    remaining = (cooldown - _now()).total_seconds()
    if remaining > 0:
        sleep_fn(remaining)
        return remaining
    return 0.0


def record_cooldown(conn: sqlite3.Connection, host: str, retry_after_seconds: float) -> None:
    now = _now()
    cooldown_until = now + timedelta(seconds=retry_after_seconds)
    conn.execute(
        """INSERT INTO rate_limits (host, cooldown_until, last_retry_after, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(host) DO UPDATE SET
             cooldown_until = excluded.cooldown_until,
             last_retry_after = excluded.last_retry_after,
             updated_at = excluded.updated_at""",
        (host, cooldown_until.isoformat(), retry_after_seconds, now.isoformat()),
    )
    conn.commit()
