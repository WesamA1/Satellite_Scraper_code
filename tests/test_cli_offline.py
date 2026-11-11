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
        calls.append((family_name, kwargs["sources"]))
        return {"wikimedia": {"downloaded": 1}}

    monkeypatch.setattr(jobs, "download_family", fake_download_family)
    monkeypatch.setattr(jobs.wikimedia, "login", lambda session, conn: True)

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "scrape", "--family", "Family A,Family B"],
    )

    assert result.exit_code == 0
    # The default sources (wikimedia, nasa) run as two lanes, each through every family in order.
    assert sorted(calls) == [
        ("Family A", ("nasa",)),
        ("Family A", ("wikimedia",)),
        ("Family B", ("nasa",)),
        ("Family B", ("wikimedia",)),
    ]
    assert [f for f, src in calls if src == ("nasa",)] == ["Family A", "Family B"]
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


def test_refresh_catalog_cmd_prints_summary_on_success(db_path, monkeypatch, tmp_path):
    from scraper import catalog as catalog_module

    monkeypatch.setattr(
        catalog_module,
        "refresh_catalog",
        lambda session, csv_path, json_path, report_only=False: {
            "row_count": 3,
            "family_count": 2,
            "variant_count": 2,
            "report_text": "Families split into variants:\n\n0 families split; 2 families -> 2 variant labels total",
            "backup_path": None,
        },
    )

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "refresh-catalog", "--catalog", str(tmp_path / "satellites.csv")],
    )

    assert result.exit_code == 0
    assert "Wrote 3 rows" in result.stdout


def test_refresh_catalog_cmd_exits_non_zero_on_login_failure(db_path, monkeypatch, tmp_path):
    from scraper import catalog as catalog_module

    def _raise(session, csv_path, json_path, report_only=False):
        raise RuntimeError("Space-Track login failed -- check SPACETRACK_USERNAME/PASSWORD in .env.")

    monkeypatch.setattr(catalog_module, "refresh_catalog", _raise)

    result = runner.invoke(
        cli.app,
        ["--db-path", str(db_path), "refresh-catalog", "--catalog", str(tmp_path / "satellites.csv")],
    )

    assert result.exit_code == 1
    assert "login failed" in result.output


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
    monkeypatch.setattr("uvicorn.Server.run", lambda *a, **k: None)
    monkeypatch.setattr(cli.desktop, "is_running", lambda url: False)
    monkeypatch.setattr("threading.Timer", _ImmediateTimer)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "serve", "--port", "8123"])

    assert result.exit_code == 0
    assert opened == ["http://127.0.0.1:8123/"]


def test_serve_no_browser_flag_skips_opening_it(db_path, monkeypatch):
    monkeypatch.setattr("uvicorn.Server.run", lambda *a, **k: None)
    monkeypatch.setattr(cli.desktop, "is_running", lambda url: False)
    monkeypatch.setattr("threading.Timer", _ImmediateTimer)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(cli.app, ["--db-path", str(db_path), "serve", "--no-browser"])

    assert result.exit_code == 0
    assert opened == []


def test_fix_csv_writes_a_fixed_copy_and_leaves_the_original(db_path, tmp_path):
    """`fix-csv`: a row written in the known wrong order comes out under the
    right headers in <name>_fixed.csv; --dry-run writes nothing."""
    from pathlib import Path

    from scraper.stages import annotate, fix_csv

    attributes = Path(__file__).resolve().parent.parent / "attributes.json"
    order = fix_csv.KNOWN_ROW_ORDERS["windows-and-manufacturer-early"]["order"]
    answers = dict.fromkeys(order, "Unknown") | {"Shape": "Rectangular", "Solar Panel Configuration": "Double Wing"}
    old_csv = tmp_path / "old.csv"
    with open(old_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", *annotate.flat_attributes(annotate.load_schema(attributes))])
        writer.writerow(["b.jpg", *(answers[attribute] for attribute in order)])
    before = old_csv.read_bytes()
    base = ["--db-path", str(db_path), "fix-csv", str(old_csv), "--attributes", str(attributes)]
    report = tmp_path / "report.csv"

    dry = runner.invoke(cli.app, [*base, "--report", str(report), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would fix 1 of 1 rows" in dry.output
    assert not (tmp_path / "old_fixed.csv").exists() and not report.exists()

    real = runner.invoke(cli.app, [*base, "--report", str(report)])
    assert real.exit_code == 0, real.output
    assert "Fixed 1 of 1 rows" in real.output
    assert old_csv.read_bytes() == before
    with open(tmp_path / "old_fixed.csv", newline="", encoding="utf-8") as f:
        assert next(csv.DictReader(f))["Solar Panel Configuration"] == "Double Wing"
    assert report.exists()

    missing = runner.invoke(
        cli.app, [*base[:2], "fix-csv", str(tmp_path / "nope.csv"), "--attributes", str(attributes)]
    )
    assert missing.exit_code != 0


def test_merge_annotations_brings_in_another_machines_labels(db_path, tmp_path):
    """`merge-annotations`: --dry-run only counts; without it the other
    database's labels are added here."""
    import sqlite3

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    def database(path, annotate_as=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = get_connection(path)
        apply_migrations(conn)
        image_id = conn.execute(
            """INSERT INTO images (family, path, status, content_sha256, created_at)
               VALUES ('ESSA', 'data/images/ESSA/a.jpg', 'active', 'aaa', '2026-11-02')"""
        ).lastrowid
        if annotate_as:
            conn.execute(
                """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
                   VALUES (?, 'Shape', '"Cylindrical"', ?, 'v1', '2026-11-09T10:00:00+00:00')""",
                (image_id, annotate_as),
            )
        conn.commit()
        conn.close()

    database(db_path)
    other = tmp_path / "usb" / "scraper.db"
    database(other, annotate_as="alice")
    attributes = tmp_path / "attributes.json"
    attributes.write_text('{"Things": {"Shape": ["Dropdown", ["Rectangular", "Cylindrical"]]}}', encoding="utf-8")
    base = ["--db-path", str(db_path), "merge-annotations", str(other), "--attributes", str(attributes)]
    review = ["--review-out", str(tmp_path / "review.csv")]

    def saved():
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT annotator, value FROM annotations").fetchall()
        finally:
            conn.close()

    dry = runner.invoke(cli.app, [*base, *review, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "1: [dry run] would be added" in dry.output
    assert saved() == []

    real = runner.invoke(cli.app, [*base, *review])
    assert real.exit_code == 0, real.output
    assert "alice: 1 added, 0 updated" in real.output
    assert saved() == [("alice", '"Cylindrical"')]

    missing = runner.invoke(cli.app, [*base[:3], str(tmp_path / "nope.db")])
    assert missing.exit_code != 0
