# sources/wikipedia.py
# Fetches images embedded in a satellite family's Wikipedia article.
#
# This complements sources/wikimedia.py: the Commons full-text search misses
# images whose filenames/descriptions don't mention the family, but the
# images actually embedded in the family's Wikipedia article are curated and
# relevant by construction. Images already downloaded by the wikimedia source
# (same upload.wikimedia.org URL recorded in metadata_wikimedia.txt) are
# skipped to avoid duplicates.
#
# No login needed — the read API is public; a descriptive User-Agent is
# required per Wikimedia policy.

import requests
import time
import csv
import re
from pathlib import Path

API_URL = "https://en.wikipedia.org/w/api.php"

# Source tag — prefixed to every file this script downloads.
SOURCE_TAG = "wikipedia"

NOISE_WORDS = [
    "logo", "patch", "emblem", "flag", "icon",
    "chart", "graph", "diagram", "map",
    "statistics", "stats", "streak", "trail",
    "commons-", "wiki", "edit-", "ambox", "question_book",
    "symbol", "padlock", "stub",
]


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31"
    })
    return session


def get_search_term(family_name):
    """Extract the core searchable term from a family name."""
    name = re.sub(r'\(.*?\)', '', family_name)
    name = name.split('/')[0]
    return name.strip().lower()


# A candidate article must be verifiably space-related, otherwise family
# names that are also common words pick up the wrong article (e.g. LEMUR —
# Spire's cubesats — matched "Gray-headed lemur"). Snippets are not reliable
# (an animal article can mention "satellite imagery"), so we check the
# article's categories, which are decisive.
SPACE_HINTS = [
    "satellite", "spacecraft", "cubesat", "nanosat", "smallsat",
    "space", "orbit", "launch", "rocket", "constellation",
    "nasa", "esa", "payload", "telescope", "probe",
]

# Titles containing these are accepted without a category lookup.
SPACE_TITLE_HINTS = ["satellite", "spacecraft", "cubesat", "constellation", "space"]


def _title_is_space_related(title):
    return any(word in title.lower() for word in SPACE_TITLE_HINTS)


def _categories_are_space_related(session, title):
    """True if any of the article's categories contains a space keyword."""
    try:
        resp = session.get(API_URL, params={
            "action":  "query",
            "format":  "json",
            "titles":  title,
            "prop":    "categories",
            "cllimit": 50,
        }, timeout=30)
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            for cat in page.get("categories", []):
                cat_name = cat.get("title", "").lower()
                # "Satellite television" categories are about broadcasting,
                # not spacecraft — don't let them count as space-related.
                if "television" in cat_name or "broadcast" in cat_name:
                    continue
                if any(word in cat_name for word in SPACE_HINTS):
                    return True
    except Exception as e:
        print(f"  Category check error for '{title}': {e}")
    return False


def find_article(session, family_name):
    """Find the best-matching space-related Wikipedia article for a family.

    Candidates are checked in search-rank order; each must either have a
    space keyword in its title or a space-related category. Returns the
    article title string, or None if nothing qualifies.
    """
    term = get_search_term(family_name)
    hits = []
    for attempt in range(1, 4):
        try:
            resp = session.get(API_URL, params={
                "action":   "query",
                "format":   "json",
                "list":     "search",
                "srsearch": f"{term} satellite",
                "srlimit":  5,
            }, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                hits = resp.json().get("query", {}).get("search", [])
                break
            print(f"  Article search failed: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  Article search error (attempt {attempt}/3): {e}")
        time.sleep(15)

    for hit in hits:
        title = hit.get("title", "")
        if "(disambiguation)" in title.lower():
            continue
        if _title_is_space_related(title):
            return title
        if _categories_are_space_related(session, title):
            return title
        time.sleep(0.5)

    return None


def search_images(session, family_name):
    """Fetch imageinfo for every image embedded in the family's article.

    Returns the raw pages dict ({} on failure or no article).
    """
    title = find_article(session, family_name)
    if not title:
        return {}
    print(f"  Article: {title}")
    time.sleep(1)

    for attempt in range(1, 4):
        try:
            resp = session.get(API_URL, params={
                "action":       "query",
                "format":       "json",
                "generator":    "images",
                "titles":       title,
                "gimlimit":     50,
                "prop":         "imageinfo",
                "iiprop":       "url|mime|extmetadata",
                "iiurlwidth":   1200,
            }, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                return resp.json().get("query", {}).get("pages", {})
            print(f"  Image list failed: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  Image list error (attempt {attempt}/3): {e}")
        time.sleep(15)
    return {}


def _already_downloaded_urls(dest_dir):
    """Collect source_url values from every metadata_*.txt already in the
    family folder, so we don't re-download an image another source grabbed."""
    urls = set()
    if not dest_dir.exists():
        return urls
    for meta in dest_dir.glob("metadata_*.txt"):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("source_url:"):
                urls.add(line.split(":", 1)[1].strip())
    return urls


def filter_images(pages, family_name, dest_dir=None):
    """Keep relevant photos, dropping SVG icons, noise images, and anything
    already downloaded from Wikimedia Commons by another source."""
    seen_urls = _already_downloaded_urls(dest_dir) if dest_dir else set()

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
        if any(word in title for word in NOISE_WORDS):
            continue
        if url in seen_urls:
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
    """Download a single image to dest_path. Returns True on success.

    Backoff is 60s/120s/180s — upload.wikimedia.org throttle windows operate
    on the scale of minutes (see sources/wikimedia.py).
    """
    for attempt in range(1, 4):
        try:
            resp = session.get(url, stream=True, timeout=20)
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

    no_images_log = Path("no_images_found_wikipedia.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_wikipedia.txt"

        if has_source_images(dest_dir):
            print("  Skipping — already downloaded from Wikipedia")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        pages  = search_images(session, family)
        images = filter_images(pages, family, dest_dir)
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
        with open("needs_review_wikipedia.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no Wikipedia images → needs_review_wikipedia.txt")
