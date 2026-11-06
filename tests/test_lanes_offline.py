"""Offline tests for scrape lanes: each site in its own worker, side by side,
with a failing site only stopping its own lane. download_family and the
Wikimedia login are faked; no network, no real waiting."""

import threading

import pytest

from scraper import jobs
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import lanes, overview, scrape_runs


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """No real two-minute pause after an error, and no real login."""
    init = lanes.LaneRun.__init__

    def wrapped(self, *args, **kwargs):
        kwargs["wait"] = lambda seconds: None
        init(self, *args, **kwargs)

    monkeypatch.setattr(lanes.LaneRun, "__init__", wrapped)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)


def test_sources_that_share_a_rate_limit_share_a_lane():
    assert lanes.group_sources(("wikimedia", "nasa", "wikidata", "dpla", "wikipedia_multilang")) == {
        "wikimedia": ("wikimedia", "wikidata", "wikipedia_multilang"),
        "nasa": ("nasa",),
        "dpla": ("dpla",),
    }


def test_lanes_run_at_the_same_time_each_with_its_own_connection(conn, tmp_path, monkeypatch):
    nasa_started = threading.Event()
    seen = {}

    def fake(conn_, session, family, **kwargs):
        (source,) = kwargs["sources"]
        seen.setdefault(source, set()).add((id(conn_), threading.current_thread().name))
        if source == "nasa":
            nasa_started.set()
        else:
            # One site at a time would never get here: NASA only starts after Wikimedia is done.
            assert nasa_started.wait(5), "lanes didn't run side by side"
        return {source: {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake)
    report = jobs.run_scrape_job(conn, ["A", "B"], ("wikimedia", "nasa"), None, tmp_path)

    assert report["completed"] == 2 and not report["interrupted"]
    (wikimedia_handle,), (nasa_handle,) = seen["wikimedia"], seen["nasa"]
    assert wikimedia_handle != nasa_handle and id(conn) not in (wikimedia_handle[0], nasa_handle[0])


def test_a_failing_site_only_stops_its_own_lane(conn, tmp_path, monkeypatch):
    families = [f"F{i}" for i in range(8)]
    done = []

    def fake(conn_, session, family, **kwargs):
        (source,) = kwargs["sources"]
        if source == "nasa":
            raise TimeoutError("read timed out")
        done.append(family)
        return {source: {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake)
    logs = []
    report = jobs.run_scrape_job(conn, families, ("wikimedia", "nasa"), None, tmp_path, log=logs.append)

    assert done == families  # Wikimedia carried on through every family
    states = {lane["name"]: lane["state"] for lane in report["lanes"]}
    assert states == {"wikimedia": "done", "nasa": "gave_up"}
    (run,) = scrape_runs.recent(conn)
    assert run["status"] == "finished" and "nasa gave up" in run["error"]
    assert run["summary"]["nasa"]["errors"] == lanes.GIVE_UP_AFTER
    assert any("[nasa] gave up" in line for line in logs)
    assert any(
        a["text"].startswith("The last scrape finished, but nasa gave up")
        for a in overview.alerts(
            conn, {"waiting_for_filter": 0, "waiting_for_review": 0}, [], overview._runs(conn), None
        )
    )


def test_an_error_is_retried_then_skipped(conn, tmp_path, monkeypatch):
    attempts = []

    def fake(conn_, session, family, **kwargs):
        attempts.append(family)
        if family == "flaky" and attempts.count("flaky") == 1:
            raise ConnectionError("reset")
        if family == "broken":
            raise ConnectionError("always down")
        return {"nasa": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake)
    report = jobs.run_scrape_job(conn, ["flaky", "broken", "fine"], ("nasa",), None, tmp_path)

    assert attempts == ["flaky", "flaky", "broken", "broken", "fine"]
    assert report["completed"] == 3
    (run,) = scrape_runs.recent(conn)
    assert run["status"] == "finished" and "nasa skipped 1 family(ies)" in run["error"]


def test_stop_ends_every_lane_after_its_current_family(conn, tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def fake(conn_, session, family, **kwargs):
        (source,) = kwargs["sources"]
        calls.append((source, family))
        if family == "B":
            cancel.set()
        return {source: {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake)
    logs = []
    report = jobs.run_scrape_job(
        conn, ["A", "B", "C", "D"], ("wikimedia", "nasa"), None, tmp_path, log=logs.append, cancel=cancel
    )

    assert report["interrupted"]
    # The other lane may already be one family further along; nobody starts D.
    assert all(family != "D" for _, family in calls)
    assert scrape_runs.recent(conn)[0]["status"] == "stopped"
    assert any("Cancelled after" in line for line in logs)


def test_time_per_source_is_recorded(conn, tmp_path, monkeypatch):
    def fake(conn_, session, family, **kwargs):
        for source in kwargs["sources"]:
            kwargs["on_source_start"](source, 1, 1)
            kwargs["on_source_done"](source, 0)
        return {}

    monkeypatch.setattr(jobs, "download_family", fake)
    jobs.run_scrape_job(conn, ["A"], ("wikimedia", "wikidata", "dpla"), None, tmp_path)
    summary = scrape_runs.recent(conn)[0]["summary"]
    assert summary["wikidata"]["lane"] == "wikimedia" and summary["dpla"]["lane"] == "dpla"
    assert all(isinstance(summary[s]["seconds"], float) for s in ("wikimedia", "wikidata", "dpla"))


def test_lane_updates_are_reported(conn, tmp_path, monkeypatch):
    def fake(conn_, session, family, **kwargs):
        (source,) = kwargs["sources"]
        kwargs["on_source_start"](source, 1, 1)
        kwargs["on_item_progress"](source, 1, 4)
        return {source: {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake)
    snapshots, statuses = [], []
    jobs.run_scrape_job(
        conn,
        ["A"],
        ("nasa", "dpla"),
        None,
        tmp_path,
        on_lanes=snapshots.append,
        on_status=lambda *a, **k: statuses.append(a),
    )
    assert statuses == []  # several lanes: the lanes table replaces "currently on"
    assert any(lane["item"] == "1 / 4" for snap in snapshots for lane in snap)
    assert {lane["name"]: lane["state"] for lane in snapshots[-1]} == {"nasa": "done", "dpla": "done"}
