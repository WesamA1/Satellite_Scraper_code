"""Offline test for Phase 1 Task 1's export stage."""

import csv
from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.license_gate import GateResult, persist
from scraper.sources.base import ImageRecord
from scraper.stages.export import FIELDNAMES, run_export


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _accepted_record(external_id="File:example.jpg"):
    return ImageRecord(
        family="Example Family",
        source_module="wikimedia",
        external_id=external_id,
        source_url="https://commons.wikimedia.org/wiki/File:Example.jpg",
        download_url="https://upload.wikimedia.org/example.jpg",
        title="Example",
        description="desc",
        author="Jane",
        license_short_name="CC BY-SA 4.0",
        license_url="https://creativecommons.org/licenses/by-sa/4.0",
        attribution_required=True,
        credit="Own work",
        retrieved_at=_now(),
        mime="image/jpeg",
    )


def test_export_writes_only_active_images(conn, tmp_path):
    accepted = _accepted_record()
    persist(conn, accepted, GateResult(accepted=True, reason=None), path="satellite_images/Example Family/1.jpg")

    rejected = _accepted_record(external_id="File:rejected.jpg")
    rejected.license_short_name = ""
    persist(conn, rejected, GateResult(accepted=False, reason="no_license"), path="")

    out_path = tmp_path / "export" / "manifest.csv"
    row_count = run_export(conn, out_path=out_path)

    assert row_count == 1
    with open(out_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == FIELDNAMES
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["family"] == "Example Family"
    assert rows[0]["license_short_name"] == "CC BY-SA 4.0"
    assert rows[0]["attribution_required"] == "1"
