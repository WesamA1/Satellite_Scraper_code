"""Phase 1 Task 7: the run report.

A per-run delta (images added per source, rejections by reason), not an all-time
total -- callers capture `since` (now()) right before a run starts and pass it in
after, so the report reflects only what just happened.
"""

import sqlite3


def build_run_report(conn: sqlite3.Connection, since: str) -> dict:
    images_by_source = {
        row["source_module"]: row["n"]
        for row in conn.execute(
            """SELECT source_module, COUNT(*) AS n FROM sources
               WHERE retrieved_at >= ? GROUP BY source_module""",
            (since,),
        )
    }
    rejections_by_reason = {
        row["reason"]: row["n"]
        for row in conn.execute(
            """SELECT reason, COUNT(*) AS n FROM rejections
               WHERE rejected_at >= ? GROUP BY reason""",
            (since,),
        )
    }
    return {"images_by_source": images_by_source, "rejections_by_reason": rejections_by_reason}
