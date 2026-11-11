"""Offline tests for bringing in another machine's annotations: images are
matched by path or contents, each annotator's answers stay their own, the
newer answers win, and the other machine's file is never changed."""

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import annotate, merge_annotations

EARLY, LATE = "2026-11-01T10:00:00+00:00", "2026-11-09T10:00:00+00:00"
SCHEMA = {"Things": {"Shape": ["Dropdown", ["Rectangular", "Cylindrical"]], "Mass": ["Dropdown", ["Small", "Large"]]}}


def _database(folder: Path) -> sqlite3.Connection:
    folder.mkdir(parents=True, exist_ok=True)
    conn = get_connection(folder / "scraper.db")
    apply_migrations(conn)
    return conn


@pytest.fixture
def here(tmp_path):
    conn = _database(tmp_path / "here")
    yield conn
    conn.close()


@pytest.fixture
def there(tmp_path):
    conn = _database(tmp_path / "there")
    yield conn
    conn.close()


def _image(conn, path, family="ESSA", sha=None, status="active") -> int:
    cur = conn.execute(
        "INSERT INTO images (family, path, status, content_sha256, created_at) VALUES (?, ?, ?, ?, ?)",
        (family, path, status, sha, EARLY),
    )
    conn.commit()
    return cur.lastrowid


def _answers(conn, image_id, annotator, saved=EARLY, **values):
    conn.executemany(
        """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
           VALUES (?, ?, ?, ?, 'v1', ?)""",
        [(image_id, attribute, json.dumps(value), annotator, saved) for attribute, value in values.items()],
    )
    conn.commit()


def _other(there) -> Path:
    return Path(there.execute("PRAGMA database_list").fetchone()[2])


def _merge(here, there, **kwargs):
    return merge_annotations.merge_from_database(here, _other(there), **kwargs)


def test_another_machines_answers_are_added_under_the_annotators_name(here, there):
    mine = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "data/images/ESSA/a.jpg", sha="aaa")
    _answers(there, theirs, "alice", saved=LATE, Shape="Cylindrical", Mass="Large")

    dry = _merge(here, there)  # a dry run unless told otherwise
    assert (dry["dry_run"], dry["sets"], dry["added"]) == (True, 1, 1)
    assert annotate.get_annotation(here, mine, "alice") == {}

    real = _merge(here, there, dry_run=False)
    assert real["added"] == 1
    assert real["annotators"]["alice"]["added"] == 1
    assert annotate.get_annotation(here, mine, "alice") == {"Shape": "Cylindrical", "Mass": "Large"}
    row = here.execute("SELECT guide_version, created_at FROM annotations WHERE annotator = 'alice'").fetchone()
    assert (row["guide_version"], row["created_at"]) == ("v1", LATE)  # as saved there, not stamped with now
    assert annotate.annotator_names(here) == ["alice"]

    again = _merge(here, there, dry_run=False)
    assert (again["added"], again["already_here"], again["backup_path"]) == (0, 1, None)


def test_a_moved_image_is_matched_by_its_contents(here, there):
    """Review's Keep moves a file, so the path there can be an old one."""
    mine = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "satellite_images/ESSA/a.jpg", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    assert _merge(here, there, dry_run=False)["added"] == 1
    assert annotate.get_annotation(here, mine, "alice") == {"Shape": "Cylindrical"}


def test_the_same_path_with_different_contents_is_not_the_same_image(here, there):
    _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "data/images/ESSA/a.jpg", sha="zzz")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    result = _merge(here, there, dry_run=False)
    assert (result["added"], result["unmatched"]) == (0, 1)
    assert result["skipped"][0]["image"] == "data/images/ESSA/a.jpg"


def test_a_file_under_two_families_goes_to_the_same_family(here, there):
    essa = _image(here, "data/images/ESSA/a.jpg", family="ESSA", sha="aaa")
    tiros = _image(here, "data/images/TIROS/a.jpg", family="TIROS", sha="aaa")
    theirs = _image(there, "old/TIROS/a.jpg", family="TIROS", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    assert _merge(here, there, dry_run=False)["added"] == 1
    assert annotate.get_annotation(here, tiros, "alice") == {"Shape": "Cylindrical"}
    assert annotate.get_annotation(here, essa, "alice") == {}


def test_copies_nothing_tells_apart_are_skipped_not_guessed(here, there):
    _image(here, "data/images/ESSA/a.jpg", family="ESSA", sha="aaa")
    _image(here, "data/images/TIROS/a.jpg", family="TIROS", sha="aaa")
    theirs = _image(there, "old/NIMBUS/a.jpg", family="NIMBUS", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    result = _merge(here, there, dry_run=False)
    assert (result["added"], result["ambiguous"]) == (0, 1)
    assert here.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0


def test_a_set_aside_copy_loses_to_the_active_one(here, there):
    _image(here, "satellite_images/ESSA/a.jpg", sha="aaa", status="rejected_duplicate")
    kept = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "elsewhere/a.jpg", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    assert _merge(here, there, dry_run=False)["added"] == 1
    assert annotate.get_annotation(here, kept, "alice") == {"Shape": "Cylindrical"}


def test_the_answers_saved_last_win_for_the_same_annotator(here, there, tmp_path):
    first = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    second = _image(here, "data/images/ESSA/b.jpg", sha="bbb")
    _answers(here, first, "alice", saved=EARLY, Shape="Rectangular", Mass="Small")
    _answers(here, second, "alice", saved=LATE, Shape="Rectangular")
    _answers(here, first, "bob", saved=LATE, Shape="Rectangular")
    their_first = _image(there, "data/images/ESSA/a.jpg", sha="aaa")
    their_second = _image(there, "data/images/ESSA/b.jpg", sha="bbb")
    _answers(there, their_first, "alice", saved=LATE, Shape="Cylindrical")  # a later correction
    _answers(there, their_second, "alice", saved=EARLY, Shape="Cylindrical")  # older than what is here

    result = _merge(here, there, dry_run=False, review_path=tmp_path / "review.csv")

    assert (result["updated"], result["kept_newer_here"]) == (1, 1)
    # All of alice's answers for the image are replaced together: Mass is gone.
    assert annotate.get_annotation(here, first, "alice") == {"Shape": "Cylindrical"}
    assert annotate.get_annotation(here, second, "alice") == {"Shape": "Rectangular"}
    assert annotate.get_annotation(here, first, "bob") == {"Shape": "Rectangular"}  # other people untouched
    with open(result["review_path"], newline="", encoding="utf-8") as f:
        review = list(csv.DictReader(f))
    assert [row["image"] for row in review] == ["data/images/ESSA/b.jpg"]

    # The database was backed up before it was changed, with the old answers.
    backup = sqlite3.connect(result["backup_path"])
    old = backup.execute(
        "SELECT value FROM annotations WHERE annotator = 'alice' AND image_id = ? AND attribute = 'Shape'", (first,)
    ).fetchone()[0]
    backup.close()
    assert json.loads(old) == "Rectangular"


def test_reviews_notes_and_variant_picks_stay_behind(here, there):
    mine = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "data/images/ESSA/a.jpg", sha="aaa")
    _answers(there, theirs, "human_review", Shape="Cylindrical")
    _answers(there, theirs, "alice", variant="ESSA-9", Shape="Cylindrical")
    _answers(here, mine, "alice", saved=EARLY, variant="ESSA-1")

    result = _merge(here, there, dry_run=False)

    assert (result["sets"], result["added"]) == (1, 1)
    saved = {
        (row["annotator"], row["attribute"]): json.loads(row["value"])
        for row in here.execute("SELECT annotator, attribute, value FROM annotations")
    }
    assert saved == {("alice", "variant"): "ESSA-1", ("alice", "Shape"): "Cylindrical"}


def test_two_images_there_that_are_one_image_here(here, there):
    mine = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    older = _image(there, "satellite_images/ESSA/a.jpg", sha="aaa")
    newer = _image(there, "satellite_images/ESSA/a_copy.jpg", sha="aaa")
    _answers(there, older, "alice", saved=EARLY, Shape="Rectangular")
    _answers(there, newer, "alice", saved=LATE, Shape="Cylindrical")
    result = _merge(here, there, dry_run=False)
    assert (result["added"], result["updated"]) == (1, 1)
    assert annotate.get_annotation(here, mine, "alice") == {"Shape": "Cylindrical"}


def test_attributes_that_arent_in_todays_list_are_counted_and_kept(here, there):
    mine = _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "data/images/ESSA/a.jpg", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical", Texture="Rough")
    result = _merge(here, there, schema=SCHEMA, dry_run=False)
    assert result["unknown_attributes"] == {"Texture": 1}
    assert annotate.get_annotation(here, mine, "alice")["Texture"] == "Rough"


def test_the_other_file_is_not_changed_and_unsaved_pages_are_read(here, there):
    """The other database is still open (as if the app there was never quit):
    its latest labels are in the -wal file, which is read from a copy."""
    _image(here, "data/images/ESSA/a.jpg", sha="aaa")
    theirs = _image(there, "data/images/ESSA/a.jpg", sha="aaa")
    _answers(there, theirs, "alice", Shape="Cylindrical")
    other = _other(there)
    assert other.with_name(other.name + "-wal").stat().st_size > 0
    before = {p.name: p.read_bytes() for p in other.parent.iterdir() if p.is_file()}

    assert _merge(here, there, dry_run=False)["added"] == 1

    assert {p.name: p.read_bytes() for p in other.parent.iterdir() if p.is_file()} == before


def test_bad_sources_are_refused(here, tmp_path):
    with pytest.raises(merge_annotations.MergeError, match="No database"):
        merge_annotations.merge_from_database(here, tmp_path / "nope.db")
    with pytest.raises(merge_annotations.MergeError, match="own database"):
        merge_annotations.merge_from_database(here, tmp_path / "here" / "scraper.db")
    not_a_database = tmp_path / "notes.db"
    not_a_database.write_text("this is not a database, just text " * 40)
    with pytest.raises(merge_annotations.MergeError, match="isn't a Ground Station database"):
        merge_annotations.merge_from_database(here, not_a_database)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(merge_annotations.MergeError, match="isn't a Ground Station database"):
        merge_annotations.merge_from_database(here, empty)
