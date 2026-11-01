"""Gunter's Space Page source: https://space.skyrocket.de

The sitemap-based family matching below is ported from the old top-level
sources/gunters_space.py almost unchanged -- the keyword/alias logic took
real debugging (see GUNTERS_ALIASES) and multi-generation family names like
Starlink v1.0/v1.5/v2 are exactly what naive slug-guessing gets wrong.

This site does not provide machine-readable per-image license data. Its
records are marked as unverified and explicitly allowed by the shared license
gate; this marker is not a claim that the images are licensed.
"""

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from scraper import rate_limiter
from scraper.sources.base import UNVERIFIED_LICENSE, ImageRecord, stream_to_file

ENABLED = False

SOURCE_MODULE = "gunters_space"

BASE_URL = "https://space.skyrocket.de"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
SITEMAP_CACHE = Path("gunters_sitemap_cache.xml")
SITEMAP_MAX_AGE = 86400  # re-download after 24 hours

NOISE_WORDS = [
    "logo",
    "patch",
    "emblem",
    "flag",
    "icon",
    "chart",
    "graph",
    "diagram",
    "map",
    "statistics",
    "stats",
    "streak",
    "trail",
]

# Space-Track family names that Gunter's site uses under a different slug.
# Word-boundary matched; a trailing "-" forces prefix matching (e.g. "fy-"
# matches "fy-1", "fy-3a" without also matching unrelated "fy" words).
GUNTERS_ALIASES = {
    "GLONASS": ["uragan"],
    "BEIDOU": ["compass"],
    "FENGYUN": ["fy-"],
    "HAIYANG": ["hy-"],
    "SHIJIAN": ["sj-"],
    "NAVSTAR": ["navstar"],
}


def _require_enabled() -> None:
    if not ENABLED:
        raise RuntimeError("gunters_space is disabled (see this module's ENABLED constant).")


def make_session(dev: bool = False) -> requests.Session:
    _require_enabled()
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 requests/2.31"})
    return session


def _load_sitemap(session: requests.Session, conn, sleep_fn=time.sleep) -> str:
    """Sitemap XML text, using a local file cache when fresh enough -- the
    whole sitemap is a few thousand URLs and doesn't change intraday, so
    re-fetching it per family would be impolite for no benefit."""
    if SITEMAP_CACHE.exists() and time.time() - SITEMAP_CACHE.stat().st_mtime < SITEMAP_MAX_AGE:
        return SITEMAP_CACHE.read_text(encoding="utf-8", errors="replace")

    host = rate_limiter.host_of(SITEMAP_URL)
    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        resp = session.get(SITEMAP_URL, timeout=60)
        if resp.status_code == 200:
            SITEMAP_CACHE.write_bytes(resp.content)
            return resp.text
        if resp.status_code == 429:
            rate_limiter.record_cooldown(conn, host, 60 * attempt)
            continue
        return ""
    return ""


def _all_img_sat_urls(sitemap_text: str) -> list[str]:
    return re.findall(r"<loc>(https://space\.skyrocket\.de/img_sat/[^<]+)</loc>", sitemap_text)


def _slug_matches(keyword: str, slug: str) -> bool:
    """True if keyword matches slug at a word boundary, preventing e.g.
    "sentinel" from matching "biosentinel". Keywords already ending in
    "-"/"_" are specific enough to use plain substring matching."""
    if keyword.endswith(("-", "_")):
        return keyword in slug
    return bool(re.search(r"(?:^|[-_])" + re.escape(keyword), slug))


def _search_keywords(family_name: str) -> list[str]:
    """Lowercase keyword variants to match against sitemap image slugs.
    Checks GUNTERS_ALIASES first, then falls back to variants derived from
    the family name itself (hyphenated, underscored, squashed, first word)."""
    upper = family_name.strip().upper().split("(")[0].split("/")[0].strip()
    if upper in GUNTERS_ALIASES:
        return GUNTERS_ALIASES[upper]

    name = re.sub(r"\s*\(.*?\)\s*", " ", family_name).strip()
    name = name.split("/")[0].strip().lower()
    words = name.split()
    if not words:
        return []

    keywords = []
    if len(words) >= 2:
        base = " ".join(words)
        keywords.append(re.sub(r"\s+", "-", base))
        keywords.append(re.sub(r"\s+", "_", base))
        keywords.append(re.sub(r"\s+", "", base))

    first = words[0]
    if first not in keywords:
        keywords.append(first)
    return keywords


def search_images(
    session: requests.Session, conn, family_name: str, sleep_fn=time.sleep, domain_hint: str | None = None
) -> dict:
    """domain_hint is accepted for shape-compatibility with the other
    sources but unused here -- Gunter's matching is already satellite-only
    by construction (it only ever indexes /img_sat/ sitemap entries)."""
    _require_enabled()
    sitemap = _load_sitemap(session, conn, sleep_fn)
    return {"family_name": family_name, "urls": _all_img_sat_urls(sitemap)}


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    keywords = _search_keywords(raw.get("family_name", query))
    if not keywords:
        return []

    results = []
    seen = set()
    for url in raw.get("urls", []):
        slug = Path(urlparse(url).path).name.lower()
        if url in seen or not any(_slug_matches(kw, slug) for kw in keywords):
            continue
        if any(word in slug for word in NOISE_WORDS):
            continue
        seen.add(url)
        results.append({"url": url, "title": Path(urlparse(url).path).name})
    return results


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    """Extract provenance and mark that this source provides no license metadata."""
    if not ENABLED:
        return None

    url = raw_item["url"]
    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"
    return ImageRecord(
        family=query,
        source_module=SOURCE_MODULE,
        external_id=url,
        source_url=url,
        download_url=url,
        title=raw_item.get("title", ""),
        description="",
        author="",
        license_short_name=UNVERIFIED_LICENSE,
        license_url=None,
        attribution_required=None,
        credit=None,
        retrieved_at=retrieved_at,
        mime=f"image/{ext}",
    )


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    _require_enabled()
    host = rate_limiter.host_of(url)
    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        resp = session.get(url, stream=True, timeout=20)
        if resp.status_code == 200:
            return stream_to_file(resp, dest_path)
        if resp.status_code == 429:
            rate_limiter.record_cooldown(conn, host, 60 * attempt)
            continue
        return False
    return False


if __name__ == "__main__":
    print(f"gunters_space: ENABLED={ENABLED}")
    if not ENABLED:
        print("Disabled -- see this module's docstring for what's required to turn it on.")
    else:
        print(_search_keywords("STARLINK"))
