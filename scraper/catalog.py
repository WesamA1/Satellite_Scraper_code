"""Rebuilds satellites.csv from a live Space-Track pull.

Consolidates what used to be two separate reference scripts
(reference/satellite_scraper.py, which fetches the catalog and derives
FAMILY_NAME, then reference/variants.py, which re-reads its JSON dump and
adds VARIANT_NAME) into one in-memory pass -- scraper/families.py never
regenerates the CSV itself, this is the piece that was never ported.

get_family_name()'s matching logic took real debugging: parenthetical
hints, then manual overrides, then suffix-stripping, in that priority
order. Do not simplify to naive regex -- it inflates family counts (see
CLAUDE.md). Ported verbatim from reference/satellite_scraper.py, same for
get_variant() from reference/variants.py.

Regenerating this file can shift family names (new launches, decays, or an
override matching differently against a new OBJECT_NAME string). This
module does not reconcile discovery_queue against that -- a stale family
there just sits unused, and a brand-new one gets queued the next time a
discovery job runs over the full family list (ensure_queued()'s INSERT OR
IGNORE is self-healing, just not instant).
"""

import csv
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.space-track.org"
LOGIN_URL = f"{BASE_URL}/ajaxauth/login"
SATCAT_URL = (
    f"{BASE_URL}/basicspacedata/query/class/satcat"
    "/OBJECT_TYPE/PAYLOAD"
    "/CURRENT/Y"
    "/DECAY/null-val"
    "/orderby/NORAD_CAT_ID asc"
    "/format/json"
    "/emptyresult/show"
)

# ── Family name extraction (reference/satellite_scraper.py, verbatim) ───────
#
# The simple regex r'[-\s]*\d+$' only handles cases where the object name ends
# in a pure trailing number (e.g. STARLINK-1234 -> STARLINK). It breaks on:
#
#   - Parenthetical hints:   COSMOS 2403 (GLONASS) -> should be GLONASS
#   - Letter+number suffixes: TRANSIT 5B -> should be TRANSIT
#   - Known operator names:  EUTE 10A (EUTE W2A) -> should be EUTELSAT
#
# The logic below handles all three, in priority order:
#   1. Check parenthetical content for a known family hint
#   2. Strip parenthetical, split on slash, check manual override prefixes
#   3. Regex strip trailing identifiers (numbers, letter+number, FM/F/M codes)
#   4. Fall back to the cleaned name

# Manual overrides: if OBJECT_NAME starts with one of these (word-boundary),
# map to the target family name. Matched longest-first to avoid prefix
# collisions (e.g. HULIANWANG DIGUI before HULIANWANG).
MANUAL_OVERRIDES = {
    # US military designations
    "NAVSTAR": "NAVSTAR",
    "MILSTAR": "MILSTAR",
    "AEHF": "AEHF",
    "WGS": "WGS",
    "SBIRS": "SBIRS",
    "DSP": "DSP",
    "DMSP": "DMSP",
    # Comsat operators
    "INTELSAT": "INTELSAT",
    "INMARSAT": "INMARSAT",
    "EUTELSAT": "EUTELSAT",
    "EUTE": "EUTELSAT",  # abbreviated form Space-Track uses
    "ECHOSTAR": "ECHOSTAR",
    "DIRECTV": "DIRECTV",
    "GALAXY": "GALAXY",
    "ANIK": "ANIK",
    "TELSTAR": "TELSTAR",
    "SYNCOM": "SYNCOM",
    # Earth observation
    "LANDSAT": "LANDSAT",
    "SENTINEL": "SENTINEL",
    "TERRA": "TERRA",
    "AQUA": "AQUA",
    "WORLDVIEW": "WORLDVIEW",
    "GEOEYE": "GEOEYE",
    "QUICKBIRD": "QUICKBIRD",
    # Weather
    "GOES": "GOES",
    "NOAA": "NOAA",
    "METEOSAT": "METEOSAT",
    "METOP": "METOP",
    "HIMAWARI": "HIMAWARI",
    "FENGYUN": "FENGYUN",
    # Science / exploration
    "EXPLORER": "EXPLORER",
    "PIONEER": "PIONEER",
    "VOYAGER": "VOYAGER",
    "MARINER": "MARINER",
    "VENERA": "VENERA",
    "LUNA": "LUNA",
    "RANGER": "RANGER",
    "VIKING": "VIKING",
    "HUBBLE": "HUBBLE",
    # Constellations
    "STARLINK": "STARLINK",
    "ONEWEB": "ONEWEB",
    "IRIDIUM": "IRIDIUM",
    "GLOBALSTAR": "GLOBALSTAR",
    "ORBCOMM": "ORBCOMM",
    "KUIPER": "KUIPER",
    "PLANET": "PLANET",
    "FLOCK": "FLOCK",
    "SKYSAT": "SKYSAT",
    "SPIRE": "SPIRE",
    "LEMUR": "LEMUR",
    "ICEYE": "ICEYE",
    "DOVE": "DOVE",
    "CAPELLA": "CAPELLA",
    # Russian
    "GLONASS": "GLONASS",
    "GORIZONT": "GORIZONT",
    "RADUGA": "RADUGA",
    "EKRAN": "EKRAN",
    "EXPRESS": "EXPRESS",
    "EKSPRESS": "EXPRESS",
    "MOLNIYA": "MOLNIYA",
    "RESURS": "RESURS",
    "METEOR": "METEOR",
    # Chinese
    "BEIDOU": "BEIDOU",
    "YAOGAN": "YAOGAN",
    "TIANHUI": "TIANHUI",
    "GAOFEN": "GAOFEN",
    "HAIYANG": "HAIYANG",
    "ZIYUAN": "ZIYUAN",
    "SHIJIAN": "SHIJIAN",
    "HULIANWANG DIGUI": "HULIANWANG DIGUI",
    "QIANFAN": "QIANFAN",
    # European
    "ARIANE": "ARIANE",
    "ARTEMIS": "ARTEMIS",
    "GIOVE": "GIOVE",
    "GALILEO": "GALILEO",
    "CRYOSAT": "CRYOSAT",
    # Japanese
    "ALOS": "ALOS",
    "ASNARO": "ASNARO",
    "IGS": "IGS",
    # Indian
    "CARTOSAT": "CARTOSAT",
    "RESOURCESAT": "RESOURCESAT",
    "RISAT": "RISAT",
    "INSAT": "INSAT",
    "GSAT": "GSAT",
    "IRNSS": "IRNSS",
    "NAVIC": "NAVIC",
}

# Parenthetical content that identifies the real family (e.g. COSMOS 2403 (GLONASS))
PAREN_FAMILY_HINTS = {
    "GLONASS": "GLONASS",
    "TELSTAR": "TELSTAR",
    "TOS": "ESSA",
    "OT": "ESSA",
    "AIMP": "EXPLORER",
    "IMP": "EXPLORER",
    "RAE": "EXPLORER",
    "INJUN": "EXPLORER",
    "GEOS": "EXPLORER",
    "ISIS": "ISIS",
    "GEO-IK": "GEO-IK",
    "MOP": "METEOSAT",
    "DBS": "DIRECTV",
    "TEMPO": "DIRECTV",
    "NIMIQ": "NIMIQ",
    "CAS": "EOLE",
}


def get_family_name(satellite_name: str) -> str:
    """Extract the family name from an OBJECT_NAME string.

    Examples:
        'STARLINK-1234'          -> 'STARLINK'
        'EXPLORER 7'             -> 'EXPLORER'
        'COSMOS 2403 (GLONASS)'  -> 'GLONASS'
        'TRANSIT 5B-1'           -> 'TRANSIT'
        'NAVSTAR 19 (USA 54)'    -> 'NAVSTAR'
        'EUTE 10A (EUTE W2A)'    -> 'EUTELSAT'
        'GLOBALSTAR M087'        -> 'GLOBALSTAR'
    """
    if not satellite_name:
        return ""

    name = satellite_name.strip().upper()

    # Step 1: Check parenthetical for a family hint
    paren_match = re.search(r"\(([^)]+)\)", name)
    if paren_match:
        paren_content = paren_match.group(1).strip()
        paren_first_word = paren_content.split()[0] if paren_content else ""
        # Strip trailing digits/letters to get base (e.g. "AIMP-D" -> "AIMP")
        paren_base = re.sub(r"[\s\-]?\d+[A-Z]?$", "", paren_first_word).strip()
        if paren_base in PAREN_FAMILY_HINTS and PAREN_FAMILY_HINTS[paren_base]:
            return PAREN_FAMILY_HINTS[paren_base]

    # Step 2: Remove parenthetical for further processing
    name_clean = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()

    # Step 3: Slash-separated alternates -- take the first part
    if "/" in name_clean:
        name_clean = name_clean.split("/")[0].strip()

    # Step 4: Manual overrides, longest key first (word-boundary match)
    for override_key in sorted(MANUAL_OVERRIDES.keys(), key=len, reverse=True):
        if name_clean.startswith(override_key):
            after = name_clean[len(override_key) : len(override_key) + 1]
            if after == "" or not after.isalnum():
                return MANUAL_OVERRIDES[override_key]

    # Step 5: Regex strip trailing identifiers
    stripped = name_clean
    # Strip " FM<digits>", " F<digits>", " M<digits>" (e.g. GLOBALSTAR M087)
    stripped = re.sub(r"\s+(?:FM|F|M)\d+[A-Z]?$", "", stripped)
    # Strip trailing " <number><optional letter>" (e.g. TIROS 3, TRANSIT 5B)
    stripped = re.sub(r"\s+\d+[A-Z]?(?:-\d+)?$", "", stripped)
    # Strip trailing " <letter><number>" (e.g. CS 3A)
    stripped = re.sub(r"\s+[A-Z]\d+[A-Z]?$", "", stripped)
    # Strip trailing "-<number>" (e.g. EXPLORER-7)
    stripped = re.sub(r"-\d+[A-Z]?$", "", stripped)
    stripped = stripped.strip()

    # Step 6: Fall back if stripping left nothing useful
    if not stripped or len(stripped) < 2:
        return name_clean

    return stripped


# ── Variant extraction (reference/variants.py, verbatim) ────────────────────
#
# A "family" groups satellites by program name (get_family_name above); a
# "variant" subdivides a family where successive generations are physically
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


def _d(s):
    """Parse a YYYY-MM-DD launch date; None if missing/malformed."""
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# Date-banded rules: family -> [(first_launch_date, variant), ...]. Bands are
# checked newest-first; a satellite gets the newest band whose start date is
# <= its launch date.
DATE_BANDS = {
    "STARLINK": [
        (date(2019, 5, 1), "STARLINK V0.9"),
        (date(2019, 11, 1), "STARLINK V1.0"),
        (date(2021, 6, 1), "STARLINK V1.5"),
        (date(2023, 2, 25), "STARLINK V2 MINI"),  # v1.5 overlap until ~Jun 2023
    ],
    "NAVSTAR": [
        (date(1978, 1, 1), "GPS BLOCK I"),
        (date(1989, 1, 1), "GPS BLOCK II"),
        (date(1990, 11, 1), "GPS BLOCK IIA"),
        (date(1997, 1, 1), "GPS BLOCK IIR"),
        (date(2005, 9, 1), "GPS BLOCK IIR-M"),
        (date(2010, 5, 1), "GPS BLOCK IIF"),
        (date(2018, 12, 1), "GPS BLOCK III"),
    ],
    "IRIDIUM": [
        (date(1997, 1, 1), "IRIDIUM BLOCK 1"),
        (date(2017, 1, 1), "IRIDIUM NEXT"),
    ],
    "GLONASS": [
        (date(1982, 1, 1), "GLONASS"),
        (date(2004, 1, 1), "GLONASS-M"),  # a few K1 prototypes fall here
        (date(2019, 1, 1), "GLONASS-K"),
    ],
    "GALILEO": [
        (date(2011, 1, 1), "GALILEO IOV"),
        (date(2014, 1, 1), "GALILEO FOC"),
    ],
    "BEIDOU": [
        (date(2000, 1, 1), "BEIDOU-1"),
        (date(2007, 1, 1), "BEIDOU-2"),
        (date(2017, 11, 1), "BEIDOU-3"),  # BD-3 demos (2015-16) land in BD-2
    ],
    "KUIPER": [
        (date(2023, 1, 1), "KUIPER PROTOTYPE"),
        (date(2025, 1, 1), "KUIPER BLOCK 1"),
    ],
    "ONEWEB": [
        (date(2019, 1, 1), "ONEWEB GEN1"),
    ],
}


# Name-digit rules: generation is encoded in the satellite name.
def _fengyun_variant(name):
    # FENGYUN 1A..1D / 2A..2H / 3A.. / 4A..: series digit = generation
    m = re.search(r"FENGYUN\s+(\d)", name)
    return f"FENGYUN-{m.group(1)}" if m else None


def _goes_variant(name):
    # GOES 1-7 spin-stabilized, 8-12 (I-M), 13-15 (N-P), 16+ (GOES-R series)
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
    "FENGYUN": _fengyun_variant,
    "GOES": _goes_variant,
    "METEOSAT": _meteosat_variant,
}


def get_variant(family: str, object_name: str, launch_date) -> str:
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


# ── Space-Track session (reference/satellite_scraper.py, verbatim shape) ────


def _credentials() -> tuple[str, str]:
    """Reads SPACETRACK_USERNAME/PASSWORD lazily (not at import time) so this
    module stays importable -- get_family_name()/get_variant() need no
    credentials at all -- and so a missing credential is a clear, reported
    RuntimeError instead of a bare KeyError on import."""
    username = os.environ.get("SPACETRACK_USERNAME", "").strip()
    password = os.environ.get("SPACETRACK_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("SPACETRACK_USERNAME/SPACETRACK_PASSWORD not set in .env -- see .env.example.")
    return username, password


def login(session: requests.Session) -> bool:
    """Log in to Space-Track and store the session cookie."""
    username, password = _credentials()
    response = session.post(LOGIN_URL, data={"identity": username, "password": password})
    return response.status_code == 200


def logout(session: requests.Session) -> None:
    """Log out of Space-Track."""
    session.get(f"{BASE_URL}/ajaxauth/logout")


def fetch_satellites(session: requests.Session) -> list[dict]:
    """Fetch NORAD IDs and names for all active payload satellites."""
    response = session.get(SATCAT_URL)
    if response.status_code != 200:
        raise RuntimeError(f"Space-Track fetch failed with status {response.status_code}.")
    return response.json()


# ── Consolidated fetch + derive + write ──────────────────────────────────────


def fetch_and_build_rows(session: requests.Session) -> tuple[list[dict], list[dict]]:
    """fetch_satellites() plus FAMILY_NAME/VARIANT_NAME derivation, in one
    in-memory pass -- replacing the old satellite_scraper.py -> satellites.json
    -> variants.py two-process flow with a single pipeline. Returns
    (raw_data, rows): the raw API dump (for save_raw_json) alongside the
    derived 4-column rows, so neither needs a second fetch to get the other."""
    data = fetch_satellites(session)
    rows = []
    for sat in data:
        family = get_family_name(sat["OBJECT_NAME"])
        variant = get_variant(family, sat["OBJECT_NAME"], sat.get("LAUNCH"))
        rows.append(
            {
                "NORAD_CAT_ID": sat["NORAD_CAT_ID"],
                "OBJECT_NAME": sat["OBJECT_NAME"],
                "FAMILY_NAME": family,
                "VARIANT_NAME": variant,
            }
        )
    return data, rows


def build_report(rows: list[dict]) -> str:
    """Families-split breakdown, same shape as reference/variants.py's
    --report output, as a string the caller prints (CLI does the printing,
    same division of labor doctor.run_checks() already uses)."""
    by_family = defaultdict(Counter)
    for r in rows:
        by_family[r["FAMILY_NAME"]][r["VARIANT_NAME"]] += 1

    lines = ["Families split into variants:"]
    n_split = 0
    for family in sorted(by_family):
        variants = by_family[family]
        if len(variants) == 1 and family in variants:
            continue
        n_split += 1
        total = sum(variants.values())
        lines.append(f"\n  {family}  ({total} satellites)")
        for variant, count in variants.most_common():
            lines.append(f"    {variant:<24} {count:>6}")

    n_variants = len({r["VARIANT_NAME"] for r in rows})
    n_families = len(by_family)
    lines.append(f"\n{n_split} families split; {n_families} families -> {n_variants} variant labels total")
    return "\n".join(lines)


def write_catalog_csv(rows: list[dict], csv_path: Path, backup: bool = True) -> Path | None:
    """Writes the 4-column CSV. If csv_path already exists and backup=True,
    copies it to {stem}.{UTC timestamp}.bak.csv first (same timestamp format
    scraper/logging_setup.py uses for log filenames) -- returns that backup
    path, or None if there was nothing to back up."""
    backup_path = None
    if backup and csv_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = csv_path.with_name(f"{csv_path.stem}.{timestamp}.bak{csv_path.suffix}")
        shutil.copy2(csv_path, backup_path)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["NORAD_CAT_ID", "OBJECT_NAME", "FAMILY_NAME", "VARIANT_NAME"])
        writer.writeheader()
        writer.writerows(rows)

    return backup_path


def save_raw_json(data: list[dict], json_path: Path) -> None:
    """Raw Space-Track dump, kept for debuggability -- never read back by
    this module, just a trail for re-deriving families/variants later
    without hitting the API again."""
    json_path.write_text(json.dumps(data, indent=2))


def refresh_catalog(
    session: requests.Session,
    csv_path: Path = Path("satellites.csv"),
    json_path: Path = Path("satellites.json"),
    report_only: bool = False,
) -> dict:
    """Logs in, fetches the catalog, derives family/variant names, and
    (unless report_only) backs up the existing CSV and writes the new one
    plus the raw JSON dump. Raises RuntimeError on missing credentials or a
    login failure, with nothing written in either case."""
    if not login(session):
        raise RuntimeError("Space-Track login failed -- check SPACETRACK_USERNAME/PASSWORD in .env.")

    try:
        data, rows = fetch_and_build_rows(session)
    finally:
        logout(session)

    backup_path = None
    if not report_only:
        backup_path = write_catalog_csv(rows, csv_path)
        save_raw_json(data, json_path)

    return {
        "row_count": len(rows),
        "family_count": len({r["FAMILY_NAME"] for r in rows}),
        "variant_count": len({r["VARIANT_NAME"] for r in rows}),
        "report_text": build_report(rows),
        "backup_path": str(backup_path) if backup_path else None,
    }
