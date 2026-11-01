"""Offline tests for Phase 1 Task 3: license gate. No network calls."""

from datetime import datetime, timezone

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.license_gate import count_rejections_by_reason, evaluate, persist
from scraper.sources.base import UNVERIFIED_LICENSE
from scraper.sources.gunters_space import extract_record as gunters_extract_record
from scraper.sources.nasa_images import extract_record as nasa_extract_record
from scraper.sources.wikimedia import extract_record as wikimedia_extract_record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


# ---- Wikimedia fixtures ----

FREE_LICENSE_PAGE = {
    "title": "File:Example Satellite.jpg",
    "imageinfo": [
        {
            "url": "https://upload.wikimedia.org/wikipedia/commons/example.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/example.jpg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example_Satellite.jpg",
            "mime": "image/jpeg",
            "extmetadata": {
                "ImageDescription": {"value": "A <b>satellite</b> in orbit"},
                "Artist": {"value": "Jane Photographer"},
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                "Credit": {"value": "Own work"},
                "AttributionRequired": {"value": "true"},
            },
        }
    ],
}

NO_LICENSE_PAGE = {
    "title": "File:Unclear Rights.jpg",
    "imageinfo": [
        {
            "url": "https://upload.wikimedia.org/wikipedia/commons/unclear.jpg",
            "mime": "image/jpeg",
            "extmetadata": {
                "ImageDescription": {"value": "Unclear rights"},
                "Artist": {"value": ""},
                "LicenseShortName": {"value": ""},
            },
        }
    ],
}


def test_wikimedia_extract_record_captures_all_license_fields():
    record = wikimedia_extract_record(FREE_LICENSE_PAGE, "Example Family", _now())
    assert record.license_short_name == "CC BY-SA 4.0"
    assert record.license_url == "https://creativecommons.org/licenses/by-sa/4.0"
    assert record.credit == "Own work"
    assert record.attribution_required is True
    assert record.author == "Jane Photographer"
    assert record.description == "A satellite in orbit"
    assert record.source_url == "https://commons.wikimedia.org/wiki/File:Example_Satellite.jpg"


def test_wikimedia_free_license_is_accepted():
    record = wikimedia_extract_record(FREE_LICENSE_PAGE, "Example Family", _now())
    result = evaluate(record)
    assert result.accepted is True
    assert result.reason is None


def test_wikimedia_empty_license_is_accepted_and_recorded_as_empty(conn):
    """Licensing is a human call made during Discovery review, not a runtime
    gate -- evaluate() always accepts, but the empty value is still recorded
    transparently (not faked) for provenance."""
    record = wikimedia_extract_record(NO_LICENSE_PAGE, "Example Family", _now())
    result = evaluate(record)
    assert result.accepted is True

    image_id = persist(conn, record, result, path="satellite_images/Example Family/1.jpg")
    row = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (image_id,)).fetchone()
    assert row["license_short_name"] == ""


# ---- NASA fixtures ----

PUBLIC_DOMAIN_ITEM = {
    "data": [
        {
            "nasa_id": "PIA00001",
            "title": "A Satellite",
            "description": "Public domain NASA image",
            "photographer": "NASA/JPL",
            "date_created": "2020-01-01T00:00:00Z",
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

RESTRICTED_ITEM = {
    "data": [
        {
            "nasa_id": "PIA00002",
            "title": "A Contractor Satellite Photo",
            "description": "Restricted rights image",
            "photographer": "Lockheed Martin",
            "copyright": "Lockheed Martin Corporation",
            "date_created": "2021-01-01T00:00:00Z",
        }
    ],
    "links": [
        {
            "href": "https://images-assets.nasa.gov/image/PIA00002/PIA00002~large.jpg",
            "rel": "preview",
            "render": "image",
        }
    ],
}


def test_nasa_public_domain_is_accepted():
    record = nasa_extract_record(PUBLIC_DOMAIN_ITEM, "Example Family", _now())
    assert record is not None
    result = evaluate(record)
    assert result.accepted is True
    assert record.license_short_name == "NASA (public domain)"


def test_nasa_copyright_field_is_accepted_and_recorded_as_empty(conn):
    record = nasa_extract_record(RESTRICTED_ITEM, "Example Family", _now())
    assert record is not None
    result = evaluate(record)
    assert result.accepted is True

    image_id = persist(conn, record, result, path="satellite_images/Example Family/1.jpg")
    row = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (image_id,)).fetchone()
    assert row["license_short_name"] == ""


def test_unverified_license_marker_is_accepted_like_any_other_value(conn):
    """UNVERIFIED_LICENSE used to be a source-specific exception in the gate
    itself; now that the gate always accepts, it's just a regular string
    value any source can write and have recorded honestly."""
    record = nasa_extract_record(PUBLIC_DOMAIN_ITEM, "Example Family", _now())
    assert record is not None
    record.license_short_name = UNVERIFIED_LICENSE

    result = evaluate(record)
    assert result.accepted is True

    image_id = persist(conn, record, result, path="satellite_images/Example Family/1.jpg")
    row = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (image_id,)).fetchone()
    assert row["license_short_name"] == UNVERIFIED_LICENSE


def test_gunters_records_are_persisted_like_any_other_source(conn, monkeypatch):
    from scraper.sources import gunters_space

    monkeypatch.setattr(gunters_space, "ENABLED", True)
    record = gunters_extract_record(
        {
            "url": "https://space.skyrocket.de/img_sat/starlink-1007.jpg",
            "title": "starlink-1007.jpg",
        },
        "STARLINK",
        _now(),
    )
    assert record is not None
    assert record.license_short_name == UNVERIFIED_LICENSE

    result = evaluate(record)
    assert result.accepted is True
    image_id = persist(conn, record, result, path="satellite_images/STARLINK/1.jpg")

    assert image_id is not None
    row = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (image_id,)).fetchone()
    assert row["license_short_name"] == UNVERIFIED_LICENSE


# ---- Persistence across a mixed batch ----


def test_mixed_batch_leaves_no_orphaned_images(conn):
    """Every record is accepted now, licensed or not -- there's nothing left
    for evaluate() to reject, so a batch mixing clearly-licensed and
    no-license-data records all land in images/licenses, none in rejections."""
    fixtures = [
        (wikimedia_extract_record(FREE_LICENSE_PAGE, "Family A", _now()), "satellite_images/Family A/1.jpg"),
        (wikimedia_extract_record(NO_LICENSE_PAGE, "Family A", _now()), "satellite_images/Family A/2.jpg"),
        (nasa_extract_record(PUBLIC_DOMAIN_ITEM, "Family B", _now()), "satellite_images/Family B/1.jpg"),
        (nasa_extract_record(RESTRICTED_ITEM, "Family B", _now()), "satellite_images/Family B/2.jpg"),
    ]

    accepted_ids = []
    for record, path in fixtures:
        result = evaluate(record)
        image_id = persist(conn, record, result, path)
        if result.accepted:
            accepted_ids.append(image_id)

    assert len(accepted_ids) == 4

    orphans = conn.execute(
        """SELECT images.* FROM images
           LEFT JOIN licenses ON licenses.image_id = images.id
           WHERE licenses.id IS NULL"""
    ).fetchall()
    assert orphans == []

    total_images = conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"]
    assert total_images == 4

    counts = count_rejections_by_reason(conn)
    assert counts == {}
