# sources/wikimedia.py
# Fetches images for a satellite family from Wikimedia Commons

import os
import requests
import json
import time
from pathlib import Path
import csv
import re

from dotenv import load_dotenv

load_dotenv()

# Wikimedia Credentials (set in .env, see .env.example)
USERNAME = os.environ["WIKIMEDIA_BOT_USERNAME"]
PASSWORD = os.environ["WIKIMEDIA_BOT_PASSWORD"]

# API Endpoints
LOGIN_URL  = "https://meta.wikimedia.org/w/api.php"
SEARCH_URL = "https://commons.wikimedia.org/w/api.php"

# Source tag — prefixed to every file this script downloads so we can tell
# Wikimedia images apart from images dropped here by other sources (NASA, etc.)
SOURCE_TAG = "wikimedia"


def make_session():
    """Create a requests session with the correct headers for Wikimedia."""
    session = requests.Session()

    # Must set User-Agent or Wikimedia returns 403
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31"
    })

    return session


def login(session):
    print("Getting login token...")
    resp = session.get(LOGIN_URL, params={
        "action": "query",
        "meta":   "tokens",
        "type":   "login",
        "format": "json",
    })
    token = resp.json()["query"]["tokens"]["logintoken"]
    print(f"Got token: {token[:10]}...")

    print("Logging in...")
    for attempt in range(1, 4):
        resp = session.post(LOGIN_URL, data={
            "action":     "login",
            "lgname":     USERNAME,
            "lgpassword": PASSWORD,
            "lgtoken":    token,
            "format":     "json",
        })

        if resp.status_code == 429:
            wait = 60 * attempt  # 60s, 120s, 180s
            print(f"  Rate limited on login (attempt {attempt}/3), waiting {wait}s...")
            time.sleep(wait)
            continue

        if not resp.text.strip():
            print(f"  Empty login response (attempt {attempt}/3)")
            time.sleep(30)
            continue

        result = resp.json()
        if result.get("login", {}).get("result") == "Success":
            print("  Logged in successfully")
            return True
        else:
            print(f"  Login failed: {result}")
            return False

    print("  Gave up after 3 login attempts")
    return False

def search_images(session, family_name):
    for attempt in range(1, 4):
        try:
            resp = session.get(SEARCH_URL, params={
                "action":       "query",
                "format":       "json",
                "generator":    "search",
                "gsrnamespace": 6,
                "gsrsearch":    f"{family_name} satellite",
                "gsrlimit":     30,
                "prop":         "imageinfo",
                "iiprop":       "url|mime|thumburl|extmetadata",
                "iiurlwidth":   1200,
            })
            if resp.text.strip():
                return resp.json()
            print(f"  Empty response (attempt {attempt}/3), re-logging in and waiting 30s...")
            login(session)
            time.sleep(30)
        except Exception as e:
            print(f"  Search error (attempt {attempt}/3): {e}, re-logging in and waiting 30s...")
            login(session)
            time.sleep(30)
    return {}

def get_search_term(family_name):
    """
    Extract the core searchable term from a family name.
    Strips parentheses, slashes and extra identifiers.
    
    Examples:
        '3CAT-5/A (TYVAK-0161)' -> '3cat'
        'GPS IIR'               -> 'gps iir'
        'STARLINK'              -> 'starlink'
    """
    # Remove anything in parentheses
    name = re.sub(r'\(.*?\)', '', family_name)
    # Remove slashes and what follows
    name = name.split('/')[0]
    # Strip and lowercase
    return name.strip().lower()

def filter_images(pages, family_name):

    # Trimmed to genuinely generic noise terms.
    # Removed location-specific words (cervinia, gonnet, poland, tubingen, rathaus, über, ueber)
    # and ambiguous terms (venus, tiangong, orbit, sky, night, over, overhead, city, town, village,
    # seen from, imaged from, annotated, annotation, planes, sum) that were causing false negatives.
    noise_words = [
        "logo", "patch", "emblem", "flag", "icon",
        "chart", "graph", "diagram", "map",
        "statistics", "stats",
        "streak", "trail",
    ]

    space_keywords = [
        "satellite", "spacecraft", "space", "orbit", "launch",
        "rocket", "mission", "nasa", "esa", "jaxa", "payload",
        "telescope", "probe", "station", "module", "capsule",
        "cubesat", "nanosat", "relay", "comsat", "spacex",
        "aerial", "astro", "cosmo", "cosmos", "geo", "leo",
        "meo", "solar", "lunar", "interplanetary", "constellation",
        "deployment", "fairing", "booster", "propulsion", "thruster",
        "antenna", "transponder", "imaging", "remote sensing",
        "earth observation", "communications", "navigation",
        "gps", "galileo", "glonass", "beidou", "iridium",
        "intelsat", "boeing", "airbus", "maxar", "lockheed",
        "northrop", "ball aerospace", "thales", "isro", "roscosmos",
    ]

    search_term = get_search_term(family_name)

    results = []
    for page in pages.values():
        title     = page.get("title", "").lower()
        imageinfo = page.get("imageinfo", [{}])[0]
        mime      = imageinfo.get("mime", "")
        url       = imageinfo.get("url", "")

        if "svg" in mime:
            continue
        if not mime.startswith("image/"):
            continue
        if any(word in title for word in noise_words):
            continue

        # Use extracted search term instead of full family name
        # Fall back to space keywords if search term not in title
        if search_term not in title and not any(keyword in title for keyword in space_keywords):
            continue

        meta = imageinfo.get("extmetadata", {})
        results.append({
            "title":       page.get("title"),
            "url":         imageinfo.get("thumburl") or url,
            "full_url":    url,
            "mime":        mime,
            "description": meta.get("ImageDescription", {}).get("value", ""),
            "author":      meta.get("Artist", {}).get("value", ""),
            "license":     meta.get("LicenseShortName", {}).get("value", ""),
            "date":        meta.get("DateTimeOriginal", {}).get("value", "") or meta.get("DateTime", {}).get("value", ""),
        })

    return results

def download_image(session, url, dest_path):
    """
    Download a single image to dest_path.
    Retries up to 3 times if rate limited.

    Backoff is 60s / 120s / 180s because Wikimedia's upload.wikimedia.org CDN
    throttle windows operate on the scale of minutes, not seconds. The previous
    15/30/45s schedule always failed because it never waited long enough for
    the throttle window to reset.

    Returns True on success, False on failure.
    """
    for attempt in range(1, 4):  # try up to 3 times
        resp = session.get(url, stream=True, timeout=20)

        if resp.status_code == 200:
            # Make sure the destination folder exists
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True

        elif resp.status_code == 429:
            wait = 60 * attempt  # 60s, 120s, 180s
            print(f"    Rate limited, waiting {wait}s (attempt {attempt}/3)...")
            time.sleep(wait)

        else:
            print(f"    Failed — HTTP {resp.status_code}")
            return False

    print(f"    Giving up after 3 attempts")
    return False


def has_wikimedia_images(dest_dir):
    """
    Return True if dest_dir already contains images downloaded by THIS source.
    We tag wikimedia files with SOURCE_TAG in the filename so we can ignore
    files dropped here by other sources (e.g. NASA scraper).
    """
    if not dest_dir.exists():
        return False
    return any(SOURCE_TAG in p.name for p in dest_dir.iterdir() if p.is_file())


if __name__ == "__main__":
    families = set()
    with open("satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)

    families = sorted(families)
    print(f"Found {len(families)} unique satellite families")

    # Load both skip sets once before the loop
    no_images_log = Path("no_images_found.txt")
    already_tried_no_images = set()
    if no_images_log.exists():
        already_tried_no_images = set(no_images_log.read_text().splitlines())

    session = make_session()
    if login(session):
        for i, family in enumerate(families, start=1):
            print(f"\n[{i}/{len(families)}] {family}")

            dest_dir  = Path("satellite_images") / family
            meta_path = dest_dir / "metadata_wikimedia.txt"

            if has_wikimedia_images(dest_dir):
                if meta_path.exists():
                    print(f"  Skipping — already downloaded from Wikimedia")
                    continue
                # Images exist but no metadata — backfill only
                print(f"  Backfilling metadata (images already present)...")
                raw    = search_images(session, family)
                pages  = raw.get("query", {}).get("pages", {})
                images = filter_images(pages, family)
                time.sleep(1)
                existing_files = sorted(p for p in dest_dir.iterdir() if SOURCE_TAG in p.name and p.is_file())
                for img, dest in zip(images, existing_files):
                    desc = re.sub(r'<[^>]+>', '', img['description']).strip()
                    with open(meta_path, "a", encoding="utf-8") as mf:
                        mf.write(f"file:        {dest.name}\n")
                        mf.write(f"title:       {img['title']}\n")
                        mf.write(f"source_url:  {img['full_url']}\n")
                        mf.write(f"mime:        {img['mime']}\n")
                        mf.write(f"date:        {img['date']}\n")
                        mf.write(f"author:      {img['author']}\n")
                        mf.write(f"license:     {img['license']}\n")
                        mf.write(f"description: {desc}\n")
                        mf.write("\n")
                print(f"  Wrote metadata for {len(existing_files)} image(s)")
                time.sleep(1)
                continue

            if family in already_tried_no_images:
                print(f"  Skipping — no images found last time")
                continue

            raw    = search_images(session, family)
            pages  = raw.get("query", {}).get("pages", {})
            images = filter_images(pages, family)
            print(f"  Found {len(images)} images after filtering")

            # Small pause between API searches to stay polite to the API
            # (separate from the CDN rate limit on downloads).
            time.sleep(1)

            if not images:
                with open("no_images_found.txt", "a") as f:
                    f.write(family + "\n")
                already_tried_no_images.add(family)
                continue

            consecutive_failures = 0
            MAX_CONSECUTIVE_FAILURES = 3

            meta_path = dest_dir / "metadata_wikimedia.txt"
            for j, img in enumerate(images, start=1):
                ext     = Path(img["url"].split("?")[0]).suffix.lower() or ".jpg"
                dest    = dest_dir / f"{family}_{SOURCE_TAG}_{j:02d}{ext}"
                success = download_image(session, img["url"], dest)
                status  = "✓" if success else "✗"
                print(f"  {status} {img['title'][:50]}")

                if success:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    with open(meta_path, "a", encoding="utf-8") as mf:
                        mf.write(f"file:        {dest.name}\n")
                        mf.write(f"title:       {img['title']}\n")
                        mf.write(f"source_url:  {img['full_url']}\n")
                        mf.write(f"mime:        {img['mime']}\n")
                        mf.write(f"date:        {img['date']}\n")
                        mf.write(f"author:      {img['author']}\n")
                        mf.write(f"license:     {img['license']}\n")
                        desc = re.sub(r'<[^>]+>', '', img['description']).strip()
                        mf.write(f"description: {desc}\n")
                        mf.write("\n")

                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"  Too many failures in a row — pausing 120s...")
                        time.sleep(120)
                        consecutive_failures = 0

                time.sleep(2)

            time.sleep(3)

        # Write families with no Wikimedia images to a review file
        needs_review = []
        for family in families:
            dest_dir = Path("satellite_images") / family
            if not has_wikimedia_images(dest_dir):
                needs_review.append(family)

        if needs_review:
            with open("needs_review.txt", "w") as f:
                f.write("\n".join(needs_review))
            print(f"\n⚠ {len(needs_review)} families with no Wikimedia images → needs_review.txt")