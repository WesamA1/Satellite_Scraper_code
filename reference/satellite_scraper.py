# satellite_pipeline.py
# Fetches active satellite data from Space-Track and saves to JSON and CSV.
#
# First time setup — run this in your terminal before running the script:
# pip install requests

import os
import requests
import json
import csv
import re

from dotenv import load_dotenv

load_dotenv()


# ── Configuration ─────────────────────────────────────────────────────────────

# Space-Track credentials (set in .env, see .env.example)
USERNAME = os.environ["SPACETRACK_USERNAME"]
PASSWORD = os.environ["SPACETRACK_PASSWORD"]

# Base URL for all API calls
BASE_URL = "https://www.space-track.org"

# Output files
SATELLITES_JSON = "satellites.json"
SATELLITES_CSV  = "satellites.csv"


# ── Authentication ────────────────────────────────────────────────────────────

def login(session):
    """Log in to Space-Track and store the session cookie."""
    response = session.post(
        f"{BASE_URL}/ajaxauth/login",
        data={"identity": USERNAME, "password": PASSWORD}
    )
    if response.status_code == 200:
        print("Login successful!")
        return True
    else:
        print(f"Login failed. Status code: {response.status_code}")
        return False


def logout(session):
    """Log out of Space-Track."""
    session.get(f"{BASE_URL}/ajaxauth/logout")
    print("Logged out.")


# ── Fetch Satellites ──────────────────────────────────────────────────────────

def fetch_satellites(session):
    """Fetch NORAD IDs and names for all active payload satellites."""
    print("Fetching satellite data from Space-Track...")

    url = (
        f"{BASE_URL}/basicspacedata/query/class/satcat"
        "/OBJECT_TYPE/PAYLOAD"
        "/CURRENT/Y"
        "/DECAY/null-val"
        "/orderby/NORAD_CAT_ID asc"
        "/format/json"
        "/emptyresult/show"
    )

    response = session.get(url)

    if response.status_code != 200:
        print(f"Fetch failed. Status code: {response.status_code}")
        return None

    data = response.json()
    print(f"Found {len(data)} satellites!")
    return data


# ── Family name extraction ───────────────────────────────────────────────────
#
# The simple regex r'[-\s]*\d+$' only handles cases where the object name ends
# in a pure trailing number (e.g. STARLINK-1234 → STARLINK). It breaks on:
#
#   - Parenthetical hints:   COSMOS 2403 (GLONASS) → should be GLONASS
#   - Letter+number suffixes: TRANSIT 5B → should be TRANSIT
#   - Known operator names:  EUTE 10A (EUTE W2A) → should be EUTELSAT
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
    "NAVSTAR": "NAVSTAR", "MILSTAR": "MILSTAR", "AEHF": "AEHF",
    "WGS": "WGS", "SBIRS": "SBIRS", "DSP": "DSP", "DMSP": "DMSP",

    # Comsat operators
    "INTELSAT": "INTELSAT", "INMARSAT": "INMARSAT", "EUTELSAT": "EUTELSAT",
    "EUTE": "EUTELSAT",  # abbreviated form Space-Track uses
    "ECHOSTAR": "ECHOSTAR", "DIRECTV": "DIRECTV", "GALAXY": "GALAXY",
    "ANIK": "ANIK", "TELSTAR": "TELSTAR", "SYNCOM": "SYNCOM",

    # Earth observation
    "LANDSAT": "LANDSAT", "SENTINEL": "SENTINEL", "TERRA": "TERRA",
    "AQUA": "AQUA", "WORLDVIEW": "WORLDVIEW", "GEOEYE": "GEOEYE",
    "QUICKBIRD": "QUICKBIRD",

    # Weather
    "GOES": "GOES", "NOAA": "NOAA", "METEOSAT": "METEOSAT",
    "METOP": "METOP", "HIMAWARI": "HIMAWARI", "FENGYUN": "FENGYUN",

    # Science / exploration
    "EXPLORER": "EXPLORER", "PIONEER": "PIONEER", "VOYAGER": "VOYAGER",
    "MARINER": "MARINER", "VENERA": "VENERA", "LUNA": "LUNA",
    "RANGER": "RANGER", "VIKING": "VIKING", "HUBBLE": "HUBBLE",

    # Constellations
    "STARLINK": "STARLINK", "ONEWEB": "ONEWEB", "IRIDIUM": "IRIDIUM",
    "GLOBALSTAR": "GLOBALSTAR", "ORBCOMM": "ORBCOMM", "KUIPER": "KUIPER",
    "PLANET": "PLANET", "FLOCK": "FLOCK", "SKYSAT": "SKYSAT",
    "SPIRE": "SPIRE", "LEMUR": "LEMUR", "ICEYE": "ICEYE",
    "DOVE": "DOVE", "CAPELLA": "CAPELLA",

    # Russian
    "GLONASS": "GLONASS", "GORIZONT": "GORIZONT", "RADUGA": "RADUGA",
    "EKRAN": "EKRAN", "EXPRESS": "EXPRESS", "EKSPRESS": "EXPRESS",
    "MOLNIYA": "MOLNIYA", "RESURS": "RESURS", "METEOR": "METEOR",

    # Chinese
    "BEIDOU": "BEIDOU", "YAOGAN": "YAOGAN", "TIANHUI": "TIANHUI",
    "GAOFEN": "GAOFEN", "HAIYANG": "HAIYANG", "ZIYUAN": "ZIYUAN",
    "SHIJIAN": "SHIJIAN", "HULIANWANG DIGUI": "HULIANWANG DIGUI",
    "QIANFAN": "QIANFAN",

    # European
    "ARIANE": "ARIANE", "ARTEMIS": "ARTEMIS", "GIOVE": "GIOVE",
    "GALILEO": "GALILEO", "CRYOSAT": "CRYOSAT",

    # Japanese
    "ALOS": "ALOS", "ASNARO": "ASNARO", "IGS": "IGS",

    # Indian
    "CARTOSAT": "CARTOSAT", "RESOURCESAT": "RESOURCESAT", "RISAT": "RISAT",
    "INSAT": "INSAT", "GSAT": "GSAT", "IRNSS": "IRNSS", "NAVIC": "NAVIC",
}

# Parenthetical content that identifies the real family (e.g. COSMOS 2403 (GLONASS))
PAREN_FAMILY_HINTS = {
    "GLONASS": "GLONASS", "TELSTAR": "TELSTAR", "TOS": "ESSA", "OT": "ESSA",
    "AIMP": "EXPLORER", "IMP": "EXPLORER", "RAE": "EXPLORER",
    "INJUN": "EXPLORER", "GEOS": "EXPLORER", "ISIS": "ISIS",
    "GEO-IK": "GEO-IK", "MOP": "METEOSAT", "DBS": "DIRECTV",
    "TEMPO": "DIRECTV", "NIMIQ": "NIMIQ", "CAS": "EOLE",
}


def get_family_name(satellite_name):
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
    paren_match = re.search(r'\(([^)]+)\)', name)
    if paren_match:
        paren_content = paren_match.group(1).strip()
        paren_first_word = paren_content.split()[0] if paren_content else ""
        # Strip trailing digits/letters to get base (e.g. "AIMP-D" -> "AIMP")
        paren_base = re.sub(r'[\s\-]?\d+[A-Z]?$', '', paren_first_word).strip()
        if paren_base in PAREN_FAMILY_HINTS and PAREN_FAMILY_HINTS[paren_base]:
            return PAREN_FAMILY_HINTS[paren_base]

    # Step 2: Remove parenthetical for further processing
    name_clean = re.sub(r'\s*\([^)]*\)\s*', ' ', name).strip()

    # Step 3: Slash-separated alternates — take the first part
    if '/' in name_clean:
        name_clean = name_clean.split('/')[0].strip()

    # Step 4: Manual overrides, longest key first (word-boundary match)
    for override_key in sorted(MANUAL_OVERRIDES.keys(), key=len, reverse=True):
        if name_clean.startswith(override_key):
            after = name_clean[len(override_key):len(override_key) + 1]
            if after == "" or not after.isalnum():
                return MANUAL_OVERRIDES[override_key]

    # Step 5: Regex strip trailing identifiers
    stripped = name_clean
    # Strip " FM<digits>", " F<digits>", " M<digits>" (e.g. GLOBALSTAR M087)
    stripped = re.sub(r'\s+(?:FM|F|M)\d+[A-Z]?$', '', stripped)
    # Strip trailing " <number><optional letter>" (e.g. TIROS 3, TRANSIT 5B)
    stripped = re.sub(r'\s+\d+[A-Z]?(?:-\d+)?$', '', stripped)
    # Strip trailing " <letter><number>" (e.g. CS 3A)
    stripped = re.sub(r'\s+[A-Z]\d+[A-Z]?$', '', stripped)
    # Strip trailing "-<number>" (e.g. EXPLORER-7)
    stripped = re.sub(r'-\d+[A-Z]?$', '', stripped)
    stripped = stripped.strip()

    # Step 6: Fall back if stripping left nothing useful
    if not stripped or len(stripped) < 2:
        return name_clean

    return stripped


# ── Save Data ─────────────────────────────────────────────────────────────────

def save_satellites_json(data):
    """Save the raw satellite data to a JSON file."""
    with open(SATELLITES_JSON, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved raw data to {SATELLITES_JSON}")


def save_satellites_csv(data):
    """Save satellite data with family names to CSV."""
    fields = ["NORAD_CAT_ID", "OBJECT_NAME", "FAMILY_NAME"]
    rows = [
        {
            "NORAD_CAT_ID": sat["NORAD_CAT_ID"],
            "OBJECT_NAME":  sat["OBJECT_NAME"],
            "FAMILY_NAME":  get_family_name(sat["OBJECT_NAME"]),
        }
        for sat in data
    ]

    with open(SATELLITES_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to {SATELLITES_CSV}")

    # Print unique family count as a sanity check
    families = {row["FAMILY_NAME"] for row in rows}
    print(f"Unique satellite families: {len(families)}")

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    session = requests.Session()

    if login(session):
        data = fetch_satellites(session)

        if data:
            print("\nFirst 3 satellites (preview):")
            for sat in data[:3]:
                print(f"  NORAD ID: {sat['NORAD_CAT_ID']} | Name: {sat['OBJECT_NAME']} | Family: {get_family_name(sat['OBJECT_NAME'])}")

            save_satellites_json(data)
            save_satellites_csv(data)

        logout(session)