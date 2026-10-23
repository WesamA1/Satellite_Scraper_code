"""Applies numbered SQL migrations from migrations/ and tracks schema_version."""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from scraper.db.connection import get_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not exists:
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.commit()
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def _migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = re.match(r"(\d+)_", path.name)
        if match:
            files.append((int(match.group(1)), path))
    return sorted(files, key=lambda item: item[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply any migration newer than the current schema_version. Returns the final version."""
    current = _current_version(conn)
    for version, path in _migration_files():
        if version <= current:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )
        conn.commit()
        current = version
    return current


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke_test.db"
        conn = get_connection(db_path)
        version = apply_migrations(conn)
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        conn.close()
        print(f"Applied schema version {version}")
        print(f"Tables: {', '.join(tables)}")
