"""Shared data shape for every source's extracted metadata, plus the ImageSource
contract that scraper/sources/registry.py dispatches against.

Phase 2: a source module -- built-in (wikimedia, nasa_images) or promoted from a
Claude-generated candidate (scraper/promotion.py) -- must expose:

    SOURCE_MODULE: str
    make_session(dev: bool = False) -> requests.Session
    search_images(session, conn, query: str, sleep_fn=time.sleep,
                   domain_hint: str | None = None) -> dict
    relevant_results(raw: dict, query: str, domain_keywords: list[str] | None = None) -> list[dict]
        (domain_keywords=None means "use this module's own sensible default";
        an explicit [] means pure term-matching, no domain-specific fallback --
        how a fully generic prompt search disables satellite-specific keywords)
    extract_record(raw_item: dict, query: str, retrieved_at: str) -> ImageRecord | None
    download_image(session, conn, url: str, dest_path, sleep_fn=time.sleep) -> bool

extract_record's license_short_name should come from real, structured per-item
license data the source's own API/page actually returns -- don't guess or
hallucinate a plausible-looking value. An empty string is the correct, honest
answer when that can't be verified, not a bug to work around. There is no
automated runtime gate on this field (scraper/license_gate.py always accepts);
whether a source's licensing is trustworthy is a human's call during Discovery
review -- reading this generated code (and, where useful, visiting the site
itself) before promoting it. Writing a real value here, truthfully, is what
that human review actually depends on.

registry.py checks for FunctionDef/Assign nodes with these names (via ast, never
by importing+running unreviewed code) before registering a dynamically-loaded
module; promotion.py's validate_module_shape runs the same check before a
candidate is promoted into scraper/sources/.
"""

import threading
from dataclasses import dataclass
from pathlib import Path

UNVERIFIED_LICENSE = "Unverified (no license metadata provided)"

DOWNLOAD_STALL_TIMEOUT_SECONDS = 60


def _looks_like_markup(head: bytes) -> bool:
    """Confirmed in production: a source's "download" URL can point at an
    HTML page instead of real media -- a landing/viewer page, a cookie-
    consent interstitial -- and without this, that HTML gets saved to disk
    and recorded as a real image. Caught via one family where ~80% of one
    source's "downloads" were HTML, mislabeled with image extensions (one
    even literally ".jsp") derived from the URL rather than the content.
    No real image or PDF format starts with '<' once leading whitespace is
    stripped; HTML/XML always does."""
    return head.lstrip()[:1] == b"<"


def stream_to_file(resp, dest_path: Path, timeout_seconds: float = DOWNLOAD_STALL_TIMEOUT_SECONDS) -> bool:
    """Writes a streaming (`stream=True`) response body to dest_path, with a
    hard wall-clock deadline independent of requests/urllib3's own read
    timeout -- confirmed in production that a CDN can half-close a
    connection (socket sits in CLOSE-WAIT) without that timeout ever firing,
    which hung a download's `for chunk in resp.iter_content(...)` loop
    forever. That one stuck item then blocked the entire scrape job, since
    nothing downstream could know to give up on it.

    Runs the actual write on a daemon thread and gives up after
    `timeout_seconds` if it hasn't finished -- the abandoned thread and its
    socket are left for the OS to eventually reclaim, but the caller gets
    back a clean False instead of hanging. Removes any partial file so a
    retry never sees corrupt/truncated bytes.

    Also bails out (same clean-False-no-partial-file path) if the first
    chunk looks like HTML/XML markup rather than real media -- see
    _looks_like_markup. Checked only on the first chunk: real image/PDF
    bytes never match, so this never trips on genuine media, however it's
    chunked.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    errors = []

    def write():
        try:
            with open(dest_path, "wb") as f:
                first_chunk = True
                for chunk in resp.iter_content(chunk_size=8192):
                    if first_chunk:
                        if _looks_like_markup(chunk):
                            raise ValueError("response body looks like HTML/XML markup, not real media")
                        first_chunk = False
                    f.write(chunk)
        except Exception as e:
            errors.append(e)

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive() or errors:
        dest_path.unlink(missing_ok=True)
        return False
    return True


@dataclass
class ImageRecord:
    family: str
    source_module: str
    external_id: str  # resume key: Commons title, nasa_id, etc.
    source_url: str  # human-visitable provenance page
    download_url: str
    title: str
    description: str
    author: str
    license_short_name: str  # real value, '', or UNVERIFIED_LICENSE -- recorded as-is, no longer gated at runtime.
    license_url: str | None
    attribution_required: bool | None
    credit: str | None
    retrieved_at: str  # ISO-8601 UTC, when we fetched this metadata
    mime: str
