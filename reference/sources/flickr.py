# sources/flickr.py
# Fetches satellite images from Flickr (https://www.flickr.com).
#
# Flickr hosts official photostreams (NASA HQ, ESA, SpaceX, Rocket Lab, ...)
# with high-quality spacecraft photos that never make it to Wikimedia.
#
# REQUIRES AN API KEY (free, ~5 minutes):
#   1. Sign in at https://www.flickr.com/services/apps/create/apply
#   2. Request a non-commercial key
#   3. Either set the environment variable FLICKR_API_KEY, or paste the key
#      into a file named  .flickr_api_key  in the project root.
# The key is read at startup; nothing runs without it.

import requests
import time
import csv
import os
import re
from pathlib import Path

REST_URL = "https://www.flickr.com/services/rest/"

# Source tag — prefixed to every file this script downloads.
SOURCE_TAG = "flickr"

MAX_IMAGES_PER_FAMILY = 30

NOISE_WORDS = [
    "logo", "patch", "emblem", "flag", "icon",
    "chart", "graph", "diagram", "map",
    "statistics", "stats", "streak", "trail",
]

# Same relevance guard as sources/wikimedia.py: keep a photo if the search
# term appears in the title, or the title contains a space keyword.
SPACE_KEYWORDS = [
    "satellite", "spacecraft", "space", "orbit", "launch",
    "rocket", "mission", "nasa", "esa", "jaxa", "payload",
    "telescope", "probe", "cubesat", "nanosat", "constellation",
    "fairing", "cleanroom", "clean room", "antenna", "solar array",
]

# Flickr license IDs -> human-readable names
LICENSES = {
    "0": "All Rights Reserved", "1": "CC BY-NC-SA 2.0", "2": "CC BY-NC 2.0",
    "3": "CC BY-NC-ND 2.0", "4": "CC BY 2.0", "5": "CC BY-SA 2.0",
    "6": "CC BY-ND 2.0", "7": "No known copyright", "8": "US Government Work",
    "9": "CC0", "10": "Public Domain Mark",
}


def load_api_key():
    """Read the Flickr API key from the environment or .flickr_api_key file."""
    key = os.environ.get("FLICKR_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(".flickr_api_key")
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31",
    })
    return session


def get_search_term(family_name):
    """Extract the core searchable term from a family name."""
    name = re.sub(r'\(.*?\)', '', family_name)
    name = name.split('/')[0]
    return name.strip().lower()


def search_images(session, family_name, api_key):
    """Search Flickr for *family_name*. Returns the raw photo list ([] on failure)."""
    term = get_search_term(family_name)
    for attempt in range(1, 4):
        try:
            resp = session.get(REST_URL, params={
                "method":         "flickr.photos.search",
                "api_key":        api_key,
                "text":           f"{term} satellite",
                "media":          "photos",
                "content_types":  "0",           # photos only, no screenshots/art
                "sort":           "relevance",
                "safe_search":    "1",
                "per_page":       "50",
                "extras":         "url_l,url_c,url_o,license,ownername,description",
                "format":         "json",
                "nojsoncallback": "1",
            }, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                if data.get("stat") != "ok":
                    print(f"  Flickr API error: {data.get('message', data)}")
                    return []
                return data.get("photos", {}).get("photo", [])
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/3)...")
                time.sleep(wait)
                continue
            print(f"  Search failed: HTTP {resp.status_code}")
            return []
        except Exception as e:
            print(f"  Search error (attempt {attempt}/3): {e}")
            time.sleep(30)
    return []


def _photo_url(photo):
    """Best available image URL: large -> medium-800 -> original -> constructed."""
    for key in ("url_l", "url_c", "url_o"):
        if photo.get(key):
            return photo[key]
    server, pid, secret = photo.get("server"), photo.get("id"), photo.get("secret")
    if server and pid and secret:
        return f"https://live.staticflickr.com/{server}/{pid}_{secret}_b.jpg"
    return ""


def filter_images(photos, family_name):
    """Keep relevant photos; returns image dicts like the other sources."""
    term = get_search_term(family_name)

    results = []
    for photo in photos:
        title = (photo.get("title") or "").lower()
        url   = _photo_url(photo)
        if not url:
            continue
        if any(word in title for word in NOISE_WORDS):
            continue
        if term not in title and not any(kw in title for kw in SPACE_KEYWORDS):
            continue

        ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"
        desc = photo.get("description", {})
        results.append({
            "title":       photo.get("title", ""),
            "url":         url,
            "full_url":    f"https://www.flickr.com/photos/{photo.get('owner')}/{photo.get('id')}",
            "mime":        f"image/{ext}",
            "author":      photo.get("ownername", ""),
            "license":     LICENSES.get(str(photo.get("license", "")), str(photo.get("license", ""))),
            "description": desc.get("_content", "") if isinstance(desc, dict) else "",
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
    api_key = load_api_key()
    if not api_key:
        print("No Flickr API key found.")
        print("Get a free key at https://www.flickr.com/services/apps/create/apply")
        print("then set FLICKR_API_KEY or create a .flickr_api_key file.")
        raise SystemExit(1)

    families = set()
    with open("satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)

    families = sorted(families)
    print(f"Found {len(families)} unique satellite families")

    no_images_log = Path("no_images_found_flickr.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_flickr.txt"

        if has_source_images(dest_dir):
            print("  Skipping — already downloaded from Flickr")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        photos = search_images(session, family, api_key)
        images = filter_images(photos, family)
        print(f"  Found {len(images)} images after filtering")

        time.sleep(1)  # Flickr free tier: 3600 requests/hour — stay well under

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
                    mf.write(f"author:      {img['author']}\n")
                    mf.write(f"license:     {img['license']}\n")
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

        time.sleep(1)

    needs_review = [
        f for f in families
        if not has_source_images(Path("satellite_images") / f)
    ]
    if needs_review:
        with open("needs_review_flickr.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no Flickr images → needs_review_flickr.txt")
