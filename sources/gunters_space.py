# sources/gunters_space.py
# Fetches images for a satellite family from Gunter's Space Page
# (https://space.skyrocket.de)
#
# Strategy: the site's sitemap.xml lists every /img_sat/ URL directly.
# We download and cache it once, then for each family name we search for
# matching image URLs by keyword rather than trying to guess page slugs.
# (Individual /doc_sdat/ pages only carry one image each, and slug guessing
# is fragile for multi-version families like Starlink v0.9/v1.0/v1.5/v2.)

import requests
import time
import re
import csv
from pathlib import Path
from urllib.parse import urlparse


BASE_URL      = "https://space.skyrocket.de"
SITEMAP_URL   = BASE_URL + "/sitemap.xml"
SITEMAP_CACHE = Path("gunters_sitemap_cache.xml")
SITEMAP_MAX_AGE = 86400  # re-download after 24 hours

NOISE_WORDS = [
    "logo", "patch", "emblem", "flag", "icon",
    "chart", "graph", "diagram", "map",
    "statistics", "stats", "streak", "trail",
]

# Space-Track family names that Gunter's uses under a different slug.
# Values are checked with word-boundary matching (start of slug or after - / _).
# Use a trailing "-" to force prefix matching (e.g. "fy-" matches "fy-1", "fy-3a").
GUNTERS_ALIASES = {
    "GLONASS":  ["uragan"],
    "BEIDOU":   ["compass"],
    "FENGYUN":  ["fy-"],
    "HAIYANG":  ["hy-"],
    "SHIJIAN":  ["sj-"],
    "NAVSTAR":  ["navstar"],   # explicit: GPS III blocks use this slug
}


# ── Session ───────────────────────────────────────────────────────────────────

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 requests/2.31",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


# ── Sitemap cache ─────────────────────────────────────────────────────────────

def _load_sitemap(session):
    """Return sitemap XML text, using a local file cache when fresh enough."""
    if SITEMAP_CACHE.exists():
        age = time.time() - SITEMAP_CACHE.stat().st_mtime
        if age < SITEMAP_MAX_AGE:
            return SITEMAP_CACHE.read_text(encoding="utf-8", errors="replace")

    print("  Downloading Gunter's sitemap (cached for 24 h)...")
    for attempt in range(1, 4):
        try:
            resp = session.get(SITEMAP_URL, timeout=60)
            if resp.status_code == 200:
                SITEMAP_CACHE.write_bytes(resp.content)
                return resp.text
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/3)...")
                time.sleep(wait)
            else:
                print(f"  Sitemap fetch failed: HTTP {resp.status_code}")
                return ""
        except Exception as e:
            print(f"  Sitemap fetch error: {e}")
            time.sleep(30)
    return ""


def _all_img_sat_urls(sitemap_text):
    """Extract every /img_sat/ URL from the sitemap XML."""
    return re.findall(
        r'<loc>(https://space\.skyrocket\.de/img_sat/[^<]+)</loc>',
        sitemap_text,
    )


# ── Keyword extraction ────────────────────────────────────────────────────────

def _slug_matches(keyword, slug):
    """
    True if *keyword* matches *slug* at a word boundary.

    Keywords ending in '-' or '_' use plain substring matching (they are
    already specific enough: "fy-" only hits "fy-1", "fy-3a", etc.).
    All other keywords must appear at the start of the slug or immediately
    after a '-' or '_', preventing e.g. "sentinel" from matching "biosentinel".
    """
    if keyword.endswith(('-', '_')):
        return keyword in slug
    return bool(re.search(r'(?:^|[-_])' + re.escape(keyword), slug))


def _search_keywords(family_name):
    """
    Return lowercase keyword variants to match against sitemap image slugs.

    Checks GUNTERS_ALIASES first (e.g. GLONASS → uragan).
    Falls back to variants derived from the family name itself.

    Examples:
        'STARLINK'              -> ['starlink']
        'GPS IIR'               -> ['gps-iir', 'gps_iir', 'gpsiir', 'gps']
        'GLONASS'               -> ['uragan']          (via alias)
        'COSMOS 2403 (GLONASS)' -> ['cosmos-2403', 'cosmos_2403', 'cosmos2403', 'cosmos']
        '3CAT-5/A (TYVAK-0161)' -> ['3cat-5', '3cat_5', '3cat5', '3cat']
    """
    upper = family_name.strip().upper().split('(')[0].split('/')[0].strip()
    if upper in GUNTERS_ALIASES:
        return GUNTERS_ALIASES[upper]

    name = re.sub(r'\s*\(.*?\)\s*', ' ', family_name).strip()
    name = name.split('/')[0].strip().lower()
    words = name.split()
    if not words:
        return []

    keywords = []
    if len(words) >= 2:
        base = " ".join(words)
        keywords.append(re.sub(r'\s+', '-', base))
        keywords.append(re.sub(r'\s+', '_', base))
        keywords.append(re.sub(r'\s+', '',  base))

    first = words[0]
    if first not in keywords:
        keywords.append(first)

    return keywords


# ── Public interface (mirrors wikimedia.py) ───────────────────────────────────

def search_images(session, family_name):
    """
    Find satellite images for *family_name* on Gunter's Space Page.

    Returns a list of image dicts:
      { "url": str, "title": str, "full_url": str, "mime": str }

    The sitemap is downloaded once and cached for 24 hours.
    """
    sitemap  = _load_sitemap(session)
    all_urls = _all_img_sat_urls(sitemap)
    keywords = _search_keywords(family_name)

    if not keywords or not all_urls:
        return []

    results = []
    seen    = set()

    for url in all_urls:
        slug = Path(urlparse(url).path).name.lower()
        if any(_slug_matches(kw, slug) for kw in keywords) and url not in seen:
            seen.add(url)
            ext = Path(urlparse(url).path).suffix.lstrip(".").lower() or "jpeg"
            results.append({
                "title":    Path(urlparse(url).path).name,
                "url":      url,
                "full_url": url,
                "mime":     f"image/{ext}",
            })

    return results


def filter_images(images, family_name):
    """
    Remove noise images (logos, diagrams, etc.) from search_images results.
    Sitemap entries are already curated, so this is mostly a safety net.
    """
    results = []
    for img in images:
        fname = img.get("title", "").lower()
        mime  = img.get("mime", "")
        if "svg" in mime:
            continue
        if not mime.startswith("image/"):
            continue
        if any(word in fname for word in NOISE_WORDS):
            continue
        results.append(img)
    return results


def download_image(session, url, dest_path):
    """
    Download a single image to dest_path.
    Returns True on success, False on failure.
    Mirrors wikimedia.py's download_image interface.
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


# ── Main (standalone run) ─────────────────────────────────────────────────────

if __name__ == "__main__":
    families = set()
    with open("satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)

    families = sorted(families)
    print(f"Found {len(families)} unique satellite families")

    no_images_log = Path("no_images_found_gunters.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    # Pre-load the sitemap once for the whole run
    sitemap  = _load_sitemap(session)
    all_urls = _all_img_sat_urls(sitemap)
    print(f"Sitemap loaded: {len(all_urls)} /img_sat/ entries")

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_gunters.txt"

        if dest_dir.exists() and any(dest_dir.iterdir()):
            if meta_path.exists():
                print("  Skipping — already downloaded")
                continue
            # Images exist but no metadata — backfill from sitemap (no extra requests)
            print("  Backfilling metadata (images already present)...")
            raw_images = search_images(session, family)
            images     = filter_images(raw_images, family)
            existing_files = sorted(
                p for p in dest_dir.iterdir()
                if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
            )
            for img, dest in zip(images, existing_files):
                with open(meta_path, "a", encoding="utf-8") as mf:
                    mf.write(f"file:       {dest.name}\n")
                    mf.write(f"title:      {img['title']}\n")
                    mf.write(f"source_url: {img['full_url']}\n")
                    mf.write(f"mime:       {img['mime']}\n")
                    mf.write("\n")
            print(f"  Wrote metadata for {len(existing_files)} image(s)")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        raw_images = search_images(session, family)
        images     = filter_images(raw_images, family)
        print(f"  Found {len(images)} images after filtering")

        if not images:
            with open(no_images_log, "a") as f:
                f.write(family + "\n")
            already_tried.add(family)
            continue

        consecutive_failures = 0
        meta_path = dest_dir / "metadata_gunters.txt"
        for j, img in enumerate(images, start=1):
            ext  = Path(img["url"].split("?")[0]).suffix.lower() or ".jpg"
            dest = dest_dir / f"{family}_{j:02d}{ext}"
            ok   = download_image(session, img["url"], dest)
            print(f"  {'✓' if ok else '✗'} {img['title'][:50]}")

            if ok:
                dest_dir.mkdir(parents=True, exist_ok=True)
                with open(meta_path, "a", encoding="utf-8") as mf:
                    mf.write(f"file:       {dest.name}\n")
                    mf.write(f"title:      {img['title']}\n")
                    mf.write(f"source_url: {img['full_url']}\n")
                    mf.write(f"mime:       {img['mime']}\n")
                    mf.write("\n")

            if ok:
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
        if not (Path("satellite_images") / f).exists()
        or not any((Path("satellite_images") / f).iterdir())
    ]
    if needs_review:
        with open("needs_review_gunters.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no images → needs_review_gunters.txt")
