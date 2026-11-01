"""Phase 1 Task 1: the export stage.

DB-driven manifest only (no file copying/flattening -- that's closer to Phase 4
release-packaging). Mirrors reference/build_labels.py's role but reads the DB
instead of flat metadata_*.txt files and satellites.csv.
"""

import csv
import sqlite3
from pathlib import Path

FIELDNAMES = [
    "path",
    "family",
    "source_module",
    "external_id",
    "source_url",
    "retrieved_at",
    "license_short_name",
    "license_url",
    "author",
    "attribution_required",
    "credit",
    "attribution_string",
]


def run_export(conn: sqlite3.Connection, out_path: Path = Path("export/manifest.csv")) -> int:
    rows = conn.execute(
        """SELECT images.path, images.family,
                  sources.source_module, sources.external_id, sources.source_url, sources.retrieved_at,
                  licenses.license_short_name, licenses.license_url, licenses.author,
                  licenses.attribution_required, licenses.credit, licenses.attribution_string
           FROM images
           JOIN sources ON sources.image_id = images.id
           JOIN licenses ON licenses.image_id = images.id
           WHERE images.status = 'active'
           ORDER BY images.family, images.id"""
    ).fetchall()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in FIELDNAMES})

    return len(rows)
