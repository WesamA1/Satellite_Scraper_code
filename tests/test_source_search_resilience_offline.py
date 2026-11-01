"""Offline tests for a real bug found by live testing: wikidata and
wikipedia_multilang's search_images() made their final Commons imageinfo
lookup with no try/except around it, unlike every preliminary request in the
same functions. download.py's orchestration (_download_from_source) has no
exception handling of its own, so an exception escaping search_images for
any single family would abort the *entire* scrape run, not just skip that
one family/source. These tests simulate that final request returning
malformed content and confirm search_images degrades to {} instead of
raising.
"""

import json

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.sources import wikidata_norad_cospar_id_linking as wikidata
from scraper.sources import wikipedia_multi_language_satellite_articles as wiki_multi


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeResponse:
    def __init__(self, json_data=None, text="ok"):
        self._json = json_data
        self.text = text if json_data is None else json.dumps(json_data)

    def json(self):
        if self._json is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


def test_wikidata_search_images_survives_a_broken_final_lookup(conn):
    calls = []

    class FlakySession:
        def get(self, url, params=None, **kwargs):
            calls.append(params.get("action"))
            if params.get("action") == "wbsearchentities":
                return FakeResponse({"search": [{"id": "Q1"}]})
            if params.get("action") == "wbgetclaims":
                return FakeResponse({"claims": {"P18": [{"mainsnak": {"datavalue": {"value": "Example.jpg"}}}]}})
            # The final Commons imageinfo lookup -- this is the request that
            # had no error handling. Simulate a non-JSON/garbled response.
            return FakeResponse(json_data=None, text="not json")

    raw = wikidata.search_images(FlakySession(), conn, "STARLINK", sleep_fn=lambda s: None)

    assert raw == {}
    assert calls == ["wbsearchentities", "wbgetclaims", "query"]


def test_wikipedia_multilang_search_images_survives_a_broken_final_lookup(conn, monkeypatch):
    monkeypatch.setattr(wiki_multi, "LANGUAGE_WIKIS", ["en"])
    calls = []

    class FlakySession:
        def get(self, url, params=None, **kwargs):
            calls.append((url, params.get("titles")))
            if params.get("list") == "search":
                return FakeResponse({"query": {"search": [{"title": "Starlink"}]}})
            if params.get("prop") == "images":
                return FakeResponse({"query": {"pages": {"1": {"images": [{"title": "File:Starlink-1.jpg"}]}}}})
            # The final combined Commons imageinfo lookup -- this is the
            # request that had no error handling.
            return FakeResponse(json_data=None, text="not json")

    raw = wiki_multi.search_images(FlakySession(), conn, "STARLINK", sleep_fn=lambda s: None)

    assert raw == {}
    assert len(calls) == 3  # search, images, then the broken final lookup
