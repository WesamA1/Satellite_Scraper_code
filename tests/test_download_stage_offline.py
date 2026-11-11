"""Offline tests for Phase 1 Task 1's download stage. No real HTTP.

A fake session stands in for requests.Session: its .get()/.post() return canned
responses keyed on URL, so the real login()/search_images()/download_image()
code in scraper/sources/*.py runs exactly as it will in production.
"""

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper import license_gate
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.sources import nasa_images, wikimedia
from scraper.stages.download import download_family


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tiny_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="blue").save(buffer, format="JPEG")
    return buffer.getvalue()


TINY_JPEG = _tiny_jpeg()  # downloads are checked to really be images


def _jpeg_for(url: str) -> bytes:
    """A different small picture per URL: identical files are now treated as
    one image, so separate items need separate pictures."""
    import hashlib

    r, g, b = hashlib.sha256(url.encode()).digest()[:3]
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color=(r, g, b)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, text="ok", content=None, headers=None):
        content = TINY_JPEG if content is None else content
        self._json = json_data
        self.status_code = status_code
        self.text = text
        self._content = content
        self.headers = headers or {}

    def json(self):
        return self._json

    def iter_content(self, chunk_size=8192):
        yield self._content


WIKIMEDIA_SEARCH_RESPONSE = {
    "query": {
        "pages": {
            "1": {
                "title": "File:Example Satellite.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/wikipedia/commons/example.jpg",
                        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/example.jpg",
                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example_Satellite.jpg",
                        "mime": "image/jpeg",
                        "extmetadata": {
                            "ImageDescription": {"value": "A satellite"},
                            "Artist": {"value": "Jane Photographer"},
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                            "Credit": {"value": "Own work"},
                            "AttributionRequired": {"value": "true"},
                        },
                    }
                ],
            },
            "2": {
                "title": "File:Mission Logo.svg",
                "imageinfo": [
                    {"url": "https://upload.wikimedia.org/logo.svg", "mime": "image/svg+xml", "extmetadata": {}}
                ],
            },
        }
    }
}

WIKIMEDIA_EMPTY_RESPONSE = {"query": {"pages": {}}}

NASA_SEARCH_RESPONSE = {
    "collection": {
        "items": [
            {
                "data": [
                    {
                        "nasa_id": "PIA00001",
                        "title": "Example Family satellite",
                        "description": "Public domain NASA image",
                        "photographer": "NASA/JPL",
                    }
                ],
                "links": [
                    {
                        "href": "https://images-assets.nasa.gov/image/PIA00001/PIA00001~large.jpg",
                        "rel": "preview",
                        "render": "image",
                    }
                ],
            }
        ]
    }
}

NASA_EMPTY_RESPONSE = {"collection": {"items": []}}


class FakeSession:
    def __init__(self, wikimedia_response=None, nasa_response=None, rate_limit_downloads_for=None):
        self.wikimedia_response = wikimedia_response
        self.nasa_response = nasa_response
        self.get_calls = []
        # {url: (times_to_429, retry_after_header)} -- the download URL returns 429
        # this many times (with the given Retry-After header) before succeeding.
        self.rate_limit_downloads_for = rate_limit_downloads_for or {}
        self._rate_limit_hits = {}
        self.same_picture = None  # set to bytes to have every download return that one picture

    def get(self, url, params=None, **kwargs):
        self.get_calls.append(url)
        if url == wikimedia.LOGIN_URL:
            return FakeResponse(json_data={"query": {"tokens": {"logintoken": "faketoken"}}})
        if url == wikimedia.SEARCH_URL:
            return FakeResponse(json_data=self.wikimedia_response)
        if url == nasa_images.SEARCH_URL:
            return FakeResponse(json_data=self.nasa_response)
        # download URL
        if url in self.rate_limit_downloads_for:
            times, retry_after = self.rate_limit_downloads_for[url]
            hits = self._rate_limit_hits.get(url, 0)
            if hits < times:
                self._rate_limit_hits[url] = hits + 1
                return FakeResponse(status_code=429, headers={"Retry-After": str(retry_after)})
        return FakeResponse(status_code=200, content=self.same_picture or _jpeg_for(url))

    def post(self, url, data=None, **kwargs):
        return FakeResponse(json_data={"login": {"result": "Success"}})


def test_download_family_accepts_relevant_licensed_images(conn, tmp_path):
    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"

    results = download_family(
        conn, session, "Example Family", image_dir=image_dir, sources=("wikimedia", "nasa"), sleep_fn=lambda s: None
    )

    assert results["wikimedia"]["downloaded"] == 1  # the logo.svg is filtered out before gating
    assert results["nasa"]["downloaded"] == 1

    images = conn.execute("SELECT * FROM images").fetchall()
    assert len(images) == 2
    for row in images:
        assert (tmp_path / "satellite_images" / "Example Family").exists()

    orphans = conn.execute(
        """SELECT images.* FROM images LEFT JOIN licenses ON licenses.image_id = images.id
           WHERE licenses.id IS NULL"""
    ).fetchall()
    assert orphans == []


def test_download_family_is_resume_safe(conn, tmp_path):
    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"

    download_family(
        conn, session, "Example Family", image_dir=image_dir, sources=("wikimedia", "nasa"), sleep_fn=lambda s: None
    )
    second = download_family(
        conn, session, "Example Family", image_dir=image_dir, sources=("wikimedia", "nasa"), sleep_fn=lambda s: None
    )

    assert second["wikimedia"]["skipped_already_fetched"] == 1
    assert second["nasa"]["skipped_already_fetched"] == 1
    assert second["wikimedia"]["downloaded"] == 0
    assert second["nasa"]["downloaded"] == 0

    total_images = conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"]
    assert total_images == 2  # no duplicates despite running twice


def test_a_new_image_never_overwrites_one_already_saved_under_the_same_name(conn, tmp_path):
    """Names come from position in the search results. When a site later
    returns a different item first, it used to be downloaded over the saved
    file (whose record and license then described the wrong picture), and
    the run stopped on a duplicate-path error."""
    import copy

    image_dir = tmp_path / "satellite_images"
    first = FakeSession(nasa_response=NASA_SEARCH_RESPONSE)
    download_family(conn, first, "Example Family", image_dir=image_dir, sources=("nasa",), sleep_fn=lambda s: None)
    (saved,) = conn.execute("SELECT path FROM images").fetchall()
    saved_path = Path(saved["path"])
    saved_path.write_bytes(TINY_JPEG + b"original")  # tell the saved file apart from a new download

    reordered = copy.deepcopy(NASA_SEARCH_RESPONSE)
    item = reordered["collection"]["items"][0]
    item["data"][0]["nasa_id"] = "PIA99999"
    item["links"][0]["href"] = "https://images-assets.nasa.gov/image/PIA99999/PIA99999~large.jpg"
    result = download_family(
        conn,
        FakeSession(nasa_response=reordered),
        "Example Family",
        image_dir=image_dir,
        sources=("nasa",),
        sleep_fn=lambda s: None,
    )

    assert result["nasa"]["downloaded"] == 1
    assert saved_path.read_bytes() == TINY_JPEG + b"original"  # untouched
    paths = sorted(Path(r["path"]).name for r in conn.execute("SELECT path FROM images"))
    assert paths == ["Example Family_nasa_01.jpg", "Example Family_nasa_01_2.jpg"]


def test_the_same_picture_from_two_sources_is_kept_once(conn, tmp_path):
    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    session.same_picture = TINY_JPEG
    results = download_family(
        conn,
        session,
        "Example Family",
        image_dir=tmp_path / "satellite_images",
        sources=("wikimedia", "nasa"),
        sleep_fn=lambda s: None,
    )
    assert results["wikimedia"]["downloaded"] == 1 and results["nasa"]["already_in_master_set"] == 1
    statuses = sorted(r["status"] for r in conn.execute("SELECT status FROM images"))
    assert statuses == ["active", "rejected_duplicate"]
    (entry,) = conn.execute("SELECT * FROM ledger.ledger_entries").fetchall()
    sources = {r["source"] for r in conn.execute("SELECT source FROM ledger.ledger_sources")}
    assert entry["state"] == "waiting" and sources == {"wikimedia", "nasa"}  # one image, both sources on record


def test_a_picture_rejected_before_is_set_aside_from_any_source(conn, tmp_path):
    from scraper.stages import review

    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE)
    session.same_picture = TINY_JPEG
    download_family(
        conn, session, "Example Family", image_dir=tmp_path / "imgs", sources=("wikimedia",), sleep_fn=lambda s: None
    )
    (first,) = conn.execute("SELECT id FROM images").fetchall()
    review.decide(conn, first["id"], "reject", tmp_path / "_quarantine", "several_satellites")

    nasa = FakeSession(nasa_response=NASA_SEARCH_RESPONSE)
    nasa.same_picture = TINY_JPEG  # the same picture, now from NASA
    result = download_family(
        conn, nasa, "Example Family", image_dir=tmp_path / "imgs", sources=("nasa",), sleep_fn=lambda s: None
    )

    assert result["nasa"]["rejected_before"] == 1 and result["nasa"]["downloaded"] == 0
    row = conn.execute("SELECT status, reject_reason FROM images ORDER BY id DESC").fetchone()
    assert (row["status"], row["reject_reason"]) == ("rejected_filter", "rejected_before")
    (entry,) = conn.execute("SELECT * FROM ledger.ledger_entries").fetchall()
    assert (entry["state"], entry["reason"]) == ("rejected", "several_satellites")  # the first reason stands


def test_a_wipe_doesnt_make_a_scrape_fetch_everything_again(conn, tmp_path):
    session = FakeSession(nasa_response=NASA_SEARCH_RESPONSE)
    download_family(
        conn, session, "Example Family", image_dir=tmp_path / "imgs", sources=("nasa",), sleep_fn=lambda s: None
    )
    for table in ("licenses", "sources", "images"):  # what a wipe clears; the ledger stays
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    again = download_family(
        conn, session, "Example Family", image_dir=tmp_path / "imgs", sources=("nasa",), sleep_fn=lambda s: None
    )
    assert again["nasa"]["skipped_already_fetched"] == 1 and again["nasa"]["downloaded"] == 0


def test_download_family_negative_caches_empty_searches(conn, tmp_path):
    session = FakeSession(wikimedia_response=WIKIMEDIA_EMPTY_RESPONSE, nasa_response=NASA_EMPTY_RESPONSE)
    image_dir = tmp_path / "satellite_images"

    download_family(conn, session, "Obscure Family", image_dir=image_dir, sources=("wikimedia", "nasa"))

    assert license_gate.has_negative_cache(conn, "Obscure Family", "wikimedia")
    assert license_gate.has_negative_cache(conn, "Obscure Family", "nasa")

    # Second run should skip searching entirely (negative cache hit) rather than
    # calling search again.
    session.get_calls.clear()
    results = download_family(conn, session, "Obscure Family", image_dir=image_dir, sources=("wikimedia", "nasa"))
    assert results["wikimedia"] == {"skipped": "negative_cache"}
    assert results["nasa"] == {"skipped": "negative_cache"}
    assert session.get_calls == []


def test_download_family_respects_retry_after_and_persists_the_cooldown(conn, tmp_path):
    """Phase 1 Task 4: a 429 with a Retry-After header records a DB-persisted
    cooldown and the retry still succeeds, via the injected sleep_fn (no real
    waiting in this test)."""
    download_url = "https://images-assets.nasa.gov/image/PIA00001/PIA00001~large.jpg"
    session = FakeSession(
        nasa_response=NASA_SEARCH_RESPONSE,
        rate_limit_downloads_for={download_url: (1, "42")},
    )
    image_dir = tmp_path / "satellite_images"
    sleeps = []

    results = download_family(
        conn, session, "Example Family", image_dir=image_dir, sources=("nasa",), sleep_fn=sleeps.append
    )

    assert results["nasa"]["downloaded"] == 1
    assert any(39 <= s <= 42 for s in sleeps)  # waited out the recorded cooldown

    row = conn.execute("SELECT * FROM rate_limits WHERE host = 'images-assets.nasa.gov'").fetchone()
    assert row is not None
    assert row["last_retry_after"] == 42


def test_download_paces_between_items_and_breaks_after_consecutive_failures(conn, tmp_path):
    """A host returning steady non-429 errors (403/500/connection failures)
    never triggers rate_limiter's cooldown -- this is the orchestration-level
    backoff that covers that case instead."""
    many_items_response = {
        "collection": {
            "items": [
                {
                    "data": [
                        {
                            "nasa_id": f"PIA0000{i}",
                            "title": "Example Family satellite",
                            "description": "Public domain NASA image",
                            "photographer": "NASA/JPL",
                        }
                    ],
                    "links": [
                        {
                            "href": f"https://images-assets.nasa.gov/image/PIA0000{i}/PIA0000{i}~large.jpg",
                            "rel": "preview",
                            "render": "image",
                        }
                    ],
                }
                for i in range(1, 5)
            ]
        }
    }

    class AlwaysFailingSession(FakeSession):
        def get(self, url, params=None, **kwargs):
            self.get_calls.append(url)
            if url == nasa_images.SEARCH_URL:
                return FakeResponse(json_data=many_items_response)
            return FakeResponse(status_code=500)  # never 429 -- no rate_limiter cooldown involved

    session = AlwaysFailingSession()
    image_dir = tmp_path / "satellite_images"
    sleeps = []

    results = download_family(
        conn, session, "Example Family", image_dir=image_dir, sources=("nasa",), sleep_fn=sleeps.append
    )

    assert results["nasa"]["download_failed"] == 4
    # 1st and 2nd failures each pace normally; the 3rd (hitting the limit)
    # triggers the long pause instead; the 4th paces normally again.
    from scraper.stages.download import CONSECUTIVE_FAILURE_PAUSE_SECONDS, DOWNLOAD_PACE_SECONDS

    assert sleeps.count(DOWNLOAD_PACE_SECONDS) == 3
    assert sleeps.count(CONSECUTIVE_FAILURE_PAUSE_SECONDS) == 1


def test_download_family_stops_between_sources_when_already_cancelled(conn, tmp_path):
    """Regression check: cancel used to only be checked between *families*
    in run_scrape_job, so once a family's download_family call started, it
    ran every source to completion (each with its own pacing/circuit-breaker
    sleeps) before cancel was checked again -- Stop could take many minutes.
    download_family must check cancel of its own, between each source."""
    import threading

    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"
    cancel = threading.Event()
    cancel.set()

    results = download_family(
        conn,
        session,
        "Example Family",
        image_dir=image_dir,
        sources=("wikimedia", "nasa"),
        sleep_fn=lambda s: None,
        cancel=cancel,
    )

    assert results == {}  # cancelled before the first source even started
    assert session.get_calls == []


def test_download_family_stops_mid_source_loop_once_cancelled(conn, tmp_path):
    import threading

    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"
    cancel = threading.Event()

    def cancel_after_first_source(seconds):
        cancel.set()

    results = download_family(
        conn,
        session,
        "Example Family",
        image_dir=image_dir,
        sources=("wikimedia", "nasa"),
        sleep_fn=cancel_after_first_source,
        cancel=cancel,
    )

    assert "wikimedia" in results
    assert "nasa" not in results  # cancelled before the second source started


def test_process_results_stops_mid_item_loop_once_cancelled(conn, tmp_path):
    """Same granularity fix, one level down -- a family with many items on
    one source shouldn't have to finish all of them either."""
    import threading

    download_url = "https://images-assets.nasa.gov/image/PIA00001/PIA00001~large.jpg"
    many_items_response = {
        "collection": {
            "items": [
                {
                    "data": [
                        {
                            "nasa_id": f"PIA0000{i}",
                            "title": "Example Family satellite",
                            "description": "x",
                            "photographer": "NASA",
                        }
                    ],
                    "links": [
                        {
                            "href": f"https://images-assets.nasa.gov/image/PIA0000{i}/PIA0000{i}~large.jpg",
                            "rel": "preview",
                            "render": "image",
                        }
                    ],
                }
                for i in range(1, 5)
            ]
        }
    }
    session = FakeSession(nasa_response=many_items_response)
    image_dir = tmp_path / "satellite_images"
    cancel = threading.Event()
    downloads_seen = []

    real_get = session.get

    def tracking_get(url, params=None, **kwargs):
        if url != download_url and "~large.jpg" in url:
            downloads_seen.append(url)
            if len(downloads_seen) >= 2:
                cancel.set()
        return real_get(url, params=params, **kwargs)

    session.get = tracking_get

    results = download_family(
        conn,
        session,
        "Example Family",
        image_dir=image_dir,
        sources=("nasa",),
        sleep_fn=lambda s: None,
        cancel=cancel,
    )

    # Stopped partway through 4 available items instead of processing all of them.
    assert results["nasa"]["downloaded"] < 4


def test_download_family_reports_which_source_it_is_currently_on(conn, tmp_path):
    """The "currently on <family>: <source> (i/n)" indicator -- a family-level
    counter alone looked identical whether a source was slow or truly stuck."""
    session = FakeSession(wikimedia_response=WIKIMEDIA_SEARCH_RESPONSE, nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"
    calls = []

    download_family(
        conn,
        session,
        "Example Family",
        image_dir=image_dir,
        sources=("wikimedia", "nasa"),
        sleep_fn=lambda s: None,
        on_source_start=lambda source, index, total: calls.append((source, index, total)),
    )

    assert calls == [("wikimedia", 1, 2), ("nasa", 2, 2)]


def test_download_family_reports_item_progress_within_the_current_source(conn, tmp_path):
    """One level finer than on_source_start -- a source with many relevant
    items looked identical whether it was slow-but-working or truly stuck,
    since on_source_start only fires once per source, not once per item."""
    session = FakeSession(nasa_response=NASA_SEARCH_RESPONSE)
    image_dir = tmp_path / "satellite_images"
    calls = []

    download_family(
        conn,
        session,
        "Example Family",
        image_dir=image_dir,
        sources=("nasa",),
        sleep_fn=lambda s: None,
        on_item_progress=lambda source, item_index, item_total: calls.append((source, item_index, item_total)),
    )

    # NASA_SEARCH_RESPONSE has exactly one relevant item.
    assert calls == [("nasa", 1, 1)]


def test_a_download_that_isnt_an_image_is_set_aside(conn, tmp_path, monkeypatch):
    from scraper.stages import download as download_stage

    monkeypatch.chdir(tmp_path)
    record = type("R", (), {"external_id": "x1", "download_url": "https://archive.example/item.pdf", "family": "SAT"})()

    def fake_download(rec, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 not a picture")
        return True

    monkeypatch.setattr(download_stage.license_gate, "is_already_fetched", lambda *a: False)
    monkeypatch.setattr(download_stage.license_gate, "evaluate", lambda rec: type("G", (), {"accepted": True})())

    def fake_persist(conn, rec, result, path):
        cur = conn.execute(
            "INSERT INTO images (family, path, status, created_at) VALUES ('SAT', ?, 'active', 'now')", (path,)
        )
        return cur.lastrowid

    monkeypatch.setattr(download_stage.license_gate, "persist", fake_persist)
    counts = download_stage._process_results(
        conn,
        "SAT",
        "archive",
        [{"id": 1}],
        extract=lambda raw, now: record,
        download=fake_download,
        image_dir=tmp_path / "satellite_images",
        sleep_fn=lambda s: None,
    )
    assert counts["not_an_image"] == 1 and counts["downloaded"] == 0
    row = conn.execute("SELECT status, reject_reason, path FROM images").fetchone()
    assert (row["status"], row["reject_reason"]) == ("rejected_filter", "not_an_image")
    assert row["path"].startswith("_quarantine") and Path(row["path"]).is_file()
