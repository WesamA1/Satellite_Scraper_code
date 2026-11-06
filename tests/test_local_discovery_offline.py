"""Offline tests for Discovery on local models: the OpenAI-style model client,
the SearXNG client, the polite site reader, the local discovery steps, local
code drafting, the health check, and searching only families with no images.
Fake HTTP sessions stand in for the model server, SearXNG and the sites; no
network."""

import json
from pathlib import Path

import pytest
import requests

from scraper import codegen, discovery, doctor, jobs, llm, local_discovery, site_reader, web_search
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class Resp:
    def __init__(self, status=200, data=None, text="", headers=None, url=""):
        self.status_code = status
        self._data = data
        self.text = text if data is None else json.dumps(data)
        self.headers = headers or {"Content-Type": "text/html"}
        self.url = url
        self.encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data

    def iter_content(self, size):
        yield self.text.encode()

    def close(self):
        pass


# ---- The model client ----


def test_answers_are_cleaned_and_read_as_json():
    assert llm.strip_reasoning("<think>hmm\nlong</think>\n  Done ") == "Done"
    assert llm.parse_json('<think>x</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Sure! Here you go: [{"b": 2}] Hope that helps.') == [{"b": 2}]
    with pytest.raises(llm.LLMError):
        llm.parse_json("no json here")


class ModelServer:
    """Answers /chat/completions with the queued replies, recording requests."""

    def __init__(self, *replies, reject_schema=False):
        self.replies = list(replies)
        self.reject_schema = reject_schema
        self.requests = []
        self.headers = {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append(json)
        if self.reject_schema and "response_format" in json:
            return Resp(400, text="response_format not supported")
        return Resp(200, {"choices": [{"message": {"content": self.replies.pop(0)}}]})

    def get(self, url, headers=None, timeout=None):
        return Resp(200, {"data": [{"id": "qwen3:32b"}, {"id": "qwen3-coder:30b"}]})


def test_chat_asks_for_the_schema_and_falls_back_when_the_server_cant():
    server = ModelServer('{"queries": ["a"]}')
    model = llm.LocalLLM("http://lab:11434/v1/", "qwen3:32b", session=server)
    assert model.chat_json("sys", "user", {"type": "object"}) == {"queries": ["a"]}
    sent = server.requests[0]
    assert sent["model"] == "qwen3:32b" and sent["response_format"]["type"] == "json_schema"
    assert sent["messages"][0] == {"role": "system", "content": "sys"}

    old_server = ModelServer('Here: {"ok": true}', '{"ok": true}', reject_schema=True)
    model = llm.LocalLLM("http://lab:8000/v1", "m", session=old_server)
    assert model.chat_json("s", "u", {"type": "object"}) == {"ok": True}
    assert "response_format" not in old_server.requests[1]
    model.chat_json("s", "u", {"type": "object"})
    assert "response_format" not in old_server.requests[2]  # remembered: not asked again


def test_unreachable_server_and_missing_settings_say_what_to_fix(monkeypatch):
    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("refused")

    with pytest.raises(llm.LLMError, match="Is Ollama/vLLM running"):
        llm.LocalLLM("http://lab:11434/v1", "m", session=Down()).chat("s", "u")
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    with pytest.raises(llm.LLMError, match="LOCAL_LLM_URL"):
        llm.from_env()
    monkeypatch.setenv("LOCAL_LLM_URL", "http://lab:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3:32b")
    monkeypatch.setenv("LOCAL_CODEGEN_MODEL", "qwen3-coder:30b")
    assert llm.from_env().model == "qwen3:32b" and llm.from_env("codegen").model == "qwen3-coder:30b"


# ---- SearXNG ----


def test_searxng_results_and_errors():
    class Search:
        def __init__(self, resp):
            self.resp, self.params = resp, None

        def get(self, url, params=None, timeout=None):
            self.params = params
            return self.resp

    ok = Search(
        Resp(200, {"results": [{"url": "https://a.org/x", "title": "A", "content": "snip"}, {"url": "ftp://b"}]})
    )
    (hit,) = web_search.SearXNG("http://lab:8888/", session=ok).search("landsat")
    assert (hit.url, hit.title, hit.snippet) == ("https://a.org/x", "A", "snip")
    assert ok.params["format"] == "json" and ok.params["q"] == "landsat"
    with pytest.raises(web_search.SearchError, match="json format"):
        web_search.SearXNG("http://lab:8888", session=Search(Resp(403, text="Forbidden"))).search("x")


# ---- Reading sites ----

HOME = """<html><head><title>Space Archive</title><script>var x=1;</script></head><body>
<h1>Space Archive</h1><p>Thousands of satellite photos.</p>
<a href="/about/terms-of-use">Terms of use</a> <a href="/api/docs">Developer API</a>
<a href="https://elsewhere.com/license">License</a> <a href="/gallery">Gallery</a></body></html>"""


def test_html_becomes_text_and_policy_links():
    page = site_reader.html_to_page("https://archive.example/", HOME)
    assert page.title == "Space Archive" and "Thousands of satellite photos." in page.text
    assert "var x" not in page.text
    # Same site only (not elsewhere.com), not the gallery; "Developer API" matches two words, so it ranks first.
    assert site_reader.policy_links(page) == [
        "https://archive.example/api/docs",
        "https://archive.example/about/terms-of-use",
    ]


class Sites:
    def __init__(self, pages, robots=""):
        self.pages, self.robots, self.fetched, self.headers = pages, robots, [], {}

    def get(self, url, timeout=None, stream=False):
        self.fetched.append(url)
        if url.endswith("/robots.txt"):
            return Resp(200, text=self.robots, headers={"Content-Type": "text/plain"})
        if url in self.pages:
            body, kind = self.pages[url]
            return Resp(200, text=body, headers={"Content-Type": kind}, url=url)
        return Resp(404)


def test_reader_honors_robots_paces_requests_and_reads_only_web_pages():
    sleeps = []
    clock = iter(range(0, 1000, 1)).__next__  # one "second" per call
    sites = Sites(
        {
            "https://archive.example/": (HOME, "text/html; charset=utf-8"),
            "https://archive.example/photo.jpg": ("binary", "image/jpeg"),
            "https://archive.example/private/x": ("secret", "text/html"),
        },
        robots="User-agent: *\nDisallow: /private/\n",
    )
    reader = site_reader.SiteReader("TestAgent/1.0", session=sites, sleep_fn=sleeps.append, clock=clock)
    assert reader.read("https://archive.example/").title == "Space Archive"
    assert reader.read("https://archive.example/photo.jpg") is None  # not a web page
    assert reader.read("https://archive.example/private/x") is None  # robots.txt says no
    assert "https://archive.example/private/x" not in sites.fetched
    assert sites.fetched.count("https://archive.example/robots.txt") == 1
    assert sleeps and all(s > 0 for s in sleeps)  # paused between requests to the same site
    assert sites.headers["User-Agent"] == "TestAgent/1.0"


# ---- The local discovery steps ----


class FakeSearch:
    label = "fake search"

    def __init__(self, results):
        self.results, self.queries = results, []

    def search(self, query, limit=10):
        self.queries.append(query)
        return [web_search.SearchResult(url, title, "") for url, title in self.results]


class FakeReader:
    def __init__(self, pages):
        self.pages, self.read_urls = pages, []

    def read(self, url):
        self.read_urls.append(url)
        if url not in self.pages:
            return None
        return site_reader.html_to_page(url, self.pages[url])


class Judge:
    """A fake local model: queries first, then one verdict per site."""

    model = "fake-model"

    def __init__(self, queries, verdicts):
        self.queries, self.verdicts, self.prompts = queries, verdicts, []

    def chat_json(self, system, user, schema, **kwargs):
        self.prompts.append(user)
        if "queries" in schema["properties"]:
            if self.queries is None:
                raise llm.LLMError("junk")
            return {"queries": self.queries}
        host = user.split("Website: ")[1].split("\n")[0]
        return self.verdicts[host]


def _verdict(**overrides):
    v = {
        "name": "Space Archive",
        "hosts_relevant_images": True,
        "api_url": "https://archive.example/api",
        "license_model": "CC BY 4.0 per image",
        "license_verifiable": True,
        "ai_training_prohibited": False,
        "notes": "Each record's API response has a license field.",
    }
    return {**v, **overrides}


def test_local_discovery_reads_judges_and_filters():
    search = FakeSearch(
        [
            ("https://space.skyrocket.de/doc_sdat/x.htm", "Gunter's"),
            ("https://commons.wikimedia.org/wiki/File:Sat.jpg", "Commons"),
            ("https://www.shutterstock.com/search/satellite", "Stock"),
            ("https://old.example/x", "Already a candidate"),
            ("https://archive.example/", "Space Archive"),
            ("https://noai.example/sat", "No AI"),
            ("https://cats.example/", "Cats"),
        ]
    )
    reader = FakeReader(
        {
            "https://archive.example/": HOME,
            "https://archive.example/about/terms-of-use": "<p>Images are CC BY 4.0, stated per image.</p>",
            "https://archive.example/api/docs": "<p>GET /api/search returns license per item.</p>",
            "https://noai.example/sat": "<p>Satellites. <a href='/terms'>Terms</a></p>",
            "https://noai.example/terms": "<p>No use for training artificial intelligence.</p>",
            "https://cats.example/": "<p>Cat photos.</p>",
        }
    )
    judge = Judge(
        ["landsat images public domain", "landsat photo archive api"],
        {
            "archive.example": _verdict(),
            "noai.example": _verdict(name="No AI", ai_training_prohibited=True),
            "cats.example": _verdict(name="Cats", hosts_relevant_images=False),
        },
    )
    logs = []
    off_limits = discovery.load_off_limits()

    found = local_discovery.find_candidate_sites(
        "LANDSAT satellite",
        llm=judge,
        search=search,
        reader=reader,
        off_limits=off_limits,
        existing_hosts={"old.example"},
        log=logs.append,
    )

    (candidate,) = found
    assert candidate["name"] == "Space Archive" and candidate["url"] == "https://archive.example/"
    assert candidate["license_verifiable"] is True and candidate["api_url"] == "https://archive.example/api"
    assert "pages read: https://archive.example/, " in candidate["notes"]
    assert "about/terms-of-use" in candidate["notes"] and "api/docs" in candidate["notes"]
    assert search.queries == ["landsat images public domain", "landsat photo archive api"]
    assert not any("skyrocket" in url or "wikimedia" in url or "shutterstock" in url for url in reader.read_urls)
    text = " ".join(logs)
    assert "space.skyrocket.de (off-limits" in text and "commons.wikimedia.org (already a source)" in text
    assert (
        "shutterstock.com (search engine, social or stock site)" in text and "old.example (already a candidate)" in text
    )
    assert "noai.example: turned away" in text and "cats.example: doesn't seem to host images" in text
    assert "CC BY 4.0, stated per image" in judge.prompts[1]  # the terms page was part of what it judged


def test_standard_queries_when_the_model_cant_write_them():
    search = FakeSearch([])
    local_discovery.find_candidate_sites("GPS IIR", llm=Judge(None, {}), search=search, reader=FakeReader({}))
    assert search.queries[0] == "GPS IIR images public domain" and len(search.queries) == 3


def test_provider_choice_and_off_limits_for_both_providers(conn, monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "local")
    calls = {}

    def fake_local(prompt, **kwargs):
        calls.update(kwargs)
        return [
            {"name": "Good", "url": "https://good.example/"},
            {"name": "Gunter", "url": "https://space.skyrocket.de/"},
        ]

    monkeypatch.setattr(local_discovery, "find_candidate_sites", fake_local)
    discovery.persist_candidates(conn, "p", [{"name": "Old", "url": "https://www.old.example/page"}])
    found = discovery.find_candidate_sites("x", conn=conn)
    assert [c["name"] for c in found] == ["Good"]
    assert calls["existing_hosts"] == {"old.example"}

    class Claude:
        class messages:
            @staticmethod
            def create(**kwargs):
                text = json.dumps(
                    [
                        {"name": "G", "url": "https://www.space.skyrocket.de/x"},
                        {"name": "N", "url": "https://n.example"},
                    ]
                )
                import types

                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)])

    monkeypatch.setenv("DISCOVERY_PROVIDER", "anthropic")
    assert [c["name"] for c in discovery.find_candidate_sites("x", client=Claude())] == ["N"]

    monkeypatch.setenv("DISCOVERY_PROVIDER", "nonsense")
    with pytest.raises(RuntimeError, match="DISCOVERY_PROVIDER"):
        discovery.provider()


def test_provider_info(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://lab:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3:32b")
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.delenv("LOCAL_CODEGEN_MODEL", raising=False)
    info = discovery.provider_info()
    assert info["provider"] == "local" and info["model"] == "qwen3:32b at http://lab:11434/v1"
    assert info["codegen_model"] == "qwen3:32b" and info["problem"] == "Missing from .env: SEARXNG_URL."


# ---- Local code drafting ----


def test_local_draft_is_cleaned_and_checked(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "local")
    good = ModelServer(
        "<think>plan</think>\n```python\nSOURCE_MODULE = 'x'\n\ndef make_session(dev=False):\n    pass\n```"
    )
    model = llm.LocalLLM("http://lab/v1", "coder", session=good)
    source = codegen.generate_source_module({"name": "X"}, llm=model)
    assert source == "SOURCE_MODULE = 'x'\n\ndef make_session(dev=False):\n    pass\n"
    assert "license_short_name" in good.requests[0]["messages"][0]["content"]  # same instructions as Claude gets

    broken = llm.LocalLLM("http://lab/v1", "coder", session=ModelServer("def make_session(:\n"))
    with pytest.raises(codegen.DraftError, match="isn't valid Python"):
        codegen.generate_source_module({"name": "X"}, llm=broken)


# ---- Health check ----


def test_doctor_checks_the_local_model_and_search(monkeypatch):
    monkeypatch.setenv("DISCOVERY_PROVIDER", "local")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://lab:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3:32b")
    monkeypatch.setenv("LOCAL_CODEGEN_MODEL", "llama-coder")
    monkeypatch.setenv("SEARXNG_URL", "http://lab:8888")
    model = llm.LocalLLM("http://lab:11434/v1", "qwen3:32b", session=ModelServer())
    by_name = {r.name: r for r in doctor.check_discovery(llm=model, search=FakeSearch([("https://a.org", "A")]))}
    assert by_name["discovery"].status == "pass"
    assert (
        by_name["discovery:local_model"].status == "fail" and "llama-coder" in by_name["discovery:local_model"].detail
    )
    assert by_name["discovery:search"].status == "pass"

    monkeypatch.setenv("DISCOVERY_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (only,) = doctor.check_discovery()
    assert only.status == "warn" and "ANTHROPIC_API_KEY" in only.detail


# ---- Searching only the gaps ----


def test_auto_discovery_can_search_only_families_without_images(conn, tmp_path, monkeypatch):
    catalog = tmp_path / "satellites.csv"
    catalog.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n1,a,AAA,AAA\n2,b,BBB,BBB\n3,c,CCC,CCC\n")
    conn.execute("INSERT INTO images (family, path, status, created_at) VALUES ('AAA', 'a.jpg', 'active', 'now')")
    conn.commit()
    searched = []
    monkeypatch.setattr(jobs.discovery, "find_candidate_sites", lambda prompt, **kwargs: searched.append(prompt) or [])
    result = jobs.run_auto_discovery_job(conn, catalog, 5, log=lambda m: None, only_missing=True)
    assert result["searched"] == 2 and not any("AAA" in p for p in searched)
    assert jobs.run_auto_discovery_job(conn, catalog, 5, log=lambda m: None, only_missing=True)["searched"] == 0
    assert Path(catalog).is_file()
