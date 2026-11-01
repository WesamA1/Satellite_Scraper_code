"""Phase 1 Task 1: the embed stage -- explicit stub.

Phase 3 (frozen DINOv3 backbone, multi-label attribute heads) is out of scope
for Phase 1. This just confirms the stage is wired in and reports how many
images are ready, writing nothing to `predictions`.
"""

import sqlite3


def run_embed(conn: sqlite3.Connection) -> int:
    count = conn.execute("SELECT COUNT(*) AS n FROM images WHERE status = 'active'").fetchone()["n"]
    print(f"[embed] stub: {count} active image(s) ready for embedding (Phase 3 implements the model).")
    return count
