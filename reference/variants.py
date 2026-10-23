# variants.py
# Splits large name-based families into hardware variants (generations/blocks)
# and adds a VARIANT_NAME column to satellites.csv.
#
#   python variants.py            # rewrite satellites.csv with VARIANT_NAME
#   python variants.py --report   # just print the variant breakdown
#
# A "family" groups satellites by program name (see satellite_scraper.py);
# a "variant" subdivides a family where successive generations are physically
# different spacecraft (Starlink v1.0 vs V2 Mini, GPS Block IIR vs III, ...).
# Families without split rules keep VARIANT_NAME == FAMILY_NAME.
#
# Rules use whatever signal the catalog actually provides:
#   - name digits/letters where Space-Track encodes the series
#     (FENGYUN 2H -> series 2; GOES 16 -> GOES-R generation)
#   - launch date everywhere else (constellation versions map cleanly to
#     launch campaigns)
#
# Known imprecision (acceptable for a first pass, all noted inline):
#   - STARLINK: v1.5 and V2 Mini launches overlapped Feb-Jun 2023; the date
#     cutoff mislabels some late v1.5s as V2 MINI.
#   - BEIDOU: BD-3 demo sats (2015-16) are labeled BD-2 by the date rule.
#   - GLONASS: a few K1 prototypes launched during the M era are labeled M.

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from satellite_scraper import get_family_name

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
SATELLITES_JSON = ROOT / "satellites.json"
SATELLITES_CSV  = ROOT / "satellites.csv"


def _d(s):
    """Parse a YYYY-MM-DD launch date; None if missing/malformed."""
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ── Date-banded rules: family -> [(first_launch_date, variant), ...] ─────────
# Bands are checked newest-first; a satellite gets the newest band whose
# start date is <= its launch date.

DATE_BANDS = {
    "STARLINK": [
        (date(2019, 5, 1),  "STARLINK V0.9"),
        (date(2019, 11, 1), "STARLINK V1.0"),
        (date(2021, 6, 1),  "STARLINK V1.5"),
        (date(2023, 2, 25), "STARLINK V2 MINI"),   # v1.5 overlap until ~Jun 2023
    ],
    "NAVSTAR": [
        (date(1978, 1, 1),  "GPS BLOCK I"),
        (date(1989, 1, 1),  "GPS BLOCK II"),
        (date(1990, 11, 1), "GPS BLOCK IIA"),
        (date(1997, 1, 1),  "GPS BLOCK IIR"),
        (date(2005, 9, 1),  "GPS BLOCK IIR-M"),
        (date(2010, 5, 1),  "GPS BLOCK IIF"),
        (date(2018, 12, 1), "GPS BLOCK III"),
    ],
    "IRIDIUM": [
        (date(1997, 1, 1),  "IRIDIUM BLOCK 1"),
        (date(2017, 1, 1),  "IRIDIUM NEXT"),
    ],
    "GLONASS": [
        (date(1982, 1, 1),  "GLONASS"),
        (date(2004, 1, 1),  "GLONASS-M"),          # a few K1 prototypes fall here
        (date(2019, 1, 1),  "GLONASS-K"),
    ],
    "GALILEO": [
        (date(2011, 1, 1),  "GALILEO IOV"),
        (date(2014, 1, 1),  "GALILEO FOC"),
    ],
    "BEIDOU": [
        (date(2000, 1, 1),  "BEIDOU-1"),
        (date(2007, 1, 1),  "BEIDOU-2"),
        (date(2017, 11, 1), "BEIDOU-3"),           # BD-3 demos (2015-16) land in BD-2
    ],
    "KUIPER": [
        (date(2023, 1, 1),  "KUIPER PROTOTYPE"),
        (date(2025, 1, 1),  "KUIPER BLOCK 1"),
    ],
    "ONEWEB": [
        (date(2019, 1, 1),  "ONEWEB GEN1"),
    ],
}

# ── Name-digit rules: generation is encoded in the satellite name ────────────

def _fengyun_variant(name):
    # FENGYUN 1A..1D / 2A..2H / 3A.. / 4A..: series digit = generation
    import re
    m = re.search(r"FENGYUN\s+(\d)", name)
    return f"FENGYUN-{m.group(1)}" if m else None


def _goes_variant(name):
    # GOES 1-7 spin-stabilized, 8-12 (I-M), 13-15 (N-P), 16+ (GOES-R series)
    import re
    m = re.search(r"GOES\s+(\d+)", name)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 7:
        return "GOES FIRST GEN"
    if n <= 12:
        return "GOES I-M"
    if n <= 15:
        return "GOES N-P"
    return "GOES-R SERIES"


def _meteosat_variant(name):
    # METEOSAT 1-7 (MFG), 8-11 (MSG), 12+ (MTG)
    import re
    m = re.search(r"METEOSAT[-\s]+(\d+)", name)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 7:
        return "METEOSAT MFG"
    if n <= 11:
        return "METEOSAT MSG"
    return "METEOSAT MTG"


NAME_RULES = {
    "FENGYUN":  _fengyun_variant,
    "GOES":     _goes_variant,
    "METEOSAT": _meteosat_variant,
}


def get_variant(family, object_name, launch_date):
    """Variant label for one satellite; falls back to the family name."""
    name = (object_name or "").upper()

    rule = NAME_RULES.get(family)
    if rule:
        v = rule(name)
        if v:
            return v

    bands = DATE_BANDS.get(family)
    if bands:
        d = _d(launch_date)
        if d:
            for start, variant in reversed(bands):
                if d >= start:
                    return variant

    return family


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Add variant labels to satellites.csv.")
    parser.add_argument("--report", action="store_true",
                        help="print the breakdown without rewriting the CSV")
    args = parser.parse_args()

    data = json.loads(SATELLITES_JSON.read_text())

    rows = []
    for sat in data:
        family  = get_family_name(sat["OBJECT_NAME"])
        variant = get_variant(family, sat["OBJECT_NAME"], sat.get("LAUNCH"))
        rows.append({
            "NORAD_CAT_ID": sat["NORAD_CAT_ID"],
            "OBJECT_NAME":  sat["OBJECT_NAME"],
            "FAMILY_NAME":  family,
            "VARIANT_NAME": variant,
        })

    # Report: only families that actually split
    by_family = defaultdict(Counter)
    for r in rows:
        by_family[r["FAMILY_NAME"]][r["VARIANT_NAME"]] += 1

    print("Families split into variants:")
    n_split = 0
    for family in sorted(by_family):
        variants = by_family[family]
        if len(variants) == 1 and family in variants:
            continue
        n_split += 1
        total = sum(variants.values())
        print(f"\n  {family}  ({total} satellites)")
        for variant, count in variants.most_common():
            print(f"    {variant:<24} {count:>6}")

    n_variants = len({r["VARIANT_NAME"] for r in rows})
    n_families = len(by_family)
    print(f"\n{n_split} families split; {n_families} families -> {n_variants} variant labels total")

    if not args.report:
        with open(SATELLITES_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["NORAD_CAT_ID", "OBJECT_NAME", "FAMILY_NAME", "VARIANT_NAME"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Rewrote {SATELLITES_CSV.name} with VARIANT_NAME column ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
