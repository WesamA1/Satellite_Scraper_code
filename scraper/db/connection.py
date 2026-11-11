"""SQLite connection helper."""

import sqlite3
from pathlib import Path


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open db_path with the project's standard pragmas and row factory, with
    the master ledger (data/ledger.db next to the database) attached as
    schema "ledger" -- see scraper/ledger.py."""
    from scraper import ledger

    # Scrape lanes write from several threads at once; wait for a busy
    # database rather than failing straight away.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    in_memory = str(db_path) in ("", ":memory:")
    ledger.attach(conn, None if in_memory else Path(db_path).resolve().parent / "data")
    return conn
