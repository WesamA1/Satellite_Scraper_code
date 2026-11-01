"""Phase 1 Task 3 (revised): licensing is a human call, not a runtime gate.

Originally this rejected any record with an empty/unverified license_short_name.
By project decision, license validity is now judged by a human during Discovery
review -- reading a candidate's generated source code (and, where useful,
visiting the site itself) before promoting it -- not by an automated per-item
check here. evaluate() always accepts; license_short_name/license_url/etc. are
still recorded as-is (empty included) so they stay visible for provenance in
the licenses table and the export manifest, they just no longer block ingestion.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from scraper.sources.base import ImageRecord


@dataclass
class GateResult:
    accepted: bool
    reason: str | None  # None if accepted, else e.g. "no_license"
    detail: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate(record: ImageRecord) -> GateResult:
    """Always accepts -- see module docstring. Kept as its own function (rather
    than inlined at the call site) so download.py doesn't need to change, and
    so a future per-item check has a single place to land again if needed."""
    return GateResult(accepted=True, reason=None)


def persist(conn: sqlite3.Connection, record: ImageRecord, result: GateResult, path: str) -> int | None:
    """Write the gate's decision to the DB.

    Accepted: inserts images + sources + licenses together in one transaction and
    returns the new image id. Rejected: inserts one rejections row and returns
    None -- no images/sources/licenses row is ever created for a rejected record.
    """
    if not result.accepted:
        conn.execute(
            """INSERT INTO rejections (family, source_module, external_id, reason, detail, rejected_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record.family, record.source_module, record.external_id, result.reason, result.detail, _now()),
        )
        conn.commit()
        return None

    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (record.family, path, _now()),
    )
    image_id = cur.lastrowid

    conn.execute(
        """INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)
           VALUES (?, ?, ?, ?, ?)""",
        (image_id, record.source_module, record.external_id, record.source_url, record.retrieved_at),
    )

    conn.execute(
        """INSERT INTO licenses
           (image_id, license_short_name, license_url, author, attribution_required, credit, verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            image_id,
            record.license_short_name,
            record.license_url,
            record.author,
            None if record.attribution_required is None else int(record.attribution_required),
            record.credit,
            _now(),
        ),
    )
    conn.commit()
    return image_id


def count_rejections_by_reason(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT reason, COUNT(*) AS n FROM rejections GROUP BY reason")
    return {row["reason"]: row["n"] for row in rows}


def is_already_fetched(conn: sqlite3.Connection, source_module: str, external_id: str) -> bool:
    """Resume check: has this exact item already been fetched (and gated)?"""
    row = conn.execute(
        "SELECT 1 FROM sources WHERE source_module = ? AND external_id = ?",
        (source_module, external_id),
    ).fetchone()
    return row is not None


def has_negative_cache(conn: sqlite3.Connection, family: str, source_module: str) -> bool:
    """Resume check: did a prior search for this family on this source find nothing?"""
    row = conn.execute(
        """SELECT 1 FROM rejections
           WHERE family = ? AND source_module = ? AND reason = 'no_images_found'""",
        (family, source_module),
    ).fetchone()
    return row is not None
