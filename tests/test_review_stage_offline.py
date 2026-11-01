"""Offline tests for the review stage: keep/reject/duplicate decisions and
variant labeling. No network, no real satellite imagery."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import review


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _make_image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)


def _insert_active_image(conn, path: Path, family="STARLINK", duplicate_group_id=None):
    cur = conn.execute(
        "INSERT INTO images (family, path, status, duplicate_group_id, created_at) VALUES (?, ?, 'active', ?, ?)",
        (family, str(path), duplicate_group_id, _now()),
    )
    conn.commit()
    return cur.lastrowid


def test_review_queue_excludes_already_reviewed_images(conn, tmp_path):
    path = tmp_path / "STARLINK" / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)

    assert len(review.review_queue(conn)) == 1

    review.decide(conn, image_id, "keep")
    assert review.review_queue(conn) == []


def test_review_queue_filters_by_family(conn, tmp_path):
    starlink_path = tmp_path / "STARLINK" / "a.jpg"
    oneweb_path = tmp_path / "ONEWEB" / "a.jpg"
    _make_image(starlink_path)
    _make_image(oneweb_path)
    _insert_active_image(conn, starlink_path, family="STARLINK")
    _insert_active_image(conn, oneweb_path, family="ONEWEB")

    queue = review.review_queue(conn, family="ONEWEB")
    assert len(queue) == 1
    assert queue[0]["family"] == "ONEWEB"


def test_review_queue_reports_duplicate_siblings(conn, tmp_path):
    path_a = tmp_path / "STARLINK" / "a.jpg"
    path_b = tmp_path / "STARLINK" / "b.jpg"
    _make_image(path_a)
    _make_image(path_b)
    id_a = _insert_active_image(conn, path_a, duplicate_group_id=5)
    id_b = _insert_active_image(conn, path_b, duplicate_group_id=5)

    queue = {row["id"]: row for row in review.review_queue(conn)}
    assert queue[id_a]["duplicate_siblings"] == [id_b]
    assert queue[id_b]["duplicate_siblings"] == [id_a]


def test_decide_keep_leaves_file_in_place_and_marks_reviewed(conn, tmp_path):
    path = tmp_path / "STARLINK" / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)

    review.decide(conn, image_id, "keep")

    row = conn.execute("SELECT status, path, reviewed_at FROM images WHERE id = ?", (image_id,)).fetchone()
    assert row["status"] == "active"
    assert Path(row["path"]) == path
    assert path.exists()
    assert row["reviewed_at"] is not None


def test_decide_reject_moves_file_to_quarantine(conn, tmp_path):
    path = tmp_path / "images" / "STARLINK" / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)
    quarantine_dir = tmp_path / "_quarantine"

    review.decide(conn, image_id, "reject", quarantine_dir=quarantine_dir)

    row = conn.execute("SELECT status, path FROM images WHERE id = ?", (image_id,)).fetchone()
    assert row["status"] == "rejected_filter"
    assert not path.exists()
    assert Path(row["path"]) == quarantine_dir / "STARLINK" / "a.jpg"
    assert Path(row["path"]).exists()


def test_decide_duplicate_moves_file_and_sets_rejected_duplicate(conn, tmp_path):
    path = tmp_path / "images" / "STARLINK" / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)
    quarantine_dir = tmp_path / "_quarantine"

    review.decide(conn, image_id, "duplicate", quarantine_dir=quarantine_dir)

    row = conn.execute("SELECT status FROM images WHERE id = ?", (image_id,)).fetchone()
    assert row["status"] == "rejected_duplicate"


def test_decide_rejects_an_unknown_decision(conn, tmp_path):
    path = tmp_path / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)

    with pytest.raises(ValueError, match="Unknown decision"):
        review.decide(conn, image_id, "maybe")


def test_available_variants_filters_to_the_requested_family(tmp_path):
    catalog = tmp_path / "satellites.csv"
    catalog.write_text(
        "NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n"
        "1,STARLINK-1,STARLINK,STARLINK V1.0\n"
        "2,STARLINK-2,STARLINK,STARLINK V1.5\n"
        "3,ONEWEB-1,ONEWEB,ONEWEB\n"
    )

    variants = review.available_variants("STARLINK", catalog=catalog)
    assert variants == ["STARLINK V1.0", "STARLINK V1.5"]


def test_available_variants_empty_when_variant_equals_family(tmp_path):
    catalog = tmp_path / "satellites.csv"
    catalog.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n3,ONEWEB-1,ONEWEB,ONEWEB\n")

    assert review.available_variants("ONEWEB", catalog=catalog) == []


def test_record_and_get_variant_round_trip(conn, tmp_path):
    path = tmp_path / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)

    assert review.get_variant(conn, image_id) is None

    review.record_variant(conn, image_id, "STARLINK V1.5")
    assert review.get_variant(conn, image_id) == "STARLINK V1.5"

    review.record_variant(conn, image_id, "STARLINK V2 MINI")  # overwrite
    assert review.get_variant(conn, image_id) == "STARLINK V2 MINI"
