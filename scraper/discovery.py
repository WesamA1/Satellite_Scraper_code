"""Phase 2: candidate source discovery.

Two ways to search, picked by DISCOVERY_PROVIDER in .env:
- "anthropic" (the default): Claude with its web search tool, via the
  Anthropic API (needs ANTHROPIC_API_KEY).
- "local": a local model plus a self-hosted SearXNG search, for the lab
  computer (scraper/local_discovery.py; setup in docs/local-discovery.md).

Either way, off-limits sites (scraper/sources/off_limits.yaml) never come
back as candidates, and every result lands as a pending candidate for a
person to approve or reject.

No real API call happens from importing or testing this module -- `client` is
only constructed (lazily, via anthropic.Anthropic()) if the caller doesn't
inject one, so offline tests always inject a fake and never touch the network
or need ANTHROPIC_API_KEY set.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

SEED_CANDIDATES_PATH = Path(__file__).parent / "sources" / "seed_candidates.yaml"
OFF_LIMITS_PATH = Path(__file__).parent / "sources" / "off_limits.yaml"
PROVIDERS = ("anthropic", "local")

DISCOVERY_MODEL = os.environ.get("ANTHROPIC_DISCOVERY_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You help find websites that host images matching a search topic, \
for a dataset that legally requires per-image open-license metadata -- like Wikimedia \
Commons' extmetadata or NASA's images-api.nasa.gov copyright field. A site only \
qualifies if it exposes machine-readable, per-image license information via an API \
or structured page markup. A general "images are free to use" claim on a terms page \
does NOT qualify unless it's actually verifiable per image, automatically.

For each candidate site you find, research whether it meets that bar. Respond with \
ONLY a JSON array (no prose, no markdown fences), each item shaped exactly like:
{"name": str, "url": str, "api_url": str or null, "license_model": str, \
"license_verifiable": bool, "notes": str}

"notes" should explain *why* license_verifiable is true or false -- what specific \
field or mechanism would let a scraper check the license of one image at a time, or \
why no such mechanism exists."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json_array(text: str) -> list[dict]:
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON array found in model response: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def provider() -> str:
    """Which search Discovery uses: DISCOVERY_PROVIDER in .env, "anthropic"
    unless set."""
    from dotenv import load_dotenv

    load_dotenv()
    name = os.environ.get("DISCOVERY_PROVIDER", "").strip().lower() or "anthropic"
    if name not in PROVIDERS:
        raise RuntimeError(f"DISCOVERY_PROVIDER must be one of {', '.join(PROVIDERS)}, not {name!r}.")
    return name


def provider_info() -> dict:
    """What Discovery and code drafting will use, for the Discovery page."""
    try:
        name = provider()
    except RuntimeError as e:
        return {"provider": None, "problem": str(e)}
    if name == "anthropic":
        from scraper.codegen import CODEGEN_MODEL

        ready = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        return {
            "provider": "anthropic",
            "model": DISCOVERY_MODEL,
            "codegen_model": CODEGEN_MODEL,
            "search": "Claude's web search",
            "problem": None if ready else "ANTHROPIC_API_KEY isn't set in .env.",
        }
    url = os.environ.get("LOCAL_LLM_URL", "").strip()
    model = os.environ.get("LOCAL_LLM_MODEL", "").strip()
    search = os.environ.get("SEARXNG_URL", "").strip()
    missing = [k for k, v in (("LOCAL_LLM_URL", url), ("LOCAL_LLM_MODEL", model), ("SEARXNG_URL", search)) if not v]
    return {
        "provider": "local",
        "model": f"{model} at {url}" if model and url else model or None,
        "codegen_model": os.environ.get("LOCAL_CODEGEN_MODEL", "").strip() or model or None,
        "search": f"SearXNG at {search}" if search else None,
        "problem": f"Missing from .env: {', '.join(missing)}." if missing else None,
    }


def load_off_limits(path: Path = OFF_LIMITS_PATH) -> list[dict]:
    """[{domain, name, reason}] -- sites Discovery must never suggest."""
    return (yaml.safe_load(path.read_text()) or []) if path.is_file() else []


def off_limits_reason(url: str, off_limits: list[dict] | None = None) -> str | None:
    host = (urlparse(url).netloc or url).lower().split(":")[0].removeprefix("www.")
    for entry in load_off_limits() if off_limits is None else off_limits:
        domain = entry["domain"].lower()
        if host == domain or host.endswith("." + domain):
            return f"{entry.get('name', domain)}: {entry.get('reason', 'off-limits')}"
    return None


def find_candidate_sites(
    prompt: str, client=None, max_uses: int = 5, conn: sqlite3.Connection | None = None, log=None
) -> list[dict]:
    """Candidate image sources for `prompt`, from whichever provider is set
    (see provider()). Returns a list of dicts: name, url, api_url,
    license_model, license_verifiable, notes. Off-limits sites are dropped
    whatever finds them. `conn`, when given, lets the local search skip
    sites already on the candidate list. Raises if the answer can't be
    read -- callers should surface that rather than silently returning an
    empty list, since "found nothing" and "couldn't parse the response" are
    different failures."""
    log = log or (lambda message: None)
    off_limits = load_off_limits()
    if client is None and provider() == "local":
        from scraper import local_discovery

        existing = set()
        if conn is not None:
            existing = {
                (urlparse(r["url"]).netloc or "").lower().removeprefix("www.")
                for r in conn.execute("SELECT url FROM candidate_sources")
            } - {""}
        found = local_discovery.find_candidate_sites(
            prompt, off_limits=off_limits, existing_hosts=existing, max_searches=max_uses, log=log
        )
    else:
        found = _claude_candidate_sites(prompt, client, max_uses)
    kept = []
    for candidate in found:
        reason = off_limits_reason(candidate.get("url", ""), off_limits)
        if reason:
            log(f"Dropped {candidate.get('name') or candidate.get('url')}: off-limits ({reason}).")
        else:
            kept.append(candidate)
    return kept


def _claude_candidate_sites(prompt: str, client, max_uses: int) -> list[dict]:
    """Claude's web_search tool researches candidate image sources."""
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in .env -- discovery needs a Console API key "
                "(see .env.example). This feature is optional; core scraping doesn't need it."
            )
        import anthropic

        client = anthropic.Anthropic()

    response = client.messages.create(
        model=DISCOVERY_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Find 3-6 candidate image sources for: {prompt}"}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
    )

    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    return _extract_json_array(text)


def persist_candidates(conn: sqlite3.Connection, prompt: str, candidates: list[dict]) -> list[int]:
    ids = []
    now = _now()
    for candidate in candidates:
        cur = conn.execute(
            """INSERT INTO candidate_sources
               (prompt, name, url, api_url, license_model, license_verifiable, notes, status, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                prompt,
                candidate.get("name", ""),
                candidate.get("url", ""),
                candidate.get("api_url"),
                candidate.get("license_model", ""),
                int(bool(candidate.get("license_verifiable", False))),
                candidate.get("notes", ""),
                now,
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def add_manual_candidate(
    conn: sqlite3.Connection,
    url: str,
    name: str = "",
    license_model: str = "",
    license_verifiable: bool = False,
    notes: str = "",
) -> int:
    """Lets a human paste in a known candidate source directly, skipping the
    web-search step. Still lands as a 'pending' row through the same
    persist_candidates() path an automated search uses, so it goes through
    the exact same approve/reject -> codegen -> review -> promote gates --
    pasting a URL here never auto-adds it to the workflow.
    license_verifiable is the human's own claim (there's no model judging it
    for a manual entry), so it's only ever a hint in the candidates table,
    never a bypass of promotion.py's shape check."""
    ids = persist_candidates(
        conn,
        "manual entry",
        [
            {
                "name": name.strip() or urlparse(url).netloc or url,
                "url": url,
                "license_model": license_model,
                "license_verifiable": license_verifiable,
                "notes": notes,
            }
        ],
    )
    return ids[0]


def load_seed_candidates(conn: sqlite3.Connection, path: Path = SEED_CANDIDATES_PATH) -> list[int]:
    """Loads scraper/sources/seed_candidates.yaml's hand-maintained list of
    known candidate sites into candidate_sources as 'pending' rows, through
    the same persist_candidates() path an automated search uses -- still
    needs the normal approve/reject -> codegen -> review -> promote gates.
    Safe to call repeatedly: a URL already present (from a prior seed load,
    a search, or a manual paste) is skipped rather than duplicated."""
    seed_entries = yaml.safe_load(path.read_text()) or []
    existing_urls = {row["url"] for row in conn.execute("SELECT url FROM candidate_sources")}

    new_entries = [entry for entry in seed_entries if entry["url"] not in existing_urls]
    if not new_entries:
        return []
    return persist_candidates(conn, "seed list", new_entries)


def review_candidate(conn: sqlite3.Connection, candidate_id: int, approved: bool) -> None:
    conn.execute(
        "UPDATE candidate_sources SET status = ?, reviewed_at = ? WHERE id = ?",
        ("approved" if approved else "rejected", _now(), candidate_id),
    )
    conn.commit()


def seed_queue(conn: sqlite3.Connection, families: list[str]) -> None:
    """Ensures every family has a discovery_queue row. Idempotent -- safe to
    call before every batch, so families added to the catalog later (or a
    fresh queue) are picked up without resetting families already searched."""
    conn.executemany(
        "INSERT OR IGNORE INTO discovery_queue (family, searched_at, candidate_count) VALUES (?, NULL, 0)",
        [(family,) for family in families],
    )
    conn.commit()


def next_batch(conn: sqlite3.Connection, batch_size: int, only: set[str] | None = None) -> list[str]:
    """Returns up to `batch_size` families that have never been auto-searched,
    in a stable order so repeated calls walk the queue rather than re-picking
    the same families. `only` limits it to those families (e.g. the ones with
    no images yet)."""
    rows = conn.execute("SELECT family FROM discovery_queue WHERE searched_at IS NULL ORDER BY family").fetchall()
    families = [row["family"] for row in rows if only is None or row["family"] in only]
    return families[:batch_size]


def families_without_images(conn: sqlite3.Connection, families: list[str]) -> set[str]:
    """Catalog families with no active image yet -- the gaps worth finding new
    sources for."""
    have = {row[0] for row in conn.execute("SELECT DISTINCT family FROM images WHERE status = 'active'")}
    return set(families) - have


def mark_searched(conn: sqlite3.Connection, family: str, candidate_count: int) -> None:
    conn.execute(
        "UPDATE discovery_queue SET searched_at = ?, candidate_count = ? WHERE family = ?",
        (_now(), candidate_count, family),
    )
    conn.commit()


def queue_status(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) AS n FROM discovery_queue").fetchone()["n"]
    searched = conn.execute("SELECT COUNT(*) AS n FROM discovery_queue WHERE searched_at IS NOT NULL").fetchone()["n"]
    return {"total": total, "searched": searched, "remaining": total - searched}
