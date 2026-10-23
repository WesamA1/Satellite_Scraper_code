# sources/nasa_images.py
# Fetches images for a satellite family from the NASA Image and Video Library
# (https://images.nasa.gov) via its public API — no API key required.
#
# API docs: https://images-api.nasa.gov
# The /search endpoint returns full-size asset links directly in the response
# (rel="canonical" is the original, the ~large rendition is 1920px), so no
# second asset lookup is needed per image.

import requests
import time
import csv
import re
from pathlib import Path

SEARCH_URL = "https://images-api.nasa.gov/search"

# Source tag — prefixed to every file this script downloads so we can tell
# NASA images apart from images dropped here by other sources.
SOURCE_TAG = "nasa"

# Cap per family: popular queries (ISS, Starlink) return hundreds of hits.
MAX_IMAGES_PER_FAMILY = 30

NOISE_WORDS = [
    "logo", "patch", "emblem", "flag", "icon",
    "chart", "graph", "diagram", "map",
    "statistics", "stats", "streak", "trail",
]


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31",
    })
    return session


def get_search_term(family_name):
    """Extract the core searchable term from a family name.

    Examples:
        '3CAT-5/A (TYVAK-0161)' -> '3cat-5'
        'GPS IIR'               -> 'gps iir'
        'STARLINK'              -> 'starlink'
    """
    name = re.sub(r'\(.*?\)', '', family_name)
    name = name.split('/')[0]
    return name.strip().lower()


def search_images(session, family_name):
    """Search the NASA Image Library for *family_name*.

    Returns the raw JSON response dict ({} on failure).
    """
    term = get_search_term(family_name)
    for attempt in range(1, 4):
        try:
            resp = session.get(SEARCH_URL, params={
                "q":          f"{term} satellite",
                "media_type": "image",
                "page_size":  100,
            }, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                return resp.json()
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/3)...")
                time.sleep(wait)
                continue
            print(f"  Search failed: HTTP {resp.status_code}")
            return {}
        except Exception as e:
            print(f"  Search error (attempt {attempt}/3): {e}")
            time.sleep(30)
    return {}


def _pick_asset_link(links):
    """Choose the best image URL from a search item's links list.

    Prefers the ~large rendition (1920px, dataset-friendly size), then the
    canonical original, then any other image link.
    """
    large = orig = other = None
    for link in links:
        href = link.get("href", "")
        if link.get("render") != "image" or not href:
            continue
        if "~large" in href:
            large = href
        elif link.get("rel") == "canonical" or "~orig" in href:
            orig = href
        elif other is None:
            other = href
    return large or orig or other


def filter_images(raw, family_name):
    """Extract relevant image dicts from a search_images response.

    Requires the search term to appear in the title, keywords, or description
    (the NASA library query matches loosely across all fields).
    """
    term = get_search_term(family_name)
    items = raw.get("collection", {}).get("items", [])

    results = []
    for item in items:
        data_list = item.get("data", [])
        if not data_list:
            continue
        data  = data_list[0]
        title = (data.get("title") or "").lower()
        desc  = (data.get("description") or "").lower()
        keys  = " ".join(data.get("keywords") or []).lower()

        if any(word in title for word in NOISE_WORDS):
            continue
        if term not in title and term not in keys and term not in desc:
            continue

        url = _pick_asset_link(item.get("links", []))
        if not url:
            continue

        ext  = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpeg"
        results.append({
            "title":       data.get("title", ""),
            "url":         url,
            "full_url":    url,
            "mime":        f"image/{ext}",
            "description": data.get("description", ""),
            "author":      data.get("photographer", "") or data.get("secondary_creator", ""),
            "license":     "NASA (public domain)",
            "date":        data.get("date_created", ""),
            "nasa_id":     data.get("nasa_id", ""),
        })

        if len(results) >= MAX_IMAGES_PER_FAMILY:
            break

    return results


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
    with open("satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)

    families = sorted(families)
    print(f"Found {len(families)} unique satellite families")

    no_images_log = Path("no_images_found_nasa.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_nasa.txt"

        if has_source_images(dest_dir):
            print("  Skipping — already downloaded from NASA")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        raw    = search_images(session, family)
        images = filter_images(raw, family)
        print(f"  Found {len(images)} images after filtering")

        time.sleep(1)  # polite pause between API searches

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
            print(f"  {'✓' if ok else '✗'} {img['title'][:50]}")

            if ok:
                dest_dir.mkdir(parents=True, exist_ok=True)
                desc = re.sub(r'\s+', ' ', img['description']).strip()[:500]
                with open(meta_path, "a", encoding="utf-8") as mf:
                    mf.write(f"file:        {dest.name}\n")
                    mf.write(f"title:       {img['title']}\n")
                    mf.write(f"source_url:  {img['full_url']}\n")
                    mf.write(f"mime:        {img['mime']}\n")
                    mf.write(f"date:        {img['date']}\n")
                    mf.write(f"author:      {img['author']}\n")
                    mf.write(f"license:     {img['license']}\n")
                    mf.write(f"nasa_id:     {img['nasa_id']}\n")
                    mf.write(f"description: {desc}\n")
                    mf.write("\n")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    print("  Too many failures — pausing 120s...")
                    time.sleep(120)
                    consecutive_failures = 0
            time.sleep(2)

        time.sleep(3)

    needs_review = [
        f for f in families
        if not has_source_images(Path("satellite_images") / f)
    ]
    if needs_review:
        with open("needs_review_nasa.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no NASA images → needs_review_nasa.txt")
