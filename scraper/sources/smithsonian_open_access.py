"""Smithsonian Open Access source: https://www.si.edu/openaccess -- CC0,
with a per-item usage field in the Open Access API. Drafted by hand (not the
Claude codegen path -- ANTHROPIC_API_KEY wasn't set) by Claude Code directly,
following the same ImageSource contract and rate_limiter usage as
wikimedia.py/nasa_images.py. Still needs a human read before promote() --
same bar as generated code.

Needs SOURCE_SMITHSONIAN_API_KEY in .env (free key from
https://api.data.gov/signup/, same program as NASA's api.nasa.gov keys).

Response shape confirmed by hand against the live API (api.si.edu/openaccess/
api/v1.0/search): each hit is a "row" with the actual metadata nested under
content.descriptiveNonRepeating -- usage rights at .metadata_usage.access
(only "CC0" means usable here; this project doesn't do anything with
non-CC0 Smithsonian records), and media files (when a record has any) under
.online_media.media[], each with a "content" URL. Plenty of Smithsonian
records (books, archival text) have no media at all -- extract_record
returns None for those, same as any other "nothing to download" case.
"""

import time
from pathlib import Path

import requests

from scraper import rate_limiter, secrets
from scraper.sources import wikimedia
from scraper.sources.base import ImageRecord, stream_to_file

SOURCE_MODULE = "smithsonian"
REQUIRES_API_KEY = True

SEARCH_URL = "https://api.si.edu/openaccess/api/v1.0/search"

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
    # type of gear/linkage) -- confirmed live via Europeana's identical
    # fallback matching: "HAIN LINKWORK/LEVER-TYPE SATELLITE MECHANISM WITH
    # A FLEXIBLE LINK", a 19th-century kinematic-models collection, nothing
    # to do with spacecraft.
    "linkwork",
    "flexible link",
]


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
    api_key = secrets.get_source_api_key("smithsonian")
    if not api_key:
        return {}

    text = f"{query} {domain_hint}".strip() if domain_hint else query
    host = rate_limiter.host_of(SEARCH_URL)
    for attempt in range(1, 4):
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            resp = session.get(SEARCH_URL, params={"api_key": api_key, "q": text, "rows": 100}, timeout=30)
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


def _non_repeating(row: dict) -> dict:
    return row.get("content", {}).get("descriptiveNonRepeating", {})


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    """`domain_keywords=None` falls back to wikimedia.SPACE_KEYWORDS, same
    convention as wikimedia itself -- museum catalog titles rarely contain
    the literal satellite family name, so requiring only that match meant
    this source almost never found anything even when the underlying API
    genuinely had relevant items."""
    rows = raw.get("response", {}).get("rows", [])
    term = query.strip().lower()
    keywords = wikimedia.SPACE_KEYWORDS if domain_keywords is None else domain_keywords

    results = []
    for row in rows:
        title = (_non_repeating(row).get("title", {}).get("content") or "").lower()
        if any(word in title for word in NOISE_WORDS):
            continue
        if term and term not in title and not any(keyword in title for keyword in keywords):
            continue
        results.append(row)
    return results


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    non_repeating = _non_repeating(raw_item)
    media_list = non_repeating.get("online_media", {}).get("media", [])
    url = next((m.get("content") for m in media_list if m.get("content")), None)
    if not url:
        return None

    access = (non_repeating.get("metadata_usage", {}).get("access") or "").strip()
    is_open = access.upper() == "CC0"

    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"

    return ImageRecord(
        family=query,
        source_module=SOURCE_MODULE,
        external_id=raw_item.get("id", ""),
        source_url=non_repeating.get("record_link", url),
        download_url=url,
        title=non_repeating.get("title", {}).get("content", ""),
        description="",
        author="Smithsonian Institution",
        license_short_name="CC0 1.0" if is_open else "",
        license_url="https://www.si.edu/termsofuse" if is_open else None,
        attribution_required=False,
        credit="Smithsonian Institution",
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
