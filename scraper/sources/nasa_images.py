"""NASA Image and Video Library source: network functions, relevance filter, and
the search-item -> ImageRecord mapping.

No auth needed. Network/backoff logic ported from reference/sources/nasa_images.py
largely unchanged.
"""

import re
import time
from pathlib import Path

import requests

from scraper import rate_limiter
from scraper.sources.base import ImageRecord, stream_to_file

SOURCE_MODULE = "nasa"

SEARCH_URL = "https://images-api.nasa.gov/search"

MAX_IMAGES_PER_FAMILY = 30

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


def make_session(dev: bool = False) -> requests.Session:
    """`dev=True` returns a requests_cache.CachedSession -- see
    wikimedia.make_session's docstring for why this defaults to off."""
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31",
        }
    )
    return session


def get_search_term(family_name: str) -> str:
    name = re.sub(r"\(.*?\)", "", family_name)
    name = name.split("/")[0]
    return name.strip().lower()


def _retry_after_seconds(resp, fallback: float) -> float:
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    return fallback


def search_images(
    session: requests.Session, conn, family_name: str, sleep_fn=time.sleep, domain_hint: str | None = "satellite"
) -> dict:
    """Search the NASA Image Library for family_name (+ domain_hint, if given).
    Returns the raw JSON response dict ({} on failure). `domain_hint` defaults to
    "satellite" to match today's behavior; pass None/"" for a domain-agnostic
    prompt search."""
    term = get_search_term(family_name)
    query = f"{term} {domain_hint}".strip() if domain_hint else term
    host = rate_limiter.host_of(SEARCH_URL)
    for attempt in range(1, 4):
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            resp = session.get(
                SEARCH_URL,
                params={
                    "q": query,
                    "media_type": "image",
                    "page_size": 100,
                },
                timeout=30,
            )
            if resp.status_code == 200 and resp.text.strip():
                return resp.json()
            if resp.status_code == 429:
                wait = _retry_after_seconds(resp, fallback=60 * attempt)
                rate_limiter.record_cooldown(conn, host, wait)
                continue
            return {}
        except Exception:
            sleep_fn(30)
    return {}


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    """Download a single image to dest_path. Returns True on success."""
    host = rate_limiter.host_of(url)
    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        try:
            resp = session.get(url, stream=True, timeout=30)
        except Exception:
            return False

        if resp.status_code == 200:
            return stream_to_file(resp, dest_path)
        elif resp.status_code == 429:
            wait = _retry_after_seconds(resp, fallback=60 * attempt)
            rate_limiter.record_cooldown(conn, host, wait)
        else:
            return False

    return False


def relevant_items(raw: dict, family_name: str) -> list[dict]:
    """Filter raw search response items to ones actually about family_name.

    Ported from the non-metadata half of old filter_images() -- noise words,
    search-term-in-title/keywords/description. Caps at MAX_IMAGES_PER_FAMILY.
    """
    term = get_search_term(family_name)
    items = raw.get("collection", {}).get("items", [])

    results = []
    for item in items:
        data_list = item.get("data", [])
        if not data_list:
            continue
        data = data_list[0]
        title = (data.get("title") or "").lower()
        desc = (data.get("description") or "").lower()
        keys = " ".join(data.get("keywords") or []).lower()

        if any(word in title for word in NOISE_WORDS):
            continue
        if term not in title and term not in keys and term not in desc:
            continue

        results.append(item)
        if len(results) >= MAX_IMAGES_PER_FAMILY:
            break

    return results


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    """Registry-facing alias matching the uniform ImageSource contract's
    relevant_results(raw, query, domain_keywords) shape (see
    scraper/sources/base.py). NASA's filter never had a domain-keyword
    fallback, so `domain_keywords` is accepted for signature uniformity and
    ignored."""
    return relevant_items(raw, query)


def _pick_asset_link(links: list[dict]) -> str | None:
    """Choose the best image URL from a search item's links list.

    Ported unchanged from reference/sources/nasa_images.py:81-98. Prefers the
    ~large rendition (1920px), then the canonical original, then any other image
    link.
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


def extract_record(item: dict, family_name: str, retrieved_at: str) -> ImageRecord | None:
    """Build an ImageRecord from one item of a NASA /search response's collection.items.

    Returns None if the item has no usable data block or no image link -- nothing
    to gate or persist.
    """
    data_list = item.get("data", [])
    if not data_list:
        return None
    data = data_list[0]

    url = _pick_asset_link(item.get("links", []))
    if not url:
        return None

    nasa_id = data.get("nasa_id", "")
    author = data.get("photographer", "") or data.get("secondary_creator", "")

    # The NASA API marks items with a copyright field when usage is restricted
    # (e.g. contractor/partner-credited images); its absence is the real
    # public-domain signal. The old code hardcoded public domain for everything.
    copyright_field = (data.get("copyright") or "").strip()
    is_public_domain = not copyright_field

    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpeg"

    return ImageRecord(
        family=family_name,
        source_module=SOURCE_MODULE,
        external_id=nasa_id,
        source_url=f"https://images.nasa.gov/details/{nasa_id}",
        download_url=url,
        title=data.get("title", ""),
        description=data.get("description", ""),
        author=author,
        license_short_name="NASA (public domain)" if is_public_domain else "",
        license_url=("https://www.nasa.gov/nasa-brand-center/images-and-media/" if is_public_domain else None),
        attribution_required=False if is_public_domain else None,
        credit=author or None,
        retrieved_at=retrieved_at,
        mime=f"image/{ext}",
    )
