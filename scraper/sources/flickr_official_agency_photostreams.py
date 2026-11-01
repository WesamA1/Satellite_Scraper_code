"""Flickr source: official agency photostreams (NASA HQ, ESA, SpaceX, Rocket
Lab, ...) via the Flickr REST API. Drafted by hand (not the Claude codegen
path -- ANTHROPIC_API_KEY wasn't set) by Claude Code directly, following the
same ImageSource contract and rate_limiter usage as wikimedia.py/nasa_images.py.
Still needs a human read before promote() -- same bar as generated code.

Needs SOURCE_FLICKR_API_KEY in .env (scraper.secrets' per-source key
convention) -- an API key from https://www.flickr.com/services/apps/create/,
which as of 2026 requires an active Flickr Pro subscription to request.

License handling: Flickr's `license` field on a photo is a numeric id for one
of ~17 fixed license types (flickr.photos.licenses.getInfo). Only ids that are
unambiguously open for reuse in a training dataset are accepted --
NC (non-commercial) and ND (no-derivatives) variants are deliberately
excluded, since an ML training set arguably creates a derivative work and may
be used commercially; CLAUDE.md's accepted-license list doesn't carve out an
exception for that.
"""

import time
from pathlib import Path

import requests

from scraper import rate_limiter, secrets
from scraper.sources import wikimedia
from scraper.sources.base import ImageRecord, stream_to_file

SOURCE_MODULE = "flickr"
REQUIRES_API_KEY = True

SEARCH_URL = "https://api.flickr.com/services/rest/"

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

# flickr.photos.licenses.getInfo's fixed id -> name mapping. Only the ids in
# OPEN_LICENSE_IDS are treated as usable; everything else (including every
# -NC/-ND variant) leaves license_short_name empty, which is what correctly
# makes the license gate reject the record.
LICENSE_NAMES = {
    0: "All Rights Reserved",
    1: "CC BY-NC-SA 2.0",
    2: "CC BY-NC 2.0",
    3: "CC BY-NC-ND 2.0",
    4: "CC BY 2.0",
    5: "CC BY-SA 2.0",
    6: "CC BY-ND 2.0",
    7: "No known copyright restrictions",
    8: "United States Government Work",
    9: "CC0 1.0",
    10: "Public Domain Mark",
    11: "CC BY 4.0",
    12: "CC BY-SA 4.0",
    13: "CC BY-ND 4.0",
    14: "CC BY-NC 4.0",
    15: "CC BY-NC-SA 4.0",
    16: "CC BY-NC-ND 4.0",
}
OPEN_LICENSE_IDS = {4, 5, 7, 8, 9, 10, 11, 12}

# Official agency photostream user ids -- Flickr search needs the numeric
# NSID, not the display name. A human should add more accounts here (and
# confirm these are still current) before promoting.
AGENCY_USER_IDS = {
    "nasa": "24662369@N07",  # NASA HQ Photo
    "esa": "95864528@N00",  # European Space Agency, ESA
    "spacex": "130608600@N05",  # SpaceX
}


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
    """Searches across the known agency photostreams for `query` (+
    domain_hint, if given). Returns the raw JSON response ({} on failure)."""
    api_key = secrets.get_source_api_key("flickr")
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
                    "method": "flickr.photos.search",
                    "api_key": api_key,
                    "text": text,
                    "user_id": ",".join(AGENCY_USER_IDS.values()),
                    "extras": "license,owner_name,url_o,url_l,date_upload",
                    "per_page": 100,
                    "format": "json",
                    "nojsoncallback": 1,
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
    convention as wikimedia itself -- agency photostream captions rarely
    contain the literal satellite family name, so requiring only that match
    meant this source almost never found anything even when the underlying
    API genuinely had relevant items."""
    photos = raw.get("photos", {}).get("photo", [])
    term = query.strip().lower()
    keywords = wikimedia.SPACE_KEYWORDS if domain_keywords is None else domain_keywords

    results = []
    for photo in photos:
        title = (photo.get("title") or "").lower()
        if any(word in title for word in NOISE_WORDS):
            continue
        if term and term not in title and not any(keyword in title for keyword in keywords):
            continue
        results.append(photo)
    return results


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    url = raw_item.get("url_o") or raw_item.get("url_l")
    if not url:
        return None

    try:
        license_id = int(raw_item.get("license", -1))
    except (TypeError, ValueError):
        license_id = -1

    license_name = LICENSE_NAMES.get(license_id, "")
    is_open = license_id in OPEN_LICENSE_IDS

    photo_id = raw_item.get("id", "")
    ext = Path(url.split("?")[0]).suffix.lstrip(".").lower() or "jpg"

    return ImageRecord(
        family=query,
        source_module=SOURCE_MODULE,
        external_id=photo_id,
        source_url=f"https://www.flickr.com/photos/{raw_item.get('owner', '')}/{photo_id}",
        download_url=url,
        title=raw_item.get("title", ""),
        description="",
        author=raw_item.get("ownername", ""),
        license_short_name=license_name if is_open else "",
        license_url="https://www.flickr.com/services/api/flickr.photos.licenses.getInfo.html" if is_open else None,
        attribution_required=license_id in {4, 5, 11, 12},  # the BY/BY-SA variants ask for credit
        credit=raw_item.get("ownername") or None,
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
