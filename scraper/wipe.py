"""Wipes every bit of per-run scrape/filter/review/embed data -- images and
their source/license/prediction/annotation rows, the rate-limit cooldown
table, the rejection log -- plus the actual image files and quarantine
directory on disk. For starting a scrape genuinely from zero.

Leaves candidate_sources and discovery_queue alone on purpose: that's
Discovery's own progress (which sources exist, which families have already
been searched for new ones), not scrape-run data. Wiping it would undo work
that has nothing to do with "start the scrape over," and isn't what a user
asking to clear images and the database for a clean scrape is asking for.
"""

import shutil
import sqlite3
from pathlib import Path

# Children before parents, though SQLite doesn't enforce the FK by default --
# this order is just the honest dependency direction (predictions/annotations/
# licenses/sources all reference images.id).
TABLES = ("predictions", "annotations", "licenses", "sources", "rejections", "rate_limits", "images")


def wipe_images_and_data(conn: sqlite3.Connection, image_dir: Path, quarantine_dir: Path) -> dict:
    table_counts = {}
    for table in TABLES:
        table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    removed_dirs = []
    for d in (image_dir, quarantine_dir):
        if d.exists():
            shutil.rmtree(d)
            removed_dirs.append(str(d))

    return {"table_counts": table_counts, "removed_dirs": removed_dirs}
