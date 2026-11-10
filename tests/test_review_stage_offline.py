"""Offline tests for the review stage: keep/reject/duplicate decisions and
variant labeling. No network, no real satellite imagery."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper import ledger
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


def test_decide_keep_moves_the_file_into_the_master_set_and_remembers_it(conn, tmp_path):
    path = tmp_path / "satellite_images" / "STARLINK" / "a.jpg"
    _make_image(path)
    image_id = _insert_active_image(conn, path)

    review.decide(conn, image_id, "keep")

    row = conn.execute(
        "SELECT status, path, reviewed_at, content_sha256 FROM images WHERE id = ?", (image_id,)
    ).fetchone()
    home = tmp_path / "data" / "images" / "STARLINK" / "a.jpg"  # the ledger sits next to the test database
    assert row["status"] == "active" and row["reviewed_at"] is not None
    assert Path(row["path"]).resolve() == home.resolve() and home.is_file() and not path.exists()
    entry = ledger.entry(conn, row["content_sha256"])
    assert (entry["state"], entry["family"], entry["path"]) == ("kept", "STARLINK", "images/STARLINK/a.jpg")


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


# ---- The same file under several families ----


def _shared_file(conn, tmp_path):
    """One satellite photo downloaded under two families' searches."""
    root = tmp_path / "satellite_images"
    a, b = root / "STARLINK" / "STARLINK_wikimedia_01.jpg", root / "ONEWEB" / "ONEWEB_nasa_03.jpg"
    for path in (a, b):
        _make_image(path)
    ids = [_insert_active_image(conn, a, "STARLINK"), _insert_active_image(conn, b, "ONEWEB")]
    from scraper.stages import dedupe

    dedupe.remove_identical_copies(conn, quarantine_dir=tmp_path / "_quarantine")
    return ids


def _catalog(tmp_path, *families):
    path = tmp_path / "satellites.csv"
    path.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n" + "".join(f"1,X,{f},{f}\n" for f in families))
    return path


def test_queue_asks_which_family(conn, tmp_path):
    first, _ = _shared_file(conn, tmp_path)
    item = next(i for i in review.review_queue(conn) if i["id"] == first)
    assert item["family_choice"] == {"families": ["ONEWEB", "STARLINK"], "copies": 2, "preselected": None}


def test_keep_waits_for_the_family(conn, tmp_path):
    first, _ = _shared_file(conn, tmp_path)
    with pytest.raises(ValueError, match="choose its family"):
        review.decide(conn, first, "keep", tmp_path / "_quarantine")


def test_choose_family_keeps_one_copy_and_moves_it(conn, tmp_path):
    first, second = _shared_file(conn, tmp_path)
    result = review.choose_family(conn, first, "ONEWEB", tmp_path / "_quarantine", _catalog(tmp_path))

    assert result == {"kept": first, "family": "ONEWEB", "quarantined": 1}
    kept = conn.execute("SELECT family, path, status FROM images WHERE id = ?", (first,)).fetchone()
    assert kept["family"] == "ONEWEB" and kept["status"] == "active"
    assert Path(kept["path"]) == tmp_path / "satellite_images" / "ONEWEB" / "STARLINK_wikimedia_01.jpg"
    assert Path(kept["path"]).is_file()
    other = conn.execute("SELECT status, path FROM images WHERE id = ?", (second,)).fetchone()
    assert other["status"] == "rejected_duplicate" and Path(other["path"]).is_file()  # quarantined, not deleted
    assert review.family_choice(conn, first) is None
    review.decide(conn, first, "keep", tmp_path / "_quarantine")  # now it can be kept


def test_choose_family_accepts_any_catalog_family(conn, tmp_path):
    first, _ = _shared_file(conn, tmp_path)
    catalog = _catalog(tmp_path, "IRIDIUM")
    with pytest.raises(ValueError, match="isn't one of"):
        review.choose_family(conn, first, "MADE UP", tmp_path / "_quarantine", catalog)
    assert review.choose_family(conn, first, "IRIDIUM", tmp_path / "_quarantine", catalog)["family"] == "IRIDIUM"


def test_reject_rejects_every_copy(conn, tmp_path):
    first, second = _shared_file(conn, tmp_path)
    review.decide(conn, first, "reject", tmp_path / "_quarantine")
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM images")}
    assert statuses == {first: "rejected_filter", second: "rejected_filter"}


def test_quarantine_never_overwrites(conn, tmp_path):
    a = tmp_path / "x" / "STARLINK" / "same.jpg"
    b = tmp_path / "y" / "STARLINK" / "same.jpg"
    for path in (a, b):
        _make_image(path)
    ids = [_insert_active_image(conn, a), _insert_active_image(conn, b)]
    for image_id in ids:
        review.decide(conn, image_id, "reject", tmp_path / "_quarantine")
    paths = {r["path"] for r in conn.execute("SELECT path FROM images")}
    assert len(paths) == 2 and all(Path(p).is_file() for p in paths)


# ---- Known variants ----


def _variant_catalog(tmp_path):
    catalog = tmp_path / "satellites.csv"
    catalog.write_text(
        "NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n1,GOES 16,GOES,GOES-R SERIES\n2,ONEWEB-1,ONEWEB,ONEWEB\n"
    )
    return catalog


def test_known_variants_are_added_and_merged(tmp_path):
    catalog = _variant_catalog(tmp_path)
    before = catalog.read_text()

    assert review.add_known_variant("GOES", "  GOES  N-P ", catalog) is True  # spacing tidied
    assert review.add_known_variant("ONEWEB", "OneWeb Gen 2", catalog) is True

    assert review.available_variants("GOES", catalog) == ["GOES N-P", "GOES-R SERIES"]
    assert review.available_variants("ONEWEB", catalog) == ["OneWeb Gen 2"]
    assert catalog.read_text() == before  # the catalog itself is never edited
    assert review.load_known_variants(tmp_path / "known_variants.json") == {
        "GOES": ["GOES N-P"],
        "ONEWEB": ["OneWeb Gen 2"],
    }


@pytest.mark.parametrize("variant", ["goes-r series", "GOES N-P", "goes"])
def test_known_variant_already_there_isnt_added_again(tmp_path, variant):
    catalog = _variant_catalog(tmp_path)
    review.add_known_variant("GOES", "GOES N-P", catalog)
    assert review.add_known_variant("GOES", variant, catalog) is False
    assert review.available_variants("GOES", catalog) == ["GOES N-P", "GOES-R SERIES"]


@pytest.mark.parametrize("variant", ["", "   ", "x" * 81])
def test_known_variant_rejects_bad_names(tmp_path, variant):
    with pytest.raises(ValueError):
        review.add_known_variant("GOES", variant, _variant_catalog(tmp_path))


# ---- Reject reasons ----


def test_reject_records_the_reason_and_duplicates_say_so(conn, tmp_path):
    a = _insert_active_image(conn, _make_and_return(tmp_path / "a.jpg"))
    b = _insert_active_image(conn, _make_and_return(tmp_path / "b.jpg"))
    review.decide(conn, a, "reject", tmp_path / "_quarantine", reason="not_recognizable")
    review.decide(conn, b, "duplicate", tmp_path / "_quarantine")
    reasons = dict(conn.execute("SELECT id, reject_reason FROM images").fetchall())
    assert reasons == {a: "not_recognizable", b: "duplicate"}


def test_unknown_reject_reason_is_refused(conn, tmp_path):
    image_id = _insert_active_image(conn, _make_and_return(tmp_path / "a.jpg"))
    with pytest.raises(ValueError, match="reason"):
        review.decide(conn, image_id, "reject", tmp_path / "_quarantine", reason="ugly")
    assert conn.execute("SELECT status FROM images").fetchone()["status"] == "active"


def _make_and_return(path: Path) -> Path:
    _make_image(path)
    return path


# ---- Less junk to sift through: score order, a page at once, the filter's set-asides ----


def _score(conn, image_id, score):
    conn.execute("UPDATE images SET filter_score = ? WHERE id = ?", (score, image_id))
    conn.commit()


def _waiting(conn, tmp_path, name, score=None, family="STARLINK"):
    path = tmp_path / family / name
    _make_image(path)
    image_id = _insert_active_image(conn, path, family=family)
    if score is not None:
        _score(conn, image_id, score)
    return image_id


def test_review_queue_shows_the_most_likely_satellites_first(conn, tmp_path):
    unscreened = _waiting(conn, tmp_path, "a.jpg")
    borderline = _waiting(conn, tmp_path, "b.jpg", 0.6)
    likely = _waiting(conn, tmp_path, "c.jpg", 0.99)

    queue = review.review_queue(conn)

    assert [item["id"] for item in queue] == [likely, borderline, unscreened]
    assert [item["filter_score"] for item in queue] == [0.99, 0.6, None]


def test_queue_counts_say_what_is_waiting_and_what_is_not_screened(conn, tmp_path):
    _waiting(conn, tmp_path, "a.jpg")
    _waiting(conn, tmp_path, "b.jpg", 0.9)
    _waiting(conn, tmp_path, "c.jpg", family="ONEWEB")
    imported = _waiting(conn, tmp_path, "d.jpg")
    conn.execute("UPDATE images SET origin = 'imported' WHERE id = ?", (imported,))  # the filter never screens these
    conn.commit()

    assert review.queue_counts(conn) == {"waiting": 4, "unscreened": 2, "screened_out": 0}
    assert review.queue_counts(conn, family="ONEWEB") == {"waiting": 1, "unscreened": 1, "screened_out": 0}


def test_decide_many_keeps_the_ticked_and_rejects_the_rest(conn, tmp_path):
    keeper = _waiting(conn, tmp_path, "a.jpg", 0.9)
    junk = [_waiting(conn, tmp_path, f"junk{i}.jpg", 0.6) for i in range(3)]

    counts = review.decide_many(conn, [keeper], junk, "not_a_satellite", tmp_path / "_quarantine")

    assert counts == {"kept": 1, "rejected": 3, "skipped": 0}
    statuses = dict(conn.execute("SELECT id, status FROM images"))
    assert statuses[keeper] == "active"
    assert {statuses[i] for i in junk} == {"rejected_filter"}
    assert {row[0] for row in conn.execute("SELECT reject_reason FROM images WHERE id != ?", (keeper,))} == {
        "not_a_satellite"
    }
    assert (tmp_path / "data" / "images" / "STARLINK" / "a.jpg").is_file()
    assert review.review_queue(conn) == []


def test_decide_many_needs_a_reason_and_skips_what_is_already_decided(conn, tmp_path):
    first = _waiting(conn, tmp_path, "a.jpg")
    second = _waiting(conn, tmp_path, "b.jpg")

    with pytest.raises(ValueError, match="Say why"):
        review.decide_many(conn, [], [first], "", tmp_path / "_quarantine")
    with pytest.raises(ValueError, match="both"):
        review.decide_many(conn, [first], [first], "other", tmp_path / "_quarantine")
    assert len(review.review_queue(conn)) == 2  # nothing happened

    review.decide(conn, first, "keep")
    counts = review.decide_many(conn, [], [first, second, 999], "other", tmp_path / "_quarantine")

    assert counts == {"kept": 0, "rejected": 1, "skipped": 2}  # a kept image isn't rejected by a stale page
    assert conn.execute("SELECT status FROM images WHERE id = ?", (first,)).fetchone()[0] == "active"


def test_decide_many_leaves_a_file_in_several_families_for_one_at_a_time(conn, tmp_path):
    first = _waiting(conn, tmp_path, "a.jpg", family="STARLINK")
    second = _waiting(conn, tmp_path, "a.jpg", family="ONEWEB")
    conn.execute("UPDATE images SET content_sha256 = 'same' WHERE id IN (?, ?)", (first, second))
    conn.commit()

    counts = review.decide_many(conn, [first], [second], "other", tmp_path / "_quarantine")

    assert counts == {"kept": 0, "rejected": 0, "skipped": 2}
    assert len(review.review_queue(conn)) == 2


def _screen_out(conn, tmp_path, name, score):
    from scraper.stages.filter import run_filter

    image_id = _waiting(conn, tmp_path, name)
    run_filter(conn, quarantine_dir=tmp_path / "_quarantine", scorer=lambda paths: [score for _ in paths])
    return image_id


def test_screened_out_lists_the_filters_set_asides_closest_calls_first(conn, tmp_path):
    clear_junk = _screen_out(conn, tmp_path, "a.jpg", 0.05)
    close_call = _screen_out(conn, tmp_path, "b.jpg", 0.5)
    by_a_person = _waiting(conn, tmp_path, "c.jpg", 0.9)
    review.decide(conn, by_a_person, "reject", tmp_path / "_quarantine", "other")

    assert [image["id"] for image in review.screened_out(conn)] == [close_call, clear_junk]
    assert review.queue_counts(conn)["screened_out"] == 2


def test_keep_after_all_overrules_the_filter(conn, tmp_path):
    image_id = _screen_out(conn, tmp_path, "a.jpg", 0.5)
    assert not (tmp_path / "STARLINK" / "a.jpg").exists()

    review.keep_after_all(conn, image_id)

    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    assert (row["status"], row["reject_reason"]) == ("active", None)
    assert row["reviewed_at"] is not None  # kept by a person: out of the queue, and the filter leaves it alone
    assert (tmp_path / "data" / "images" / "STARLINK" / "a.jpg").is_file()
    assert ledger.entry(conn, row["content_sha256"])["state"] == "kept"
    assert review.screened_out(conn) == []

    with pytest.raises(ValueError, match="wasn't set aside by the filter"):
        review.keep_after_all(conn, image_id)
