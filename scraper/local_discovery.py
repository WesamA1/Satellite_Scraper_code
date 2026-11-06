"""Discovery with a local model: finds candidate image sources without the
Anthropic API, for running on the lab computer.

Claude's version researches on its own with a web search tool. Smaller local
models do that unreliably, so this runs fixed steps instead, each one small:

1. The model writes a few search queries for the topic.
2. SearXNG runs them. Off-limits sites, sites the scraper already uses,
   general image and stock sites, and sites already on the candidate list
   are dropped.
3. Each remaining site's page, plus its terms / license / API pages, is read
   (politely: see scraper/site_reader.py).
4. The model judges each site from what was read, answering in a fixed JSON
   shape: does it host images of the topic, is there per-image license data a
   scraper can check, do its terms prohibit AI/ML training.
5. Sites that host relevant images and don't prohibit AI/ML training come
   back as candidates, in the same shape Claude's version returns. They land
   as pending candidates; a person still approves or rejects every one.

The model's verdict is a first reading, not a decision: each candidate's
notes list the pages it read, so the reviewer can check.
"""

from collections import OrderedDict
from typing import Callable, Optional
from urllib.parse import urlparse

from scraper import llm as llm_module
from scraper import site_reader, web_search

MAX_SITES = 6
MAX_POLICY_PAGES = 2

# Sites the scraper already has a source for.
KNOWN_SOURCE_DOMAINS = (
    "wikimedia.org",
    "wikipedia.org",
    "wikidata.org",
    "images.nasa.gov",
    "images-api.nasa.gov",
    "europeana.eu",
    "dp.la",
    "si.edu",
    "flickr.com",
)

# Search engines, social media, shops and stock-photo sites: no per-image
# open license a scraper could check, or a license that isn't an open one.
GENERAL_SITE_NAMES = {
    "google",
    "bing",
    "yahoo",
    "duckduckgo",
    "pinterest",
    "shutterstock",
    "gettyimages",
    "istockphoto",
    "alamy",
    "dreamstime",
    "depositphotos",
    "123rf",
    "freepik",
    "vecteezy",
    "unsplash",
    "pexels",
    "pixabay",
    "facebook",
    "instagram",
    "twitter",
    "x",
    "reddit",
    "youtube",
    "tiktok",
    "amazon",
    "ebay",
    "etsy",
}

QUERIES_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "hosts_relevant_images": {"type": "boolean"},
        "api_url": {"type": ["string", "null"]},
        "license_model": {"type": "string"},
        "license_verifiable": {"type": "boolean"},
        "ai_training_prohibited": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": [
        "name",
        "hosts_relevant_images",
        "api_url",
        "license_model",
        "license_verifiable",
        "ai_training_prohibited",
        "notes",
    ],
    "additionalProperties": False,
}

QUERIES_SYSTEM = """You write web search queries that find websites hosting images of a \
topic under open licenses (public domain, Creative Commons, CC0) with license \
information per image, such as archives, museum collections, space agency image \
libraries and digital libraries with an API. Avoid stock-photo and wallpaper sites. \
Answer with JSON only: {"queries": ["...", "..."]}."""

JUDGE_SYSTEM = """You review one website for a machine-learning image dataset that \
legally requires open-license information for every image. From the page text \
provided (and only that -- don't assume anything it doesn't say), answer:

- hosts_relevant_images: does this site actually host images of the topic itself?
- license_verifiable: can a program check the license of ONE image at a time, \
automatically -- e.g. a license field in an API response, or structured license \
markup on each image's page? A general "our images are free to use" statement on a \
terms page is NOT enough. If unsure, answer false.
- ai_training_prohibited: do the terms forbid using the content for AI or machine \
learning training, text and data mining, or automated collection? If they do, \
answer true.
- api_url: the URL of a public search API for the images, if the text shows one, \
else null.
- license_model: the licenses used, in a few words (e.g. "public domain (US \
government work)", "CC BY-SA 4.0 per image", "all rights reserved").
- notes: two or three sentences on WHY, naming the specific field, page or rule \
that decides license_verifiable and ai_training_prohibited.

Answer with JSON only."""


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def why_skipped(url: str, off_limits: list[dict], existing_hosts: set[str]) -> Optional[str]:
    """Why a search result's site isn't worth reading, or None if it is."""
    host = _host(url)
    for entry in off_limits:
        if _matches(host, entry["domain"]):
            return f"off-limits ({entry.get('reason', 'see off_limits.yaml')})"
    if any(_matches(host, d) for d in KNOWN_SOURCE_DOMAINS):
        return "already a source"
    if GENERAL_SITE_NAMES & set(host.split(".")[:-1]):
        return "search engine, social or stock site"
    if any(_matches(host, h) for h in existing_hosts):
        return "already a candidate"
    return None


def _queries(llm, prompt: str, max_searches: int, log) -> list[str]:
    fallback = [
        f"{prompt} images public domain",
        f"{prompt} image archive creative commons license",
        f"{prompt} photo collection API open access",
    ]
    try:
        answer = llm.chat_json(
            QUERIES_SYSTEM, f"Topic: {prompt}\nWrite {max_searches} different queries.", QUERIES_SCHEMA
        )
        queries = [q.strip() for q in answer.get("queries", []) if isinstance(q, str) and q.strip()]
    except llm_module.LLMError as e:
        log(f"The model couldn't write search queries ({e}); using standard ones.")
        queries = []
    return (queries or fallback)[:max_searches]


def _evidence(reader, first_url: str) -> tuple[list[str], str]:
    """(pages read, their text) for one site: the search result's page (or
    the site's home page), plus a couple of its terms / license / API pages."""
    page = reader.read(first_url)
    if page is None:
        root = f"{urlparse(first_url).scheme}://{urlparse(first_url).netloc}/"
        page = reader.read(root) if root != first_url else None
    if page is None:
        return [], ""
    pages = [page]
    for url in site_reader.policy_links(page, MAX_POLICY_PAGES):
        extra = reader.read(url)
        if extra is not None:
            pages.append(extra)
    text = "\n\n".join(f"=== {p.url} ({p.title or 'untitled'}) ===\n{p.text}" for p in pages)
    return [p.url for p in pages], text


def find_candidate_sites(
    prompt: str,
    llm=None,
    search=None,
    reader=None,
    off_limits: Optional[list[dict]] = None,
    existing_hosts: Optional[set[str]] = None,
    max_searches: int = 5,
    max_sites: int = MAX_SITES,
    log: Callable[[str], None] = lambda message: None,
) -> list[dict]:
    """Candidate sources for `prompt`, as dicts with name, url, api_url,
    license_model, license_verifiable, notes (the shape persist_candidates
    takes)."""
    from scraper.sources.wikimedia import USER_AGENT

    llm = llm or llm_module.from_env("discovery")
    search = search or web_search.from_env()
    reader = reader or site_reader.SiteReader(USER_AGENT)
    off_limits = off_limits if off_limits is not None else []
    existing_hosts = existing_hosts or set()

    queries = _queries(llm, prompt, max_searches, log)
    log(f"Searching: {' | '.join(queries)}")
    sites: "OrderedDict[str, dict]" = OrderedDict()
    skipped: dict[str, str] = {}
    for query in queries:
        for result in search.search(query, limit=10):
            host = _host(result.url)
            if not host or host in skipped:
                continue
            reason = why_skipped(result.url, off_limits, existing_hosts)
            if reason:
                skipped[host] = reason
                continue
            site = sites.setdefault(host, {"url": result.url, "title": result.title, "hits": 0})
            site["hits"] += 1
    if skipped:
        log("Left out: " + ", ".join(f"{h} ({why})" for h, why in skipped.items()))

    ranked = sorted(sites.items(), key=lambda kv: -kv[1]["hits"])[:max_sites]  # stable: ties keep search order
    candidates = []
    for host, site in ranked:
        pages, text = _evidence(reader, site["url"])
        if not pages:
            log(f"{host}: couldn't read it (robots.txt, not a web page, or no answer); skipped.")
            continue
        try:
            verdict = llm.chat_json(
                JUDGE_SYSTEM,
                f"Topic: {prompt}\nWebsite: {host}\n\n{text}",
                JUDGE_SCHEMA,
                max_tokens=1500,
            )
        except llm_module.LLMError as e:
            log(f"{host}: the model's verdict couldn't be read ({e}); skipped.")
            continue
        if verdict.get("ai_training_prohibited"):
            log(f"{host}: turned away, its terms appear to prohibit AI/ML training. {verdict.get('notes', '')}")
            continue
        if not verdict.get("hosts_relevant_images"):
            log(f"{host}: doesn't seem to host images of {prompt!r}; skipped.")
            continue
        notes = (verdict.get("notes") or "").strip()
        candidates.append(
            {
                "name": (verdict.get("name") or site["title"] or host).strip()[:200],
                "url": f"{urlparse(site['url']).scheme}://{urlparse(site['url']).netloc}/",
                "api_url": verdict.get("api_url") or None,
                "license_model": (verdict.get("license_model") or "").strip(),
                "license_verifiable": bool(verdict.get("license_verifiable")),
                "notes": f"{notes} [Local model {getattr(llm, 'model', '')}; pages read: {', '.join(pages)}]".strip(),
            }
        )
        checkable = "license checkable per image" if candidates[-1]["license_verifiable"] else "license not checkable"
        log(f"{host}: candidate ({checkable}).")
    return candidates


if __name__ == "__main__":
    # Smoke test on the lab computer: checks the model server and SearXNG, then
    # runs one search and prints what it would suggest. Writes nothing.
    #   uv run python -m scraper.local_discovery "LANDSAT satellite"
    import sys

    from scraper import discovery, doctor

    for check in doctor.check_discovery():
        print(f"[{check.status.upper():4}] {check.name}: {check.detail}")
    topic = " ".join(sys.argv[1:]) or "LANDSAT satellite"
    results = find_candidate_sites(topic, off_limits=discovery.load_off_limits(), log=print)
    print(f"\n{len(results)} candidate(s) for {topic!r}:")
    for c in results:
        print(f"- {c['name']} {c['url']}")
        print(f"  license: {c['license_model']} (checkable per image: {c['license_verifiable']})")
        print(f"  {c['notes']}")
