"""Phase 1 Task 5: retry-failed.

A failed download already leaves no DB row (Task 1's design), so it's retried
automatically on the next `scrape` -- there's nothing to do for that case. What
actually gets stuck forever is the negative cache: a family once marked
no_images_found is never searched again by design (that's the resume-safety
guarantee). This clears that cache for the given scope so those families get
re-searched on the next scrape.
"""

import sqlite3


def clear_negative_cache(conn: sqlite3.Connection, family: str | None = None, source_module: str | None = None) -> int:
    query = "DELETE FROM rejections WHERE reason = 'no_images_found'"
    params = []
    if family is not None:
        query += " AND family = ?"
        params.append(family)
    if source_module is not None:
        query += " AND source_module = ?"
        params.append(source_module)

    cur = conn.execute(query, tuple(params))
    conn.commit()
    return cur.rowcount
