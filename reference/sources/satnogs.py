# sources/satnogs.py
# Fetches images for a satellite family from SatNOGS DB
# (https://db.satnogs.org) — a community-maintained satellite database with
# photos for thousands of cubesats and smallsats that the bigger archives miss.
#
# Strategy: the API returns the ENTIRE satellite catalog in one JSON response
# (~1.3 MB, no pagination, no auth). We download and cache it once, then match
# our own NORAD IDs from satellites.csv against it — exact matches, no keyword
# guessing. Each catalog entry carries at most one photo, but a family has
# many member satellites, so families still net multiple images.

import requests
import json
import time
import csv
from pathlib import Path

CATALOG_URL     = "https://db.satnogs.org/api/satellites/?format=json"
MEDIA_BASE_URL  = "https://db.satnogs.org/media/"
CATALOG_CACHE   = Path("satnogs_catalog_cache.json")
CATALOG_MAX_AGE = 86400  # re-download after 24 hours

SATELLITES_CSV = "satellites.csv"

# Source tag — prefixed to every file this script downloads.
SOURCE_TAG = "satnogs"

# Cap per family: STARLINK alone has thousands of members but they all share
# a handful of distinct photos anyway.
MAX_IMAGES_PER_FAMILY = 30


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31",
    })
    return session


# ── Catalog cache ─────────────────────────────────────────────────────────────

def _load_catalog(session):
    """Return the full SatNOGS satellite list, using a local cache when fresh."""
    if CATALOG_CACHE.exists():
        age = time.time() - CATALOG_CACHE.stat().st_mtime
        if age < CATALOG_MAX_AGE:
            return json.loads(CATALOG_CACHE.read_text(encoding="utf-8"))

    print("  Downloading SatNOGS catalog (cached for 24 h)...")
    for attempt in range(1, 4):
        try:
            resp = session.get(CATALOG_URL, timeout=120)
            if resp.status_code == 200:
                CATALOG_CACHE.write_bytes(resp.content)
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/3)...")
                time.sleep(wait)
            else:
                print(f"  Catalog fetch failed: HTTP {resp.status_code}")
                return []
        except Exception as e:
            print(f"  Catalog fetch error: {e}")
            time.sleep(30)
    return []


def _catalog_by_norad(catalog):
    """Index catalog entries that have a photo by NORAD ID."""
    by_norad = {}
    for entry in catalog:
        norad = entry.get("norad_cat_id")
        image = entry.get("image") or ""
        if norad and image:
            by_norad[int(norad)] = entry
    return by_norad


def _family_norad_map():
    """Map FAMILY_NAME -> sorted list of NORAD IDs from satellites.csv."""
    mapping = {}
    with open(SATELLITES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            family = row["FAMILY_NAME"].strip()
            if not family:
                continue
            try:
                norad = int(row["NORAD_CAT_ID"])
            except (ValueError, KeyError):
                continue
            mapping.setdefault(family, []).append(norad)
    for norads in mapping.values():
        norads.sort()
    return mapping


def _image_url(entry):
    """Build the full media URL from a catalog entry's image path."""
    image = entry.get("image") or ""
    if image.startswith("http"):
        return image
    return MEDIA_BASE_URL + image.lstrip("/")


# ── Public interface (mirrors wikimedia.py) ───────────────────────────────────

# Lazily built module-level caches so search_images() can be called per family
# without re-parsing the catalog and CSV every time.
_BY_NORAD   = None
_FAMILY_MAP = None


def search_images(session, family_name):
    """Find SatNOGS photos for every member satellite of *family_name*.

    Returns a list of image dicts:
      { "url", "title", "full_url", "mime", "norad", "sat_name" }
    """
    global _BY_NORAD, _FAMILY_MAP
    if _BY_NORAD is None:
        _BY_NORAD = _catalog_by_norad(_load_catalog(session))
    if _FAMILY_MAP is None:
        _FAMILY_MAP = _family_norad_map()

    results = []
    seen_urls = set()
    for norad in _FAMILY_MAP.get(family_name, []):
        entry = _BY_NORAD.get(norad)
        if not entry:
            continue
        url = _image_url(entry)
        if url in seen_urls:
            continue  # sister satellites often share one photo
        seen_urls.add(url)
        ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpeg"
        results.append({
            "title":    entry.get("name", ""),
            "url":      url,
            "full_url": url,
            "mime":     f"image/{ext}",
            "norad":    norad,
            "sat_name": entry.get("name", ""),
        })
        if len(results) >= MAX_IMAGES_PER_FAMILY:
            break

    return results


def filter_images(images, family_name):
    """SatNOGS photos are matched by NORAD ID, so keyword noise filtering is
    unnecessary — just drop SVGs and non-images for interface parity."""
    return [
        img for img in images
        if img["mime"].startswith("image/") and "svg" not in img["mime"]
    ]


def download_image(session, url, dest_path):
    """Download a single image to dest_path. Returns True on success."""
    for attempt in range(1, 4):
        try:
            resp = session.get(url, stream=True, timeout=30)
        except Exception as e:
            print(f"    Download error: {e}")
            return False

        if resp.status_code == 200:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        elif resp.status_code == 429:
            wait = 60 * attempt
            print(f"    Rate limited, waiting {wait}s (attempt {attempt}/3)...")
            time.sleep(wait)
        else:
            print(f"    Failed — HTTP {resp.status_code}")
            return False

    print("    Giving up after 3 attempts")
    return False


def has_source_images(dest_dir):
    """True if dest_dir already contains images downloaded by THIS source."""
    if not dest_dir.exists():
        return False
    return any(f"_{SOURCE_TAG}_" in p.name for p in dest_dir.iterdir() if p.is_file())


if __name__ == "__main__":
    families = set()
    with open(SATELLITES_CSV, newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)

    families = sorted(families)
    print(f"Found {len(families)} unique satellite families")

    no_images_log = Path("no_images_found_satnogs.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    # Pre-load the catalog once for the whole run
    _BY_NORAD   = _catalog_by_norad(_load_catalog(session))
    _FAMILY_MAP = _family_norad_map()
    print(f"SatNOGS catalog loaded: {len(_BY_NORAD)} satellites with photos")

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_satnogs.txt"

        if has_source_images(dest_dir):
            print("  Skipping — already downloaded from SatNOGS")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        images = filter_images(search_images(session, family), family)
        print(f"  Found {len(images)} images")

        if not images:
            with open(no_images_log, "a") as f:
                f.write(family + "\n")
            already_tried.add(family)
            continue

        consecutive_failures = 0
        for j, img in enumerate(images, start=1):
            ext  = Path(img["url"].split("?")[0]).suffix.lower() or ".jpg"
            dest = dest_dir / f"{family}_{SOURCE_TAG}_{j:02d}{ext}"
            ok   = download_image(session, img["url"], dest)
            print(f"  {'✓' if ok else '✗'} {img['sat_name'][:40]} (NORAD {img['norad']})")

            if ok:
                dest_dir.mkdir(parents=True, exist_ok=True)
                with open(meta_path, "a", encoding="utf-8") as mf:
                    mf.write(f"file:       {dest.name}\n")
                    mf.write(f"title:      {img['title']}\n")
                    mf.write(f"source_url: {img['full_url']}\n")
                    mf.write(f"mime:       {img['mime']}\n")
                    mf.write(f"norad_id:   {img['norad']}\n")
                    mf.write(f"license:    CC-BY-SA 4.0 (SatNOGS DB)\n")
                    mf.write("\n")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("  Too many failures — pausing 120s...")
                    time.sleep(120)
                    consecutive_failures = 0
            time.sleep(2)

        time.sleep(1)

    needs_review = [
        f for f in families
        if not has_source_images(Path("satellite_images") / f)
    ]
    if needs_review:
        with open("needs_review_satnogs.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no SatNOGS images → needs_review_satnogs.txt")
