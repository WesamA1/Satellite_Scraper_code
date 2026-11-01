"""Wikipedia (multi-language) source: finds Commons files embedded in a
satellite's Wikipedia article across several language editions, not just
the English one -- different language editions often embed different
photos of the same satellite. Drafted by hand (not the Claude codegen path
-- ANTHROPIC_API_KEY wasn't set) by Claude Code directly. Still needs a
human read before promote() -- same bar as generated code.

This isn't a separate license regime from Wikimedia Commons: every image an
article embeds is itself a Commons file, with the exact same extmetadata
Commons' own search API returns. So instead of re-implementing extmetadata
parsing, this reuses scraper.sources.wikimedia's relevant_pages/extract_record
directly (see _COMMONS below) -- the only new logic here is *discovery*: find
candidate Commons filenames via each language Wikipedia's article, then
resolve them through Commons' own imageinfo endpoint.

Known limitation for the human reviewer to weigh before promoting: a file
already fetched via the "wikimedia" source keeps its own
(source_module="wikimedia", external_id=<title>) key in the `sources` table.
This module inserts the same file under
(source_module="wikipedia_multilang", external_id=<same title>) --
license_gate.is_already_fetched() dedupes per (source_module, external_id)
pair, so a file already present via direct Commons search would NOT be
recognized as a duplicate here and could be downloaded a second time. Worth
fixing (e.g. a shared external_id namespace, or checking "wikimedia" too)
before relying on this source for volume.
"""

import time

import requests

from scraper import rate_limiter
from scraper.sources import wikimedia as _commons
from scraper.sources.base import ImageRecord

SOURCE_MODULE = "wikipedia_multilang"

COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Language editions to check. Add more here as needed -- a human should
# weigh request volume (one search + one images call per language per
# family) against how often a non-English edition actually adds anything.
LANGUAGE_WIKIS = ["en", "de", "fr", "es", "ja", "ru"]

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff")


def make_session(dev: bool = False) -> requests.Session:
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 requests/2.31"})
    return session


def _embedded_commons_titles(session: requests.Session, conn, query: str, sleep_fn) -> set[str]:
    """For each configured language, find the best-matching article (a plain
    text search -- titles rarely match a family name exactly once
    translated), then list the Commons filenames it embeds."""
    titles: set[str] = set()

    for lang in LANGUAGE_WIKIS:
        api_url = f"https://{lang}.wikipedia.org/w/api.php"
        host = rate_limiter.host_of(api_url)
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            search_resp = session.get(
                api_url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 1,
                    "format": "json",
                },
                timeout=30,
            )
            hits = search_resp.json().get("query", {}).get("search", [])
            if not hits:
                continue
            article_title = hits[0]["title"]

            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            images_resp = session.get(
                api_url,
                params={
                    "action": "query",
                    "titles": article_title,
                    "prop": "images",
                    "imlimit": 50,
                    "format": "json",
                },
                timeout=30,
            )
            pages = images_resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                for img in page.get("images", []):
                    name = img.get("title", "")
                    if name.lower().endswith(IMAGE_EXTENSIONS):
                        titles.add(name)
        except Exception:
            continue

    return titles


def search_images(
    session: requests.Session, conn, query: str, sleep_fn=time.sleep, domain_hint: str | None = None
) -> dict:
    """domain_hint is accepted for shape-compatibility but unused -- the
    language-article search already scopes to the exact family name, with
    no generic-term suffix needed. Returns the same shape Commons'
    imageinfo+extmetadata response has, so relevant_results/extract_record
    can reuse wikimedia.py's parsing unchanged."""
    titles = _embedded_commons_titles(session, conn, query, sleep_fn)
    if not titles:
        return {}

    host = rate_limiter.host_of(COMMONS_API)
    try:
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        resp = session.get(
            COMMONS_API,
            params={
                "action": "query",
                "format": "json",
                "titles": "|".join(sorted(titles)[:50]),
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
    """Delegates to wikimedia.relevant_pages -- same extmetadata shape, same
    noise-word/keyword filter."""
    pages = raw.get("query", {}).get("pages", {})
    keywords = _commons.SPACE_KEYWORDS if domain_keywords is None else domain_keywords
    return _commons.relevant_pages(pages, query, domain_keywords=keywords)


def extract_record(page: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    """Reuses wikimedia.extract_record's extmetadata parsing (same Commons
    data), then relabels the provenance to this source instead of
    "wikimedia" -- it was found by a different discovery path."""
    from dataclasses import replace

    record = _commons.extract_record(page, query, retrieved_at)
    if record is None:
        return None
    return replace(record, source_module=SOURCE_MODULE)


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    """Identical to wikimedia.download_image -- it's the same CDN."""
    return _commons.download_image(session, conn, url, dest_path, sleep_fn=sleep_fn)
