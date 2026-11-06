"""SQLite connection helper."""

import sqlite3
from pathlib import Path


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open db_path with the project's standard pragmas and row factory."""
    # Scrape lanes write from several threads at once; wait for a busy
    # database rather than failing straight away.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
