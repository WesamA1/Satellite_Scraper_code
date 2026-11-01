"""Wikimedia Commons source: network functions, relevance filter, and the
extmetadata -> ImageRecord mapping.

Network/backoff logic is ported from reference/sources/wikimedia.py largely
unchanged. Credentials come from .env (WIKIMEDIA_BOT_USERNAME/PASSWORD), same as
reference/sources/wikimedia.py.
"""

import os
import re
import time

import requests
from dotenv import load_dotenv

from scraper import rate_limiter
from scraper.sources.base import ImageRecord, stream_to_file

load_dotenv()

SOURCE_MODULE = "wikimedia"

LOGIN_URL = "https://meta.wikimedia.org/w/api.php"
SEARCH_URL = "https://commons.wikimedia.org/w/api.php"

# Trimmed to genuinely generic noise terms; see reference/sources/wikimedia.py:128-137
# for the history of why location/ambiguous words were removed (false negatives).
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
    # A FLEXIBLE LINK", a 19th-century kinematic-models collection that
    # Commons could plausibly host scans from too, nothing to do with
    # spacecraft.
    "linkwork",
    "flexible link",
]

SPACE_KEYWORDS = [
    "satellite",
    "spacecraft",
    "space",
    "orbit",
    "launch",
    "rocket",
    "mission",
    "nasa",
    "esa",
    "jaxa",
    "payload",
    "telescope",
    "probe",
    "station",
    "module",
    "capsule",
    "cubesat",
    "nanosat",
    "relay",
    "comsat",
    "spacex",
    "aerial",
    "astro",
    "cosmo",
    "cosmos",
    "geo",
    "leo",
    "meo",
    "solar",
    "lunar",
    "interplanetary",
    "constellation",
    "deployment",
    "fairing",
    "booster",
    "propulsion",
    "thruster",
    "antenna",
    "transponder",
    "imaging",
    "remote sensing",
    "earth observation",
    "communications",
    "navigation",
    "gps",
    "galileo",
    "glonass",
    "beidou",
    "iridium",
    "intelsat",
    "boeing",
    "airbus",
    "maxar",
    "lockheed",
    "northrop",
    "ball aerospace",
    "thales",
    "isro",
    "roscosmos",
]


def make_session(dev: bool = False) -> requests.Session:
    """Create a requests session with the correct headers for Wikimedia.

    `dev=True` returns a requests_cache.CachedSession instead -- Task 9's dev
    profile. Off by default everywhere: a cached response masking real API/
    rate-limit state would cut against "be polite to APIs" if left on by
    accident in a real scrape.
    """
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    # Must set User-Agent or Wikimedia returns 403
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 (wa00008@mix.wvu.edu) requests/2.31"})
    return session


def _retry_after_seconds(resp, fallback: float) -> float:
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header)
        except ValueError:
            pass
    return fallback


def login(session: requests.Session, conn, sleep_fn=time.sleep) -> bool:
    username = os.environ["WIKIMEDIA_BOT_USERNAME"]
    password = os.environ["WIKIMEDIA_BOT_PASSWORD"]

    rate_limiter.wait_for_cooldown(conn, rate_limiter.host_of(LOGIN_URL), sleep_fn)
    resp = session.get(
        LOGIN_URL,
        params={
            "action": "query",
            "meta": "tokens",
            "type": "login",
            "format": "json",
        },
    )
    token = resp.json()["query"]["tokens"]["logintoken"]

    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, rate_limiter.host_of(LOGIN_URL), sleep_fn)
        resp = session.post(
            LOGIN_URL,
            data={
                "action": "login",
                "lgname": username,
                "lgpassword": password,
                "lgtoken": token,
                "format": "json",
            },
        )

        if resp.status_code == 429:
            wait = _retry_after_seconds(resp, fallback=60 * attempt)  # 60s, 120s, 180s
            rate_limiter.record_cooldown(conn, rate_limiter.host_of(LOGIN_URL), wait)
            rate_limiter.wait_for_cooldown(conn, rate_limiter.host_of(LOGIN_URL), sleep_fn)
            continue

        if not resp.text.strip():
            sleep_fn(30)
            continue

        result = resp.json()
        return result.get("login", {}).get("result") == "Success"

    return False


def search_images(
    session: requests.Session, conn, family_name: str, sleep_fn=time.sleep, domain_hint: str | None = "satellite"
) -> dict:
    """Search Commons for family_name (+ domain_hint, if given). Returns the raw
    JSON response ({} on failure). `domain_hint` defaults to "satellite" to match
    today's behavior exactly; pass None/"" for a domain-agnostic prompt search."""
    host = rate_limiter.host_of(SEARCH_URL)
    query = f"{family_name} {domain_hint}".strip() if domain_hint else family_name
    for attempt in range(1, 4):
        try:
            rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
            resp = session.get(
                SEARCH_URL,
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrnamespace": 6,
                    "gsrsearch": query,
                    "gsrlimit": 30,
                    "prop": "imageinfo",
                    "iiprop": "url|mime|thumburl|extmetadata|descriptionurl",
                    "iiurlwidth": 1200,
                },
            )
            if resp.text.strip():
                return resp.json()
            login(session, conn, sleep_fn)
            sleep_fn(30)
        except Exception:
            login(session, conn, sleep_fn)
            sleep_fn(30)
    return {}


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    """Download a single image to dest_path. Retries up to 3 times if rate limited.

    Backoff is 60s/120s/180s (or the server's Retry-After, if given) -- Wikimedia's
    CDN throttle windows operate on the scale of minutes, not seconds.
    """
    host = rate_limiter.host_of(url)
    for attempt in range(1, 4):
        rate_limiter.wait_for_cooldown(conn, host, sleep_fn)
        resp = session.get(url, stream=True, timeout=20)

        if resp.status_code == 200:
            return stream_to_file(resp, dest_path)
        elif resp.status_code == 429:
            wait = _retry_after_seconds(resp, fallback=60 * attempt)
            rate_limiter.record_cooldown(conn, host, wait)
        else:
            return False

    return False


def get_search_term(family_name: str) -> str:
    """Extract the core searchable term from a family name."""
    name = re.sub(r"\(.*?\)", "", family_name)
    name = name.split("/")[0]
    return name.strip().lower()


def relevant_pages(pages: dict, family_name: str, domain_keywords: list[str] = SPACE_KEYWORDS) -> list[dict]:
    """Filter raw search result pages to ones actually about family_name.

    Ported from the non-metadata half of old filter_images() -- mime-type check,
    noise words, search-term-or-domain-keyword-in-title. `domain_keywords`
    defaults to SPACE_KEYWORDS (today's behavior); pass [] for a domain-agnostic
    prompt where only the search term itself should have to match.
    """
    search_term = get_search_term(family_name)

    results = []
    for page in pages.values():
        title = page.get("title", "").lower()
        imageinfo = page.get("imageinfo", [{}])[0]
        mime = imageinfo.get("mime", "")

        if "svg" in mime:
            continue
        if not mime.startswith("image/"):
            continue
        if any(word in title for word in NOISE_WORDS):
            continue
        if search_term not in title and not any(keyword in title for keyword in domain_keywords):
            continue

        results.append(page)

    return results


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    """Registry-facing alias: unwraps the full search response, then filters.
    Same as relevant_pages, matching the uniform ImageSource contract's
    relevant_results(raw, query, domain_keywords) shape (see
    scraper/sources/base.py). `domain_keywords=None` means "use SPACE_KEYWORDS",
    this module's own sensible default; pass [] for pure term-matching."""
    pages = raw.get("query", {}).get("pages", {})
    keywords = SPACE_KEYWORDS if domain_keywords is None else domain_keywords
    return relevant_pages(pages, query, domain_keywords=keywords)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _field(extmetadata: dict, key: str) -> str:
    return extmetadata.get(key, {}).get("value", "")


def extract_record(page: dict, family_name: str, retrieved_at: str) -> ImageRecord:
    """Build an ImageRecord from one page of a Commons imageinfo search response.

    `page` is one value from `response["query"]["pages"]`, with
    `iiprop=url|mime|thumburl|extmetadata` already requested.
    """
    imageinfo = page.get("imageinfo", [{}])[0]
    extmetadata = imageinfo.get("extmetadata", {})

    attribution_required_raw = _field(extmetadata, "AttributionRequired")
    attribution_required = attribution_required_raw.strip().lower() == "true" if attribution_required_raw else None

    full_url = imageinfo.get("url", "")
    download_url = imageinfo.get("thumburl") or full_url
    source_url = imageinfo.get("descriptionurl") or full_url

    return ImageRecord(
        family=family_name,
        source_module=SOURCE_MODULE,
        external_id=page.get("title", ""),
        source_url=source_url,
        download_url=download_url,
        title=page.get("title", ""),
        description=_strip_html(_field(extmetadata, "ImageDescription")),
        author=_field(extmetadata, "Artist"),
        license_short_name=_field(extmetadata, "LicenseShortName"),
        license_url=_field(extmetadata, "LicenseUrl") or None,
        attribution_required=attribution_required,
        credit=_field(extmetadata, "Credit") or None,
        retrieved_at=retrieved_at,
        mime=imageinfo.get("mime", ""),
    )
