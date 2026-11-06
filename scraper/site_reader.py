"""Reads a candidate site's pages for local Discovery: its main page and its
terms, license or API pages, as plain text for the local model to judge.

Polite by design, since these are other people's sites:
- honors robots.txt (a page it disallows isn't read),
- one request at a time, with a pause between requests to the same site,
- timeouts and a size cap, and only HTML/text is read (never images),
- identifies itself with the project's User-Agent.

Nothing here collects images; it only reads what a person would read to
decide whether a site's licensing can be trusted.
"""

import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

PAUSE_SECONDS = 2.0  # between requests to the same site
TIMEOUT_SECONDS = 20
MAX_BYTES = 1_500_000
MAX_TEXT_CHARS = 8000  # per page, what the model gets to read

# Links worth following from a site's page to judge its licensing.
POLICY_WORDS = (
    "terms",
    "license",
    "licence",
    "copyright",
    "rights",
    "usage",
    "reuse",
    "re-use",
    "permission",
    "image use",
    "using our images",
    "legal",
    "api",
    "developer",
    "open access",
)


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, link text)


class _TextAndLinks(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self._href = urljoin(self.base_url, href)
                self._link_text = []
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_page(url: str, html: str) -> Page:
    parser = _TextAndLinks(url)
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    text = "\n".join(line for line in lines if line)
    return Page(url, " ".join(parser.title.split()), text[:MAX_TEXT_CHARS], parser.links)


def policy_links(page: Page, limit: int = 3) -> list[str]:
    """Links on `page` that look like its terms, license or API pages, on the
    same site, most likely first."""
    host = urlparse(page.url).netloc
    scored = []
    for url, text in page.links:
        if urlparse(url).netloc != host or url.rstrip("/") == page.url.rstrip("/"):
            continue
        haystack = f"{text} {urlparse(url).path}".lower()
        score = sum(word in haystack for word in POLICY_WORDS)
        if score:
            scored.append((-score, url))
    seen, out = set(), []
    for _, url in sorted(scored):
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out[:limit]


class SiteReader:
    def __init__(self, user_agent: str, session=None, sleep_fn=time.sleep, clock=time.monotonic):
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.sleep_fn = sleep_fn
        self.clock = clock
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}

    def _pace(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = PAUSE_SECONDS - (self.clock() - last)
            if wait > 0:
                self.sleep_fn(wait)
        self._last_request[host] = self.clock()

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            rules = robotparser.RobotFileParser()
            try:
                self._pace(parts.netloc)
                resp = self.session.get(f"{root}/robots.txt", timeout=TIMEOUT_SECONDS)
                rules.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except requests.RequestException:
                rules.parse([])  # no robots.txt to be had: nothing disallowed
            self._robots[root] = rules
        return self._robots[root].can_fetch(self.user_agent, url)

    def read(self, url: str) -> Page | None:
        """The page as text, or None when robots.txt disallows it, it isn't
        HTML/text, or it can't be fetched."""
        if not self.allowed(url):
            return None
        self._pace(urlparse(url).netloc)
        try:
            resp = self.session.get(url, timeout=TIMEOUT_SECONDS, stream=True)
            if resp.status_code != 200:
                return None
            kind = resp.headers.get("Content-Type", "text/html").lower()
            if not any(t in kind for t in ("html", "text/plain", "xml", "json")):
                return None
            body = b""
            for chunk in resp.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            resp.close()
        except requests.RequestException:
            return None
        html = body.decode(resp.encoding or "utf-8", errors="replace")
        if "json" in kind or "text/plain" in kind:
            return Page(resp.url or url, "", html[:MAX_TEXT_CHARS], [])
        return html_to_page(resp.url or url, html)
