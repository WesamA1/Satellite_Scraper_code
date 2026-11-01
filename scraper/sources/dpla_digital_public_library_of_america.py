"""DPLA (Digital Public Library of America) source: https://dp.la --
aggregates ~4000+ US libraries/archives/museums via a search API. Drafted
by hand (not the Claude codegen path -- ANTHROPIC_API_KEY wasn't set) by
Claude Code directly, following the same ImageSource contract and
rate_limiter usage as wikimedia.py/nasa_images.py. Still needs a human read
before promote() -- same bar as generated code, but this one in particular:

Needs SOURCE_DPLA_API_KEY in .env (self-service: see
https://pro.dp.la/developers/api-basics -- DPLA emails you a key within a
minute or two of requesting one, no approval or form).

License handling is the weak point here, flagged in the seed list as
license_verifiable: false for a real reason. DPLA's per-item
`sourceResource.rights` field is **free text**, written independently by
each of the thousands of contributing institutions -- there's no fixed
controlled vocabulary the way Flickr's numeric license ids or Europeana's
rightsstatements.org URIs give you. The OPEN_RIGHTS_TEXT allowlist below is
a narrow, literal-substring match against a few known-safe phrasings seen
in practice; it will under-match (miss genuinely open items phrased
differently) far more than it will over-match, which is the safe direction
to err in, but a human should look hard at whatever this actually returns
before trusting it at any volume.
"""

import time
from pathlib import Path

import requests

from scraper import rate_limiter, secrets
from scraper.sources import wikimedia
from scraper.sources.base import ImageRecord, stream_to_file

SOURCE_MODULE = "dpla"
REQUIRES_API_KEY = True

SEARCH_URL = "https://api.dp.la/v2/items"

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
    # A FLEXIBLE LINK", a 19th-century kinematic-models collection DPLA very
    # plausibly also aggregates, nothing to do with spacecraft.
    "linkwork",
    "flexible link",
]

# Narrow, literal substrings of known-safe rights text. Deliberately not
# trying to be clever about parsing free text -- see module docstring.
OPEN_RIGHTS_TEXT = (
    "creativecommons.org/publicdomain/zero",
    "creativecommons.org/licenses/by/",
    "no copyright",
    "public domain",
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
    api_key = secrets.get_source_api_key("dpla")
    if not api_key:
        return {}

    text = f"{query} {domain_hint}".strip() if domain_hint else query
    host = rate_limiter.host_of(SEARCH_URL)
    for attempt in range(1, 4):
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            resp = session.get(SEARCH_URL, params={"api_key": api_key, "q": text, "page_size": 100}, timeout=30)
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
    convention as wikimedia itself -- thousands of contributing institutions
    rarely title anything with the literal satellite family name, so
    requiring only that match meant this source almost never found anything
    even when the underlying API genuinely had relevant items."""
    docs = raw.get("docs", [])
    term = query.strip().lower()
    keywords = wikimedia.SPACE_KEYWORDS if domain_keywords is None else domain_keywords

    results = []
    for doc in docs:
        title = (
            " ".join(doc.get("sourceResource", {}).get("title", []) or []).lower()
            if isinstance(doc.get("sourceResource", {}).get("title"), list)
            else (doc.get("sourceResource", {}).get("title") or "").lower()
        )
        if any(word in title for word in NOISE_WORDS):
            continue
        if term and term not in title and not any(keyword in title for keyword in keywords):
            continue
        results.append(doc)
    return results


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    url = raw_item.get("object")
    if not url:
        return None

    rights_raw = raw_item.get("sourceResource", {}).get("rights", "")
    rights_text = (rights_raw[0] if isinstance(rights_raw, list) else rights_raw) or ""
    is_open = any(signal in rights_text.lower() for signal in OPEN_RIGHTS_TEXT)

    title_raw = raw_item.get("sourceResource", {}).get("title", "")
    title = (title_raw[0] if isinstance(title_raw, list) else title_raw) or ""
    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"

    return ImageRecord(
        family=query,
        source_module=SOURCE_MODULE,
        external_id=raw_item.get("id", ""),
        source_url=raw_item.get("isShownAt", url),
        download_url=url,
        title=title,
        description="",
        author="",
        license_short_name=rights_text if is_open else "",
        license_url=None,
        attribution_required=None,
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
