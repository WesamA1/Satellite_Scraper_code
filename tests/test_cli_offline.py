"""Offline tests for Phase 1 Task 5: the CLI. All network-free -- the pieces
that would otherwise make real calls (download_family, doctor's reachability
check, filter's real CLIP pipeline) are monkeypatched at their CLI-visible
import points, same dependency-injection pattern as the rest of the suite.
"""

import csv
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from scraper import cli, jobs
from scraper.stages import filter as filter_module

runner = CliRunner()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def catalog_path(tmp_path):
    path = tmp_path / "satellites.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["NORAD_CAT_ID", "OBJECT_NAME", "FAMILY_NAME", "VARIANT_NAME"])
        for i in range(8):
            writer.writerow([i, f"SAT {i}", f"Family {i}", f"Family {i}"])
    return path


def test_scrape_dry_run_with_explicit_families_touches_nothing(db_path, monkeypatch):
    called = []
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: called.append((a, k)))

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "scrape", "--dry-run", "--family", "Family A,Family B"],
    )

    assert result.exit_code == 0
    assert "Family A" in result.stdout
    assert "Family B" in result.stdout
    assert called == []  # dry-run never calls download_family


def test_scrape_dry_run_applies_smoke_profile_family_limit(db_path, catalog_path):
    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "scrape", "--dry-run", "--profile", "smoke", "--catalog", str(catalog_path)],
    )

    assert result.exit_code == 0
    # smoke profile caps at 5 families out of the 8 in the fixture catalog
    assert result.stdout.count("Family ") >= 5
    assert "Family 7" not in result.stdout  # beyond the 5-family cap


def test_scrape_real_run_calls_download_family_per_family(db_path, monkeypatch):
    calls = []

    def fake_download_family(conn, session, family_name, **kwargs):
        calls.append(family_name)
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "scrape", "--family", "Family A,Family B"],
    )

    assert result.exit_code == 0
    assert calls == ["Family A", "Family B"]
    assert "Run report" in result.stdout


def test_scrape_aborts_if_wikimedia_login_fails(db_path, monkeypatch):
    called = []
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: called.append(1))
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: False)

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "scrape", "--family", "Family A"])

    assert result.exit_code == 1
    assert called == []


def test_filter_dry_run_reports_without_touching_disk(db_path, tmp_path, monkeypatch):
    from PIL import Image

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(db_path)
    apply_migrations(conn)
    image_path = tmp_path / "img.jpg"
    Image.new("RGB", (4, 4)).save(image_path)
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, 'active', ?)",
        (str(image_path), _now()),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: lambda paths: [0.1 for _ in paths])

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "filter", "--dry-run"])

    assert result.exit_code == 0
    assert "'rejected': 1" in result.stdout
    assert image_path.exists()  # dry-run never moves the file


def test_status_lists_zero_image_families(db_path, catalog_path):
    result = runner.invoke(cli.app, ["--db-path", str(db_path), "status", "--catalog", str(catalog_path)])

    assert result.exit_code == 0
    assert "8 family(ies) with zero images" in result.stdout


def test_doctor_runs_offline_and_reports_a_table(db_path, monkeypatch):
    monkeypatch.setattr("scraper.doctor._default_fetch", lambda url: True)

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "doctor"])

    assert "gpu" in result.stdout
    assert "disk_space" in result.stdout
    assert "reachable:commons.wikimedia.org" in result.stdout


def test_retry_failed_clears_cache_and_rescrapes_given_family(db_path, monkeypatch):
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(db_path)
    apply_migrations(conn)
    conn.execute(
        """INSERT INTO rejections (family, source_module, reason, rejected_at)
           VALUES ('Family A', 'nasa', 'no_images_found', ?)""",
        (_now(),),
    )
    conn.commit()
    conn.close()

    calls = []

    def fake_download_family(conn, session, family_name, **k):
        calls.append(family_name)
        return {"nasa": {"downloaded": 0}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "retry-failed", "--family", "Family A", "--source", "nasa"],
    )

    assert result.exit_code == 0
    assert calls == ["Family A"]
    assert "Cleared 1 negative-cache" in result.stdout


def test_retry_failed_without_family_only_clears_and_does_not_rescrape(db_path, monkeypatch):
    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    conn = get_connection(db_path)
    apply_migrations(conn)
    conn.execute(
        """INSERT INTO rejections (family, source_module, reason, rejected_at)
           VALUES ('Family A', 'nasa', 'no_images_found', ?)""",
        (_now(),),
    )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr(jobs, "download_family", lambda *a, **k: calls.append(1))

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "retry-failed"])

    assert result.exit_code == 0
    assert calls == []  # no family scope given -> informational only, no auto re-scrape
    assert "Cleared 1" in result.stdout


def test_browser_target_url_uses_localhost_for_bind_all_host():
    assert cli._browser_target_url("0.0.0.0", 8000) == "http://127.0.0.1:8000/"


def test_browser_target_url_passes_through_other_hosts():
    assert cli._browser_target_url("127.0.0.1", 9000) == "http://127.0.0.1:9000/"
    assert cli._browser_target_url("192.168.1.5", 8000) == "http://192.168.1.5:8000/"


class _ImmediateTimer:
    """Stands in for threading.Timer -- runs the callback synchronously
    instead of actually waiting, so the test doesn't sleep."""

    def __init__(self, interval, function, args=None, kwargs=None):
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}

    def start(self):
        self.function(*self.args, **self.kwargs)


def test_serve_opens_the_browser_by_default(db_path, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    monkeypatch.setattr("threading.Timer", _ImmediateTimer)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "serve", "--port", "8123"])

    assert result.exit_code == 0
    assert opened == ["http://127.0.0.1:8123/"]


def test_serve_no_browser_flag_skips_opening_it(db_path, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    monkeypatch.setattr("threading.Timer", _ImmediateTimer)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "serve", "--no-browser"])

    assert result.exit_code == 0
    assert opened == []
