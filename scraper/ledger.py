"""The master ledger: one entry for every image ever seen, keyed by its
contents (SHA-256), with the decision about it and every source it came from.

It lives in data/ledger.db, next to the master set (data/images/<family>/)
and the annotations, so it travels with data/ to another machine and
survives wiping scraper.db. Every connection from
scraper.db.connection.get_connection attaches it as the schema "ledger" --
the ledger file is <folder holding the database>/data/ledger.db, so a test
database in a temp folder gets its own ledger there.

Entry states:
- waiting:  downloaded or added, not reviewed yet
- kept:     in the master set; `path` is its one copy, relative to data/
- rejected: never wanted again; `reason` says why (Review's reasons,
            "filter", "duplicate" for a look-alike, "off_limits_source", ...)

What it's for:
- a rejected image never comes back, from any source, even after a wipe;
- an image already in the master set isn't stored a second time -- a new
  source for it is recorded instead (more license provenance);
- import-data can rebuild the working database from it after a wipe.

One machine should own the master set (the one that scrapes and reviews);
other machines get copies of data/.
"""

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LEDGER_FILE = "ledger.db"
SCHEMA_NAME = "ledger"
MAX_LOOKALIKE_DISTANCE = 8  # same threshold as the dedupe stage's look-alikes

STATES = ("waiting", "kept", "rejected")

# A source row that names a real license ("Unverified ..." is a source saying it has none).
HAS_LICENSE = "COALESCE(s.license_short_name, '') != '' AND s.license_short_name NOT LIKE 'Unverified%'"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger.ledger_entries (
  sha256      TEXT PRIMARY KEY,
  phash       TEXT,
  family      TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('waiting', 'kept', 'rejected')),
  reason      TEXT,
  decided_by  TEXT,
  decided_at  TEXT,
  path        TEXT,
  file_name   TEXT,
  first_seen  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger.idx_ledger_state ON ledger_entries(state);
CREATE TABLE IF NOT EXISTS ledger.ledger_sources (
  id                    INTEGER PRIMARY KEY,
  sha256                TEXT NOT NULL,
  source                TEXT NOT NULL,
  external_id           TEXT NOT NULL DEFAULT '',
  source_url            TEXT,
  license_short_name    TEXT,
  license_url           TEXT,
  author                TEXT,
  credit                TEXT,
  attribution_required  INTEGER,
  retrieved_at          TEXT,
  UNIQUE (sha256, source, external_id)
);
CREATE INDEX IF NOT EXISTS ledger.idx_ledger_sources_item ON ledger_sources(source, external_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def attach(conn: sqlite3.Connection, data_dir: Path | None) -> None:
    """Attach the ledger as schema "ledger" (an in-memory one when there's no
    data folder, e.g. for an in-memory database)."""
    if data_dir is None:
        target = ":memory:"
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        target = str(data_dir / LEDGER_FILE)
    conn.execute(f"ATTACH DATABASE ? AS {SCHEMA_NAME}", (target,))
    if target != ":memory:":
        conn.execute(f"PRAGMA {SCHEMA_NAME}.journal_mode = WAL")
    conn.executescript(_SCHEMA)


def data_dir(conn: sqlite3.Connection) -> Path:
    """The data folder this connection's ledger lives in."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == SCHEMA_NAME and row[2]:
            return Path(row[2]).parent
    return Path("data")


def home(conn: sqlite3.Connection) -> Path:
    """The master set's one home: data/images/ (one folder per family)."""
    return data_dir(conn) / "images"


def relative_to_data(conn: sqlite3.Connection, path: Path | str) -> str:
    try:
        return Path(path).resolve().relative_to(data_dir(conn).resolve()).as_posix()
    except ValueError:
        return str(path)


def entry(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ledger.ledger_entries WHERE sha256 = ?", (sha256,)).fetchone()


def _ensure(conn, sha256: str, family: str, phash: str | None = None, file_name: str | None = None) -> sqlite3.Row:
    now = _now()
    conn.execute(
        """INSERT INTO ledger.ledger_entries (sha256, phash, family, state, file_name, first_seen, updated_at)
           VALUES (?, ?, ?, 'waiting', ?, ?, ?)
           ON CONFLICT(sha256) DO UPDATE SET phash = COALESCE(ledger_entries.phash, excluded.phash),
                                             file_name = COALESCE(ledger_entries.file_name, excluded.file_name)""",
        (sha256, phash, family, file_name, now, now),
    )
    return entry(conn, sha256)


def add_source(conn: sqlite3.Connection, sha256: str, source: dict | None) -> None:
    """Record where this image came from (a no-op for an entry that doesn't
    exist yet, or a source already recorded)."""
    if not source or entry(conn, sha256) is None:
        return
    conn.execute(
        """INSERT OR IGNORE INTO ledger.ledger_sources
           (sha256, source, external_id, source_url, license_short_name, license_url, author, credit,
            attribution_required, retrieved_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sha256,
            source.get("source") or "unknown",
            source.get("external_id") or "",
            source.get("source_url"),
            source.get("license_short_name"),
            source.get("license_url"),
            source.get("author"),
            source.get("credit"),
            source.get("attribution_required"),
            source.get("retrieved_at") or _now(),
        ),
    )
    conn.commit()


def check_new(
    conn: sqlite3.Connection,
    sha256: str,
    family: str,
    phash: str | None = None,
    file_name: str | None = None,
    source: dict | None = None,
) -> str:
    """What to do with a newly downloaded or added image, recording it either
    way. Returns:
    - "new":            first time seen; a waiting entry is created
    - "rejected_before": rejected earlier -- set it aside
    - "already_kept":    already in the master set -- don't store it again
    - "waiting":         already waiting for review (another copy is in the queue)
    """
    existing = entry(conn, sha256)
    if existing is None:
        _ensure(conn, sha256, family, phash, file_name)
        add_source(conn, sha256, source)
        conn.commit()
        return "new"
    add_source(conn, sha256, source)
    if existing["state"] == "rejected":
        return "rejected_before"
    if existing["state"] == "kept":
        return "already_kept"
    return "waiting"


def keep(conn: sqlite3.Connection, sha256: str, family: str, path: str | None, by: str, phash: str | None = None):
    _ensure(conn, sha256, family, phash, Path(path).name if path else None)
    conn.execute(
        """UPDATE ledger.ledger_entries SET state = 'kept', family = ?, path = ?, reason = NULL,
                  decided_by = ?, decided_at = ?, updated_at = ? WHERE sha256 = ?""",
        (family, path, by, _now(), _now(), sha256),
    )
    conn.commit()


def reject(conn: sqlite3.Connection, sha256: str, family: str, reason: str, by: str, phash: str | None = None):
    """Remember a rejection. An entry rejected already keeps its first reason
    (a copy set aside as "rejected before" doesn't overwrite why)."""
    current = _ensure(conn, sha256, family, phash)
    if current["state"] == "rejected":
        return
    conn.execute(
        """UPDATE ledger.ledger_entries SET state = 'rejected', reason = ?, path = NULL,
                  decided_by = ?, decided_at = ?, updated_at = ? WHERE sha256 = ?""",
        (reason, by, _now(), _now(), sha256),
    )
    conn.commit()


def set_family(conn: sqlite3.Connection, sha256: str, family: str, path: str | None = None) -> None:
    if entry(conn, sha256) is None:
        return
    conn.execute(
        "UPDATE ledger.ledger_entries SET family = ?, path = COALESCE(?, path), updated_at = ? WHERE sha256 = ?",
        (family, path, _now(), sha256),
    )
    conn.commit()


def source_known(conn: sqlite3.Connection, source: str, external_id: str) -> bool:
    """Has this exact item from this source been seen before (whatever was
    decided about it)? Lets a scrape skip it even after a wipe."""
    return (
        conn.execute(
            "SELECT 1 FROM ledger.ledger_sources WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()
        is not None
    )


def _distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def looks_like_rejected(
    conn: sqlite3.Connection, phash: str | None, sha256: str | None = None, max_distance: int = MAX_LOOKALIKE_DISTANCE
) -> sqlite3.Row | None:
    """The closest rejected image this one looks like (resized, recompressed
    copies), or None. A hint for Review, not a verdict."""
    if not phash:
        return None
    best, best_distance = None, max_distance + 1
    for row in conn.execute(
        "SELECT * FROM ledger.ledger_entries WHERE state = 'rejected' AND phash IS NOT NULL AND sha256 != ?",
        (sha256 or "",),
    ):
        try:
            distance = _distance(phash, row["phash"])
        except ValueError:
            continue
        if distance < best_distance:
            best, best_distance = row, distance
    return best


def source_from_db(conn: sqlite3.Connection, image_id: int) -> dict | None:
    """An image's source + license record from scraper.db, as a ledger source."""
    row = conn.execute(
        """SELECT s.source_module, s.external_id, s.source_url, s.retrieved_at, l.license_short_name,
                  l.license_url, l.author, l.credit, l.attribution_required
           FROM sources s LEFT JOIN licenses l ON l.image_id = s.image_id WHERE s.image_id = ?""",
        (image_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "source": row["source_module"],
        "external_id": row["external_id"],
        "source_url": row["source_url"],
        "retrieved_at": row["retrieved_at"],
        "license_short_name": row["license_short_name"],
        "license_url": row["license_url"],
        "author": row["author"],
        "credit": row["credit"],
        "attribution_required": row["attribution_required"],
    }


def sources_for(conn: sqlite3.Connection, sha256: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ledger.ledger_sources WHERE sha256 = ? ORDER BY id", (sha256,)).fetchall()


def counts(conn: sqlite3.Connection) -> dict:
    by_state = {state: 0 for state in STATES}
    for row in conn.execute("SELECT state, COUNT(*) FROM ledger.ledger_entries GROUP BY state"):
        by_state[row[0]] = row[1]
    unlicensed = conn.execute(
        f"""SELECT COUNT(*) FROM ledger.ledger_entries e WHERE e.state = 'kept' AND NOT EXISTS (
             SELECT 1 FROM ledger.ledger_sources s WHERE s.sha256 = e.sha256 AND {HAS_LICENSE})"""
    ).fetchone()[0]
    reasons = {
        row[0] or "unrecorded": row[1]
        for row in conn.execute(
            "SELECT reason, COUNT(*) FROM ledger.ledger_entries WHERE state = 'rejected' GROUP BY reason"
        )
    }
    return {**by_state, "license_unrecorded": unlicensed, "rejected_reasons": reasons}


EXPORT_FIELDS = [
    "sha256",
    "state",
    "family",
    "reason",
    "path",
    "file_name",
    "decided_by",
    "decided_at",
    "first_seen",
    "sources",
    "licenses",
    "source_urls",
]


def export_csv(conn: sqlite3.Connection, out: Path) -> int:
    """The ledger as a spreadsheet: one row per image, its sources and
    licenses joined with " | "."""
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM ledger.ledger_entries ORDER BY family, state, file_name").fetchall()
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            srcs = sources_for(conn, row["sha256"])
            writer.writerow(
                {
                    **{k: row[k] for k in EXPORT_FIELDS[:9]},
                    "sources": " | ".join(s["source"] for s in srcs),
                    "licenses": " | ".join(s["license_short_name"] or "unrecorded" for s in srcs),
                    "source_urls": " | ".join(s["source_url"] or "" for s in srcs),
                }
            )
    return len(rows)
