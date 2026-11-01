"""USGS source -- INCOMPLETE, needs more research before this is worth
promoting. Drafted by hand (not the Claude codegen path -- ANTHROPIC_API_KEY
wasn't set) by Claude Code directly, same as the other candidates from this
batch, but flagged pending here rather than finished.

Why this one stalls where the others didn't: USGS doesn't expose one unified,
documented API with a clean per-item machine-readable license field the way
NASA's images-api.nasa.gov does. What USGS actually runs is split across
several different systems (EarthExplorer/M2M for Landsat-style earth-
observation data products, which need an authenticated session and aren't
really "photos of satellite hardware" anyway; the separate USGS Photographic
Library, which is simpler but I couldn't confirm a documented public search
API for it without a live fetch). Rather than guess at an endpoint shape I
can't verify and risk writing extract_record logic that silently claims a
public-domain license without a real per-item field backing it, this stub
keeps the ImageSource shape (so registry.validate_shape passes and nothing
crashes) but always returns no results / no license. A human should do
real research here -- find the actual API this should hit -- before this
goes further; see the seed list's own note that this entry was marked
license_verifiable: false for exactly this reason.
"""

import time

import requests

from scraper.sources.base import ImageRecord

SOURCE_MODULE = "usgs"


def make_session(dev: bool = False) -> requests.Session:
    if dev:
        import requests_cache

        session = requests_cache.CachedSession("scraper_dev_cache", expire_after=3600)
    else:
        session = requests.Session()
    session.headers.update({"User-Agent": "SatellitePipeline/1.0 requests/2.31"})
    return session


def search_images(
    session: requests.Session, conn, query: str, sleep_fn=time.sleep, domain_hint: str | None = None
) -> dict:
    """No verified endpoint to call yet -- see the module docstring. Returns
    nothing rather than guessing at a URL that might not even exist."""
    return {}


def relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]:
    return []


def extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None:
    return None


def download_image(session: requests.Session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool:
    return False
