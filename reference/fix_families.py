"""
Corrected family extraction for satellites.csv.

Logic, in priority order:
1. If OBJECT_NAME has a parenthetical hint like "(GLONASS)", "(USA 145)",
   "(TELSTAR 10)", use the parenthetical content as the family source.
2. Strip trailing identifiers: numbers, letter+number suffixes (5B, 4A),
   FM-prefixed IDs (FM123), dash-separated codes.
3. Apply manual overrides for known-tricky cases.
4. Fall back to full OBJECT_NAME if nothing strips.
"""

import csv
import re
from collections import Counter

# Manual overrides for families where automatic rules fail or need help.
# Key: exact OBJECT_NAME substring to match (case-insensitive), Value: family name.
# Applied BEFORE regex extraction.
MANUAL_OVERRIDES = {
    # Soviet/Russian — parentheticals are the real family identifier
    # (handled by paren rule, listed here for documentation)

    # US military designations — "USA XXX" is a cover name, parenthetical or
    # prefix tells the real family
    "NAVSTAR": "NAVSTAR",  # GPS satellites
    "MILSTAR": "MILSTAR",
    "AEHF": "AEHF",
    "WGS": "WGS",
    "SBIRS": "SBIRS",
    "DSP": "DSP",
    "DMSP": "DMSP",

    # Common comsat operators
    "INTELSAT": "INTELSAT",
    "INMARSAT": "INMARSAT",
    "EUTELSAT": "EUTELSAT",
    "EUTE": "EUTELSAT",  # abbreviated form in CSV
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
    "HIMAWARI": "HIMAWARI",
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

# Parenthetical content that should map to a family (for the "COSMOS 2403 (GLONASS)" case)
PAREN_FAMILY_HINTS = {
    "GLONASS": "GLONASS",
    "USA": None,  # "USA XXX" in a paren means military — use the prefix of OBJECT_NAME instead
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


def extract_family(object_name: str) -> str:
    """Extract family name from an OBJECT_NAME string."""
    if not object_name:
        return ""

    name = object_name.strip().upper()

    # Step 1: Check for parenthetical hint
    paren_match = re.search(r'\(([^)]+)\)', name)
    paren_content = None
    if paren_match:
        paren_content = paren_match.group(1).strip()
        # Extract the leading word from the parenthetical
        paren_first_word = paren_content.split()[0] if paren_content else ""
        # Strip trailing digits/letters from first word to get base
        paren_base = re.sub(r'[\s\-]?\d+[A-Z]?$', '', paren_first_word).strip()
        if paren_base in PAREN_FAMILY_HINTS and PAREN_FAMILY_HINTS[paren_base]:
            return PAREN_FAMILY_HINTS[paren_base]

    # Step 2: Remove parenthetical for further processing
    name_clean = re.sub(r'\s*\([^)]*\)\s*', ' ', name).strip()

    # Step 3: Handle slash-separated alternates — take the first part
    if '/' in name_clean:
        name_clean = name_clean.split('/')[0].strip()

    # Step 4: Check manual overrides against the leading tokens
    #   Try longest-first match (e.g., "HULIANWANG DIGUI" before "HULIANWANG")
    for override_key in sorted(MANUAL_OVERRIDES.keys(), key=len, reverse=True):
        if name_clean.startswith(override_key):
            # Make sure it's a word boundary (avoid "STAR" matching "STARLINK" wrong direction)
            after = name_clean[len(override_key):len(override_key)+1]
            if after == "" or not after.isalnum():
                return MANUAL_OVERRIDES[override_key]

    # Step 5: Regex strip — remove trailing identifiers
    # Patterns to strip from the end:
    #   " 123", " 5B", " 4A-2", " FM123", " M123", "-123", " F12"
    stripped = name_clean
    # Strip " FM<digits>", " F<digits>", " M<digits>" (e.g. "GLOBALSTAR M087")
    stripped = re.sub(r'\s+(?:FM|F|M)\d+[A-Z]?$', '', stripped)
    # Strip trailing " <number><optional letter>" (e.g. "TIROS 3", "TRANSIT 5B")
    stripped = re.sub(r'\s+\d+[A-Z]?(?:-\d+)?$', '', stripped)
    # Strip trailing " <letter><number>" (e.g. "CS 3A")
    stripped = re.sub(r'\s+[A-Z]\d+[A-Z]?$', '', stripped)
    # Strip trailing "-<number>" (e.g. "EXPLORER-7")
    stripped = re.sub(r'-\d+[A-Z]?$', '', stripped)
    stripped = stripped.strip()

    # Step 6: If stripped is empty or just noise, fall back to name_clean
    if not stripped or len(stripped) < 2:
        return name_clean

    return stripped


def main():
    old_families = []
    new_families = []
    rows = []

    with open('/mnt/user-data/uploads/satellites.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            old_families.append(row['FAMILY_NAME'])
            new_families.append(extract_family(row['OBJECT_NAME']))

    old_counts = Counter(old_families)
    new_counts = Counter(new_families)

    print(f"Total satellites: {len(rows)}")
    print()
    print(f"OLD: {len(old_counts)} unique families, "
          f"{sum(1 for _, c in old_counts.items() if c == 1)} singletons")
    print(f"NEW: {len(new_counts)} unique families, "
          f"{sum(1 for _, c in new_counts.items() if c == 1)} singletons")
    print()

    print("=" * 70)
    print("TOP 30 FAMILIES — NEW extraction:")
    print("=" * 70)
    for fam, count in new_counts.most_common(30):
        print(f"  {count:6d}  {fam}")
    print()

    print("=" * 70)
    print("COVERAGE CHECK: How many satellites are in top-N families?")
    print("=" * 70)
    for n in [10, 25, 50, 100, 200]:
        top_n_total = sum(c for _, c in new_counts.most_common(n))
        pct = 100 * top_n_total / len(rows)
        print(f"  Top {n:3d} families cover {top_n_total:6d} satellites ({pct:.1f}%)")
    print()

    print("=" * 70)
    print("SAMPLE: before/after for specific cases")
    print("=" * 70)
    samples_of_interest = [
        "COSMOS 2403 (GLONASS)",
        "COSMOS 2464 (GLONASS)",
        "TRANSIT 2A",
        "TRANSIT 5B-1",
        "NAVSTAR 19 (USA 54)",
        "TELSTAR 2 (A-41)",
        "SYNCOM 2 (A 26)",
        "EXPLORER 33 (AIMP-D)",
        "EXPRESS AMU",
        "EUTE 10A (EUTE W2A)",
        "GLOBALSTAR M087",
        "ORBCOMM FM109",
        "FLOCK 4H 1",
        "ICEYE-X14",
        "SOLRAD 3/INJUN 1",
        "METEOSAT 4 (MOP 1)",
    ]
    for obj in samples_of_interest:
        # find matching row
        old = "(not in csv)"
        for r in rows:
            if r['OBJECT_NAME'] == obj:
                old = r['FAMILY_NAME']
                break
        new = extract_family(obj)
        marker = "✓" if new != old else " "
        print(f"  {marker} {obj}")
        print(f"        OLD: {old}")
        print(f"        NEW: {new}")
    print()

    # Write the corrected CSV
    out_path = '/mnt/user-data/outputs/satellites_corrected.csv'
    import os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['NORAD_CAT_ID', 'OBJECT_NAME', 'FAMILY_NAME'])
        writer.writeheader()
        for row, new_fam in zip(rows, new_families):
            writer.writerow({
                'NORAD_CAT_ID': row['NORAD_CAT_ID'],
                'OBJECT_NAME': row['OBJECT_NAME'],
                'FAMILY_NAME': new_fam,
            })
    print(f"Wrote corrected CSV to: {out_path}")


if __name__ == "__main__":
    main()
