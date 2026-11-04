"""A record of every scrape run, for the Status page's overview.

run_scrape_job opens a row when a run starts and closes it when the run ends
-- finished, stopped (Ctrl+C or the stop button) or failed -- with a summary
per source of what that run did: images added, searches that came back
empty, items turned away for licensing, downloads that weren't images.
"""

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start(conn: sqlite3.Connection, families: list[str], sources: tuple[str, ...] | list[str], started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, status, families, sources) VALUES (?, 'running', ?, ?)",
        (started_at, json.dumps(list(families)), json.dumps(list(sources))),
    )
    conn.commit()
    return cur.lastrowid


def per_source(conn: sqlite3.Connection, since: str) -> dict[str, dict[str, int]]:
    """What each source did since `since`."""
    summary: dict[str, dict[str, int]] = {}

    def bump(source, key, n):
        summary.setdefault(source, {"images": 0, "empty_searches": 0, "no_license": 0, "not_an_image": 0})[key] += n

    for row in conn.execute(
        """SELECT s.source_module AS source, COUNT(*) AS n, SUM(i.reject_reason = 'not_an_image') AS bad
           FROM sources s JOIN images i ON i.id = s.image_id WHERE s.retrieved_at >= ? GROUP BY s.source_module""",
        (since,),
    ):
        bump(row["source"], "images", row["n"] - (row["bad"] or 0))
        bump(row["source"], "not_an_image", row["bad"] or 0)
    for row in conn.execute(
        "SELECT source_module AS source, reason, COUNT(*) AS n FROM rejections WHERE rejected_at >= ? GROUP BY 1, 2",
        (since,),
    ):
        if row["reason"] == "no_images_found":
            bump(row["source"], "empty_searches", row["n"])
        else:
            bump(row["source"], "no_license", row["n"])
    return summary


def finish(
    conn: sqlite3.Connection, run_id: int, status: str, since: str, families_done: int, error: str | None = None
) -> dict:
    summary = per_source(conn, since)
    conn.execute(
        """UPDATE scrape_runs SET finished_at = ?, status = ?, families_done = ?, images_added = ?, summary = ?,
                                  error = ? WHERE id = ?""",
        (
            _now(),
            status,
            families_done,
            sum(s["images"] for s in summary.values()),
            json.dumps(summary),
            error,
            run_id,
        ),
    )
    conn.commit()
    return summary


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute("SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [
        {
            "id": r["id"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "families": json.loads(r["families"]),
            "sources": json.loads(r["sources"]),
            "families_done": r["families_done"],
            "images_added": r["images_added"],
            "summary": json.loads(r["summary"]) if r["summary"] else {},
            "error": r["error"],
        }
        for r in rows
    ]
