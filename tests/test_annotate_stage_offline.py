"""Offline tests for the annotate stage: attribute schema validation, value
normalization (same rules as the old PyQt Attribute Annotation Tool), the
annotation queue, submit, and CSV export. No network, no real imagery."""

import ast
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import annotate, review

SCHEMA = {
    "Highly-Distinct": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole", "Horn"]],
        "Manufacturer": ["Text Box"],
    },
    "Non-Distinct": {
        "Mass": ["Dropdown", ["Small", "Large"]],
        "Has Logo": ["Check Button"],
    },
}

GOOD_VALUES = {
    "Shape": "Rectangular",
    "Antennas": ["Dipole", "Patch"],
    "Manufacturer": "  SpaceX ",
    "Mass": "Small",
    "Has Logo": "Present",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _insert_image(conn, tmp_path: Path, name="sat.jpg", family="STARLINK", reviewed=True, status="active"):
    path = tmp_path / family / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)
    cur = conn.execute(
        "INSERT INTO images (family, path, status, reviewed_at, created_at) VALUES (?, ?, ?, ?, ?)",
        (family, str(path), status, _now() if reviewed else None, _now()),
    )
    conn.commit()
    return cur.lastrowid


# ---- Schema ----


def test_validate_schema_accepts_the_shipped_attributes_file():
    shipped = Path(__file__).parent.parent / "attributes.json"
    schema = annotate.load_schema(shipped)
    assert "Highly-Distinct" in schema
    assert annotate.flat_attributes(schema)


def test_validate_schema_canonicalizes_lowercase_type_names():
    # The old Admin Panel wrote new attributes as "text box", which its own
    # annotation screen then didn't recognize.
    cleaned = annotate.validate_schema({"Cat": {"Notes": ["text box"]}})
    assert cleaned == {"Cat": {"Notes": ["Text Box"]}}


def test_validate_schema_strips_blank_options():
    cleaned = annotate.validate_schema({"Cat": {"Mass": ["Dropdown", ["Small", " ", "Large "]]}})
    assert cleaned["Cat"]["Mass"] == ["Dropdown", ["Small", "Large"]]


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {"Cat": []},
        {"": {"A": ["Text Box"]}},
        {"Cat": {"A": ["Not A Type"]}},
        {"Cat": {"A": "Text Box"}},
        {"Cat": {"A": ["Dropdown", []]}},
        {"Cat": {"A": ["Dropdown", ["x", "x"]]}},
        {"Cat": {"A": ["Slider", 10, 5]}},
        {"Cat": {"A": ["Number", "low", 5]}},
        {"One": {"A": ["Text Box"]}, "Two": {"A": ["Text Box"]}},
        {"Cat": {"variant": ["Text Box"]}},
    ],
)
def test_validate_schema_rejects_bad_input(bad):
    with pytest.raises(annotate.SchemaError):
        annotate.validate_schema(bad)


def test_save_and_load_schema_round_trip_keeps_order(tmp_path):
    path = tmp_path / "attributes.json"
    annotate.save_schema(SCHEMA, path)
    loaded = annotate.load_schema(path)
    assert loaded == SCHEMA
    assert list(loaded) == list(SCHEMA)
    assert list(loaded["Highly-Distinct"]) == ["Shape", "Antennas", "Manufacturer"]
    assert not path.with_suffix(".json.tmp").exists()


def test_load_schema_missing_file_is_empty(tmp_path):
    assert annotate.load_schema(tmp_path / "nope.json") == {}


def test_load_schema_reports_invalid_json(tmp_path):
    path = tmp_path / "attributes.json"
    path.write_text("{not json")
    with pytest.raises(annotate.SchemaError):
        annotate.load_schema(path)


def test_schema_version_changes_with_content():
    assert annotate.schema_version(SCHEMA) == annotate.schema_version(json.loads(json.dumps(SCHEMA)))
    changed = json.loads(json.dumps(SCHEMA))
    changed["Non-Distinct"]["Mass"][1].append("Medium")
    assert annotate.schema_version(changed) != annotate.schema_version(SCHEMA)


# ---- Values ----


def test_normalize_values_applies_the_old_tools_rules():
    values = annotate.normalize_values(SCHEMA, {**GOOD_VALUES, "Antennas": []})
    assert values["Antennas"] == ["None"]  # empty multi-select
    assert values["Manufacturer"] == "SpaceX"  # stripped


def test_multi_select_keeps_schema_order():
    values = annotate.normalize_values(SCHEMA, GOOD_VALUES)
    assert values["Antennas"] == ["Patch", "Dipole"]


def test_dropdown_other_accepts_free_text():
    values = annotate.normalize_values(SCHEMA, {**GOOD_VALUES, "Shape": "Hexagonal"})
    assert values["Shape"] == "Hexagonal"


def test_normalize_values_reports_every_bad_field_at_once():
    with pytest.raises(annotate.AnnotationError) as exc:
        annotate.normalize_values(SCHEMA, {"Shape": "", "Antennas": ["Laser"], "Mass": "Huge"})
    assert set(exc.value.errors) == {"Shape", "Antennas", "Manufacturer", "Mass", "Has Logo"}


def test_unusable_marks_every_attribute():
    values = annotate.normalize_values(SCHEMA, {}, unusable=True)
    assert values == {name: "UNUSABLE" for name in annotate.flat_attributes(SCHEMA)}


@pytest.mark.parametrize(
    "spec,raw,expected",
    [
        (["Number", 0, 10], 7, 7),
        (["Slider"], "42", 42),
        (["Rating Scale"], 5, 5),
        (["Boolean"], False, False),
        (["Date"], "2026-11-02", "2026-11-02"),
        (["Time"], "14:30", "14:30"),
        (["Date-Time"], "2026-11-02T14:30", "2026-11-02T14:30"),
        (["Single-Select"], "Not Present", "Not Present"),
        (["Long Text"], " notes ", "notes"),
    ],
)
def test_normalize_value_per_type(spec, raw, expected):
    assert annotate.normalize_value(spec, raw) == expected


@pytest.mark.parametrize(
    "spec,raw",
    [
        (["Number", 0, 10], 11),
        (["Rating Scale"], None),
        (["Rating Scale"], 6),
        (["Boolean"], "yes"),
        (["Date"], "02/11/2026"),
        (["Check Button"], "Maybe"),
        (["Text Box"], "   "),
    ],
)
def test_normalize_value_rejects(spec, raw):
    with pytest.raises(ValueError):
        annotate.normalize_value(spec, raw)


@pytest.mark.parametrize("name", ["", "   ", "human_review", "x" * 65])
def test_validate_annotator_rejects(name):
    with pytest.raises(ValueError):
        annotate.validate_annotator(name)


# ---- Queue + submit ----


def test_queue_only_includes_reviewed_active_images_by_default(conn, tmp_path):
    kept = _insert_image(conn, tmp_path, "kept.jpg")
    _insert_image(conn, tmp_path, "unreviewed.jpg", reviewed=False)
    _insert_image(conn, tmp_path, "rejected.jpg", status="rejected_filter")

    result = annotate.annotation_queue(conn, "wesam")
    assert [item["id"] for item in result["queue"]] == [kept]
    assert result["queue"][0]["name"] == "kept.jpg"
    assert result["remaining"] == 1

    assert annotate.annotation_queue(conn, "wesam", reviewed_only=False)["remaining"] == 2


def test_queue_filters_by_family(conn, tmp_path):
    _insert_image(conn, tmp_path, family="STARLINK")
    other = _insert_image(conn, tmp_path, family="GPS")
    result = annotate.annotation_queue(conn, "wesam", family="GPS")
    assert [item["id"] for item in result["queue"]] == [other]


def test_submitted_images_leave_only_that_annotators_queue(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)

    assert annotate.annotation_queue(conn, "wesam")["remaining"] == 0
    assert annotate.annotation_queue(conn, "someone_else")["remaining"] == 1


def test_variant_label_does_not_count_as_annotated(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    review.record_variant(conn, image_id, "STARLINK V2")
    assert annotate.annotation_queue(conn, "human_review_but_not_really")["remaining"] == 1


def test_submit_stores_json_values_with_schema_version(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)

    assert annotate.get_annotation(conn, image_id, "wesam") == {
        "Shape": "Rectangular",
        "Antennas": ["Patch", "Dipole"],
        "Manufacturer": "SpaceX",
        "Mass": "Small",
        "Has Logo": "Present",
    }
    versions = {r["guide_version"] for r in conn.execute("SELECT guide_version FROM annotations")}
    assert versions == {annotate.schema_version(SCHEMA)}


def test_resubmitting_overwrites_and_leaves_variant_alone(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    review.record_variant(conn, image_id, "STARLINK V2")
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)
    annotate.submit_annotation(conn, image_id, "wesam", {**GOOD_VALUES, "Mass": "Large"}, SCHEMA)

    assert annotate.get_annotation(conn, image_id, "wesam")["Mass"] == "Large"
    assert review.get_variant(conn, image_id) == "STARLINK V2"
    count = conn.execute("SELECT COUNT(*) AS n FROM annotations WHERE annotator = 'wesam'").fetchone()["n"]
    assert count == len(annotate.flat_attributes(SCHEMA))


def test_invalid_submit_writes_nothing(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    with pytest.raises(annotate.AnnotationError):
        annotate.submit_annotation(conn, image_id, "wesam", {"Shape": "Rectangular"}, SCHEMA)
    assert conn.execute("SELECT COUNT(*) AS n FROM annotations").fetchone()["n"] == 0


def test_submit_unknown_image_or_empty_schema_raises(conn, tmp_path):
    with pytest.raises(ValueError):
        annotate.submit_annotation(conn, 999, "wesam", GOOD_VALUES, SCHEMA)
    image_id = _insert_image(conn, tmp_path)
    with pytest.raises(ValueError):
        annotate.submit_annotation(conn, image_id, "wesam", {}, {})


# ---- Export ----


def test_export_matches_the_old_csv_layout(conn, tmp_path):
    first = _insert_image(conn, tmp_path, "a.jpg", family="GPS")
    second = _insert_image(conn, tmp_path, "b.jpg", family="GPS")
    annotate.submit_annotation(conn, first, "wesam", GOOD_VALUES, SCHEMA)
    annotate.submit_annotation(conn, second, "wesam", {}, SCHEMA, unusable=True)

    out = tmp_path / "export" / "annotations.csv"
    assert annotate.export_annotations(conn, SCHEMA, out) == 2

    with open(out, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames[:6] == ["name", "Shape", "Antennas", "Manufacturer", "Mass", "Has Logo"]
    assert reader.fieldnames[6:] == ["family", "image_id", "annotator", "guide_version", "annotated_at"]

    a, b = rows
    assert a["name"] == "a.jpg"
    assert ast.literal_eval(a["Antennas"]) == ["Patch", "Dipole"]  # str(list), like the old tool
    assert a["Manufacturer"] == "SpaceX"
    assert b["Shape"] == "UNUSABLE"


def test_export_keeps_columns_aligned_after_schema_change(conn, tmp_path):
    # The old append-only writer took its header from the first row, so a
    # later attribute-order change silently shifted every following row.
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)

    reordered = {
        "Non-Distinct": {**SCHEMA["Non-Distinct"], "Paint": ["Text Box"]},
        "Highly-Distinct": SCHEMA["Highly-Distinct"],
    }
    out = tmp_path / "annotations.csv"
    annotate.export_annotations(conn, reordered, out)
    with open(out, newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["Mass"] == "Small"
    assert row["Shape"] == "Rectangular"
    assert row["Paint"] == ""


def test_export_can_filter_by_annotator(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)
    annotate.submit_annotation(conn, image_id, "alex", GOOD_VALUES, SCHEMA)
    review.record_variant(conn, image_id, "STARLINK V2")

    out = tmp_path / "annotations.csv"
    assert annotate.export_annotations(conn, SCHEMA, out) == 2
    assert annotate.export_annotations(conn, SCHEMA, out, annotator="alex") == 1
    assert annotate.annotation_counts(conn) == {"images": 1, "annotators": 2}
