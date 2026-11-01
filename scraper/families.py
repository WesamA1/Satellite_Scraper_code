"""Loads the satellite family catalog.

Phase 1 doesn't port satellite_scraper.py/variants.py into scraper/ (that's the
catalog-fetch step, untouched this phase) -- this just reads the CSV they already
produce, same source reference/coverage_report.py uses.
"""

import csv
from pathlib import Path


def load_families(csv_path: Path = Path("satellites.csv")) -> list[str]:
    families = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)
    return sorted(families)
