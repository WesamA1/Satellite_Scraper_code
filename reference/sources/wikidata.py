# sources/wikidata.py
# Fetches satellite images from Wikidata (https://www.wikidata.org).
#
# Wikidata items for satellites carry a NORAD catalog number (property P377)
# and often an image (P18). One SPARQL query returns every such pairing
# (~2,000 satellites), which we cache and match against our own NORAD IDs —
# exact matches, no keyword guessing, same strategy as sources/satnogs.py.
#
# Image URLs are Commons Special:FilePath links, so files may overlap with
# what sources/wikimedia.py or sources/wikipedia.py already downloaded; we
# dedupe by comparing filenames against every metadata_*.txt in the family
# folder before downloading.

import requests
import json
import time
import csv
from pathlib import Path
from urllib.parse import unquote

SPARQL_URL      = "https://query.wikidata.org/sparql"
SPARQL_QUERY    = """
SELECT ?norad ?image WHERE {
  ?sat wdt:P377 ?norad ;
       wdt:P18  ?image .
}
"""
CATALOG_CACHE   = Path("wikidata_catalog_cache.json")
CATALOG_MAX_AGE = 86400  # re-download after 24 hours

SATELLITES_CSV = "satellites.csv"

# Source tag — prefixed to every file this script downloads.
SOURCE_TAG = "wikidata"

MAX_IMAGES_PER_FAMILY = 30


def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31",
    })
    return session


# ── Catalog cache ─────────────────────────────────────────────────────────────

def _load_catalog(session):
    """Return {norad_id(int): [image_url, ...]}, using a local cache when fresh."""
    if CATALOG_CACHE.exists():
        age = time.time() - CATALOG_CACHE.stat().st_mtime
        if age < CATALOG_MAX_AGE:
            return {int(k): v for k, v in
                    json.loads(CATALOG_CACHE.read_text(encoding="utf-8")).items()}

    print("  Querying Wikidata SPARQL (cached for 24 h)...")
    for attempt in range(1, 4):
        try:
            resp = session.get(SPARQL_URL, params={
                "query":  SPARQL_QUERY,
                "format": "json",
            }, headers={"Accept": "application/sparql-results+json"}, timeout=120)
            if resp.status_code == 200:
                by_norad = {}
                for row in resp.json()["results"]["bindings"]:
                    try:
                        norad = int(row["norad"]["value"])
                    except ValueError:
                        continue
                    url = row["image"]["value"].replace("http://", "https://")
                    by_norad.setdefault(norad, []).append(url)
                CATALOG_CACHE.write_text(
                    json.dumps({str(k): v for k, v in by_norad.items()}),
                    encoding="utf-8")
                return by_norad
            if resp.status_code == 429:
                wait = 60 * attempt
                print(f"  Rate limited, waiting {wait}s (attempt {attempt}/3)...")
                time.sleep(wait)
            else:
                print(f"  SPARQL query failed: HTTP {resp.status_code}")
                return {}
        except Exception as e:
            print(f"  SPARQL query error: {e}")
            time.sleep(30)
    return {}


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


def _image_basename(url):
    """Normalized filename for dedupe (works for both Special:FilePath and
    upload.wikimedia.org URLs)."""
    return unquote(url.split("/")[-1]).replace("_", " ").lower()


def _already_downloaded_basenames(dest_dir):
    """Filenames recorded in every metadata_*.txt already in the family folder,
    so we skip images another Commons-backed source already fetched."""
    names = set()
    if not dest_dir.exists():
        return names
    for meta in dest_dir.glob("metadata_*.txt"):
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("source_url:"):
                names.add(_image_basename(line.split(":", 1)[1].strip()))
    return names


# ── Public interface (mirrors satnogs.py) ─────────────────────────────────────

_BY_NORAD   = None
_FAMILY_MAP = None


def search_images(session, family_name):
    """Find Wikidata images for every member satellite of *family_name*.

    Returns a list of image dicts:
      { "url", "title", "full_url", "mime", "norad" }
    """
    global _BY_NORAD, _FAMILY_MAP
    if _BY_NORAD is None:
        _BY_NORAD = _load_catalog(session)
    if _FAMILY_MAP is None:
        _FAMILY_MAP = _family_norad_map()

    results = []
    seen = set()
    for norad in _FAMILY_MAP.get(family_name, []):
        for url in _BY_NORAD.get(norad, []):
            basename = _image_basename(url)
            if basename in seen:
                continue  # sister satellites often share one image
            seen.add(basename)
            ext = Path(unquote(url.split("/")[-1])).suffix.lstrip(".").lower() or "jpeg"
            results.append({
                "title":    unquote(url.split("/")[-1]),
                "url":      url,
                "full_url": url,
                "mime":     f"image/{ext}",
                "norad":    norad,
            })
            if len(results) >= MAX_IMAGES_PER_FAMILY:
                return results
    return results


def filter_images(images, family_name, dest_dir=None):
    """Drop SVGs/non-images and anything another source already downloaded."""
    seen = _already_downloaded_basenames(dest_dir) if dest_dir else set()
    return [
        img for img in images
        if img["mime"].startswith("image/")
        and "svg" not in img["mime"]
        and _image_basename(img["url"]) not in seen
    ]


def download_image(session, url, dest_path):
    """Download a single image to dest_path. Returns True on success.

    Special:FilePath URLs redirect to upload.wikimedia.org; backoff matches
    sources/wikimedia.py since it's the same CDN.
    """
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

    no_images_log = Path("no_images_found_wikidata.txt")
    already_tried = set()
    if no_images_log.exists():
        already_tried = set(no_images_log.read_text().splitlines())

    session = make_session()

    _BY_NORAD   = _load_catalog(session)
    _FAMILY_MAP = _family_norad_map()
    print(f"Wikidata catalog loaded: {len(_BY_NORAD)} satellites with images")

    for i, family in enumerate(families, start=1):
        print(f"\n[{i}/{len(families)}] {family}")

        dest_dir  = Path("satellite_images") / family
        meta_path = dest_dir / "metadata_wikidata.txt"

        if has_source_images(dest_dir):
            print("  Skipping — already downloaded from Wikidata")
            continue

        if family in already_tried:
            print("  Skipping — no images found last time")
            continue

        images = filter_images(search_images(session, family), family, dest_dir)
        print(f"  Found {len(images)} images")

        if not images:
            with open(no_images_log, "a") as f:
                f.write(family + "\n")
            already_tried.add(family)
            continue

        consecutive_failures = 0
        for j, img in enumerate(images, start=1):
            ext  = Path(unquote(img["url"].split("/")[-1])).suffix.lower() or ".jpg"
            dest = dest_dir / f"{family}_{SOURCE_TAG}_{j:02d}{ext}"
            ok   = download_image(session, img["url"], dest)
            print(f"  {'✓' if ok else '✗'} {img['title'][:50]}")

            if ok:
                dest_dir.mkdir(parents=True, exist_ok=True)
                with open(meta_path, "a", encoding="utf-8") as mf:
                    mf.write(f"file:       {dest.name}\n")
                    mf.write(f"title:      {img['title']}\n")
                    mf.write(f"source_url: {img['full_url']}\n")
                    mf.write(f"mime:       {img['mime']}\n")
                    mf.write(f"norad_id:   {img['norad']}\n")
                    mf.write(f"license:    see Commons file page\n")
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
        with open("needs_review_wikidata.txt", "w") as f:
            f.write("\n".join(needs_review))
        print(f"\n{len(needs_review)} families with no Wikidata images → needs_review_wikidata.txt")
