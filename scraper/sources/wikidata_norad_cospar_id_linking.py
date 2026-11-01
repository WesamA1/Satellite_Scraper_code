"""Wikidata source: resolves a satellite's Wikidata item to its P18 ("image")
property, which points at a Commons file. Drafted by hand (not the Claude
codegen path -- ANTHROPIC_API_KEY wasn't set) by Claude Code directly. Still
needs a human read before promote() -- same bar as generated code.

Important scope note for whoever reviews this: the candidate was proposed
under the heading "Wikidata linking (NORAD/COSPAR ID)", which in CLAUDE.md's
roadmap is actually a *separate* idea from an image source -- cross-
referencing a satellite's NORAD/COSPAR catalog numbers against its Wikidata
item for data-quality purposes, not fetching images. This module only
implements the image-fetching half (P18), which is a real, if thin,
additional image source -- one P18 value per item, typically. The
NORAD/COSPAR metadata-linking idea is a different feature and isn't built
here.

Like the Wikipedia multi-language source, every image this finds is itself
a Commons file, so this reuses scraper.sources.wikimedia's extmetadata
parsing (see _commons below) rather than duplicating it. Same known
limitation applies: a file already fetched via the "wikimedia" source
isn't recognized as already-fetched here, since license_gate dedupes on
(source_module, external_id) and this uses a different source_module.
"""

import time

import requests

from scraper import rate_limiter
from scraper.sources import wikimedia as _commons
from scraper.sources.base import ImageRecord

SOURCE_MODULE = "wikidata"

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
IMAGE_PROPERTY = "P18"  # Wikidata's stable "image" property


def make_session(dev: bool = False) -> requests.Session:
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 requests/2.31"})
    return session


def _find_item_image(session: requests.Session, conn, query: str, sleep_fn) -> str | None:
    """wbsearchentities to find the best-matching item, then wbgetclaims for
    its P18 value (a bare Commons filename, no "File:" prefix)."""
    host = rate_limiter.host_of(WIKIDATA_API)
    try:
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        search_resp = session.get(
            WIKIDATA_API,
            params={
                "action": "wbsearchentities",
                "search": query,
                "language": "en",
                "format": "json",
                "limit": 1,
            },
            timeout=30,
        )
        hits = search_resp.json().get("search", [])
        if not hits:
            return None
        entity_id = hits[0]["id"]

        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        claims_resp = session.get(
            WIKIDATA_API,
            params={
                "action": "wbgetclaims",
                "entity": entity_id,
                "property": IMAGE_PROPERTY,
                "format": "json",
            },
            timeout=30,
        )
        claims = claims_resp.json().get("claims", {}).get(IMAGE_PROPERTY, [])
        if not claims:
            return None
        return claims[0]["mainsnak"]["datavalue"]["value"]
    except Exception:
        return None


def search_images(
    session: requests.Session, conn, query: str, sleep_fn=time.sleep, domain_hint: str | None = None
) -> dict:
    """domain_hint is accepted for shape-compatibility but unused -- a
    Wikidata entity search already scopes to the exact name. Returns the
    same shape Commons' imageinfo+extmetadata response has (one entry at
    most), so relevant_results/extract_record reuse wikimedia.py's parsing
    unchanged."""
    filename = _find_item_image(session, conn, query, sleep_fn)
    if not filename:
        return {}

    host = rate_limiter.host_of(COMMONS_API)
    try:
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        resp = session.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "titles": f"File:{filename}",
                "prop": "imageinfo",
                "iiprop": "url|mime|thumburl|extmetadata|descriptionurl",
                "iiurlwidth": 1200,
            },
            timeout=30,
        )
        if resp.text.strip():
            return resp.json()
    except Exception:
        pass  # a malformed/unreachable response here shouldn't abort the whole scrape run
    return {}


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    """A Wikidata item's P18 is already the one specific image for that
    exact entity -- no further relevance filtering needed, just pass
    through whatever Commons resolved (0 or 1 pages)."""
    pages = raw.get("query", {}).get("pages", {})
    return list(pages.values())


def extract_record(page: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    """Reuses wikimedia.extract_record's extmetadata parsing (same Commons
    data), then relabels the provenance to this source."""
    from dataclasses import replace

    record = _commons.extract_record(page, query, retrieved_at)
    if record is None:
        return None
    return replace(record, source_module=SOURCE_MODULE)


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    """Identical to wikimedia.download_image -- it's the same CDN."""
    return _commons.download_image(session, conn, url, dest_path, sleep_fn=sleep_fn)
