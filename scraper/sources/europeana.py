"""Europeana source: https://www.europeana.eu -- aggregates many European
cultural institutions, with a per-item rights statement via the Search API.
Drafted by hand (not the Claude codegen path -- ANTHROPIC_API_KEY wasn't
set) by Claude Code directly, following the same ImageSource contract and
rate_limiter usage as wikimedia.py/nasa_images.py. Still needs a human read
before promote() -- same bar as generated code.

Needs SOURCE_EUROPEANA_API_KEY in .env (free key from
https://pro.europeana.eu/page/get-api -- as of 2025 this requires creating a
Europeana account first, then requesting a personal key from account settings).

License handling, confirmed by hand against the live API
(api.europeana.eu/record/v2/search.json): each item's "rights" field is a
per-item URI, but Europeana aggregates far more than it liberates -- most
hits carry a rightsstatements.org "In Copyright"/"Copyright Not Evaluated"
style URI, which is NOT open. Only creativecommons.org URIs for CC0, the
Public Domain Mark, CC BY, and CC BY-SA are treated as open here;
rightsstatements.org is excluded entirely rather than trying to judge which
of its many non-CC categories might be safe -- same conservative stance as
every other aggregator in this project.
"""

import time
from pathlib import Path

import requests

from scraper import rate_limiter, secrets
from scraper.sources import wikimedia
from scraper.sources.base import ImageRecord, stream_to_file

SOURCE_MODULE = "europeana"
REQUIRES_API_KEY = True

SEARCH_URL = "https://api.europeana.eu/record/v2/search.json"

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
    # "satellite" has a real, unrelated meaning in mechanical engineering (a
    # type of gear/linkage) -- confirmed live, this exact title recurred
    # dozens of times for one family's Europeana hits: "HAIN LINKWORK/LEVER-
    # TYPE SATELLITE MECHANISM WITH A FLEXIBLE LINK", a 19th-century
    # kinematic-models collection, nothing to do with spacecraft.
    "linkwork",
    "flexible link",
]

OPEN_RIGHTS_PREFIXES = (
    "http://creativecommons.org/publicdomain/zero/",
    "http://creativecommons.org/publicdomain/mark/",
    "http://creativecommons.org/licenses/by/",
    "http://creativecommons.org/licenses/by-sa/",
)


def make_session(dev: bool = False) -> requests.Session:
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 requests/2.31"})
    return session


def _retry_after_seconds(resp, fallback: float) -> float:
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    return fallback


def search_images(
    session: requests.Session, conn, query: str, sleep_fn=time.sleep, domain_hint: str | None = "satellite"
) -> dict:
    api_key = secrets.get_source_api_key("europeana")
    if not api_key:
        return {}

    text = f"{query} {domain_hint}".strip() if domain_hint else query
    host = rate_limiter.host_of(SEARCH_URL)
    for attempt in range(1, 4):
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            resp = session.get(
                SEARCH_URL,
                params={
                    "wskey": api_key,
                    "query": text,
                    "media": "true",
                    "rows": 100,
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


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    """`domain_keywords=None` falls back to wikimedia.SPACE_KEYWORDS, same
    convention as wikimedia itself -- Europeana rarely titles anything with
    the literal satellite family name, so requiring only that match (the
    original behavior here) meant this source almost never found anything
    even when the underlying API genuinely had relevant items."""
    items = raw.get("items", [])
    term = query.strip().lower()
    keywords = wikimedia.SPACE_KEYWORDS if domain_keywords is None else domain_keywords

    results = []
    for item in items:
        titles = item.get("title") or []
        title = " ".join(titles).lower()
        if any(word in title for word in NOISE_WORDS):
            continue
        if term and term not in title and not any(keyword in title for keyword in keywords):
            continue
        results.append(item)
    return results


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    media_urls = raw_item.get("edmIsShownBy") or raw_item.get("edmPreview") or []
    url = media_urls[0] if media_urls else None
    if not url:
        return None

    rights_list = raw_item.get("rights") or []
    rights_uri = rights_list[0] if rights_list else ""
    is_open = rights_uri.startswith(OPEN_RIGHTS_PREFIXES)

    titles = raw_item.get("title") or []
    title = titles[0] if titles else ""
    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"

    return ImageRecord(
        family=query,
        source_module=SOURCE_MODULE,
        external_id=raw_item.get("id", ""),
        source_url=raw_item.get("guid", url),
        download_url=url,
        title=title,
        description="",
        author="",
        license_short_name=rights_uri if is_open else "",
        license_url=rights_uri if is_open else None,
        attribution_required="/by/" in rights_uri or "/by-sa/" in rights_uri,
        credit=None,
        retrieved_at=retrieved_at,
        mime=f"image/{ext}",
    )


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    host = rate_limiter.host_of(url)
    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        try:
            resp = session.get(url, stream=True, timeout=30)
        except Exception:
            return False

        if resp.status_code == 200:
            return stream_to_file(resp, dest_path)
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp, fallback=60 * attempt)
            rate_limiter.record_cooldown(conn, host, wait)
        else:
            return False

    return False
