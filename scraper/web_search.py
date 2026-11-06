"""Web search for local Discovery, through a SearXNG instance you run yourself.

SearXNG is a free, self-hosted metasearch service (no API key). Discovery
asks it for JSON: GET <SEARXNG_URL>/search?q=...&format=json. The JSON
format has to be switched on in SearXNG's settings.yml (search -> formats:
[html, json]); docs/local-discovery.md has a ready-made setup.

Settings come from .env:
    SEARXNG_URL    e.g. http://localhost:8888

Another search service (Brave's API, say) only needs a class with the same
search(query, limit) method.
"""

import os
from dataclasses import dataclass

import requests
from dotenv import load_dotenv

load_dotenv()


class SearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


@dataclass
class SearXNG:
    base_url: str
    timeout: float = 30
    session: requests.Session | None = None

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        self.session = self.session or requests.Session()

    @property
    def label(self) -> str:
        return f"SearXNG at {self.base_url}"

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        try:
            resp = self.session.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "safesearch": 1},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise SearchError(
                f"Couldn't reach SearXNG at {self.base_url} ({e.__class__.__name__}). Is it running, "
                "and is SEARXNG_URL right?"
            ) from e
        if resp.status_code == 403:
            raise SearchError(
                f"SearXNG at {self.base_url} refused a JSON search (HTTP 403). Turn on the json format in "
                "its settings.yml (search -> formats: [html, json])."
            )
        if resp.status_code != 200:
            raise SearchError(f"SearXNG at {self.base_url} answered HTTP {resp.status_code}.")
        try:
            items = resp.json().get("results", [])
        except ValueError as e:
            raise SearchError(f"SearXNG at {self.base_url} didn't answer with JSON.") from e
        return [
            SearchResult(item.get("url", ""), item.get("title", ""), item.get("content", ""))
            for item in items[:limit]
            if item.get("url", "").startswith(("http://", "https://"))
        ]


def from_env() -> SearXNG:
    url = os.environ.get("SEARXNG_URL", "").strip()
    if not url:
        raise SearchError("Local Discovery needs SEARXNG_URL in .env (see docs/local-discovery.md).")
    return SearXNG(url)
