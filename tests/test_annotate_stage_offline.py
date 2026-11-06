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
        (["Dropdown", ["Small", "Large"]], ""),  # the blank starting choice
        (["DropdownOther", ["Rectangular"]], ""),
    ],
)
def test_normalize_value_rejects(spec, raw):
    with pytest.raises(ValueError):
        annotate.normalize_value(spec, raw)


@pytest.mark.parametrize(
    "spec",
    [
        ["Text Box"],
        ["Long Text"],
        ["Dropdown", ["Small", "Large"]],
        ["DropdownOther", ["Rectangular"]],
        ["Number", 0, 10],
        ["Slider"],
        ["Rating Scale"],
        ["Boolean"],
        ["Date"],
        ["Time"],
        ["Date-Time"],
    ],
)
@pytest.mark.parametrize("raw", ["Unknown", "unknown", " UNKNOWN "])
def test_every_attribute_accepts_unknown_in_one_spelling(spec, raw):
    assert annotate.normalize_value(spec, raw) == "Unknown"


def test_multi_select_unknown_stays_a_list():
    spec = ["Multi-Select", ["Patch", "Dipole"]]
    assert annotate.normalize_value(spec, ["Unknown"]) == ["Unknown"]
    assert annotate.normalize_value(spec, "unknown") == ["Unknown"]
    assert annotate.normalize_value(spec, []) == ["None"]  # "None" still means "looked, not there"
    with pytest.raises(ValueError):
        annotate.normalize_value(spec, ["Unknown", "Patch"])


def test_radio_unsure_is_already_unknown():
    assert annotate.normalize_value(["Check Button"], "Unknown") == "Unknown"


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


def test_submitted_images_leave_everyones_queue_by_default(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)

    assert annotate.annotation_queue(conn, "wesam")["remaining"] == 0
    assert annotate.annotation_queue(conn, "someone_else")["remaining"] == 0


def test_second_annotator_can_relabel_when_not_skipping_others(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    annotate.submit_annotation(conn, image_id, "wesam", GOOD_VALUES, SCHEMA)

    assert annotate.annotation_queue(conn, "wesam", skip_annotated_by_others=False)["remaining"] == 0
    assert annotate.annotation_queue(conn, "someone_else", skip_annotated_by_others=False)["remaining"] == 1


def test_variant_label_does_not_count_as_annotated(conn, tmp_path):
    image_id = _insert_image(conn, tmp_path)
    review.record_variant(conn, image_id, "STARLINK V2")
    assert annotate.annotation_queue(conn, "wesam")["remaining"] == 1


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


# ---- Import from the old tool ----

LEGACY_HEADER = [
    "name", "Shape", "Docking/Grappling Fixtures", "Antennae", "Manufacturer",
    "Deployment Mechanism", "Mass", "Propellant Mechanism", "Retired Column",
]  # fmt: skip

IMPORT_SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Docking Systems": ["DropdownOther", ["None", "Docking Rings"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
        "Solar Panel Configuration": ["DropdownOther", ["None", "Double Wing", "Surface"]],
        "Manufacturer": ["Text Box"],
    },
    "Other": {
        "Deployment Mechanisms": ["Multi-Select", ["Boom Arm", "Solar Gimbals", "Sail Deployer"]],
        "Mass": ["Dropdown", ["Small", "Large"]],
        "Propulsion Mechanism": ["Multi-Select", ["Monopropellant", "Ion Propulsion"]],
    },
}


def _write_legacy_csv(path: Path, rows: list[list[str]]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(LEGACY_HEADER)
        writer.writerows(rows)


GOOD_ROW = [
    "a.jpg", "Rectangular", "Docking Rings", "['Patch', 'Dipole']", "Lockheed",
    "Solar Gimbals and Sail Deployer", "Small", "['Monoropellant']", "x",
]  # fmt: skip
UNUSABLE_ROW = ["b.jpg", *["UNUSABLE"] * 7, ""]
# The old writer's bug: values from a different attribute order under this header.
SHIFTED_ROW = ["c.jpg", "Rectangular", "None", "['Patch']", "Double Wing", "Wrap", "Small", "['None']", ""]
UNMATCHED_ROW = ["not_in_db.jpg", "Rectangular", "None", "['Patch']", "Unknown", "None", "Small", "['None']", ""]


def test_map_legacy_columns_follows_renames():
    mapping, unmapped = annotate.map_legacy_columns(LEGACY_HEADER, IMPORT_SCHEMA)
    assert mapping["Antennae"] == "Antennas"
    assert mapping["Docking/Grappling Fixtures"] == "Docking Systems"
    assert mapping["Deployment Mechanism"] == "Deployment Mechanisms"
    assert mapping["Propellant Mechanism"] == "Propulsion Mechanism"
    assert unmapped == ["Retired Column"]


@pytest.mark.parametrize(
    "text,expected",
    [("['Patch', 'Omni']", ["Patch", "Omni"]), ("Rectangular", "Rectangular"), ("[broken", "[broken"), ("", "")],
)
def test_parse_legacy_cell(text, expected):
    assert annotate.parse_legacy_cell(text) == expected


def test_misplaced_values_spots_other_attributes_options():
    assert annotate.misplaced_values("Manufacturer", "Double Wing", IMPORT_SCHEMA) == ["Double Wing"]
    assert annotate.misplaced_values("Manufacturer", "Double Wing and Surface", IMPORT_SCHEMA) == [
        "Double Wing",
        "Surface",
    ]
    assert annotate.misplaced_values("Manufacturer", "Lockheed", IMPORT_SCHEMA) == []
    assert annotate.misplaced_values("Antennas", ["None"], IMPORT_SCHEMA) == []
    assert annotate.misplaced_values("Shape", "UNUSABLE", IMPORT_SCHEMA) == []


def test_import_legacy_csv(conn, tmp_path):
    a = _insert_image(conn, tmp_path, "a.jpg")
    b = _insert_image(conn, tmp_path, "b.jpg")
    c = _insert_image(conn, tmp_path, "c.jpg")
    old_csv = tmp_path / "annotatedInfo.csv"
    _write_legacy_csv(old_csv, [GOOD_ROW, UNUSABLE_ROW, SHIFTED_ROW, UNMATCHED_ROW])
    review_path = tmp_path / "review.csv"

    result = annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, review_path=review_path)

    assert result["rows"] == 4
    assert result["imported"] == 2
    assert result["misaligned"] == 1
    assert result["unmatched"] == 1
    assert result["unmapped_columns"] == ["Retired Column"]
    assert result["missing_attributes"] == ["Solar Panel Configuration"]

    imported = annotate.get_annotation(conn, a, "legacy_miat")
    assert imported["Antennas"] == ["Patch", "Dipole"]
    assert imported["Deployment Mechanisms"] == ["Solar Gimbals", "Sail Deployer"]  # text -> multi-select list
    assert imported["Propulsion Mechanism"] == ["Monopropellant"]  # old tool's own option spelling, renamed
    assert imported["Manufacturer"] == "Lockheed"
    assert annotate.get_annotation(conn, b, "legacy_miat")["Shape"] == "UNUSABLE"
    assert annotate.get_annotation(conn, c, "legacy_miat") == {}  # shifted row skipped, not guessed at

    versions = {r["guide_version"] for r in conn.execute("SELECT guide_version FROM annotations")}
    assert versions == {"legacy-miat"}

    with open(review_path, newline="", encoding="utf-8") as f:
        reviewed = {row["name"]: row["reason"] for row in csv.DictReader(f)}
    assert set(reviewed) == {"c.jpg", "not_in_db.jpg"}
    assert "Manufacturer: Double Wing" in reviewed["c.jpg"]

    # Imported images count as done for everyone; the skipped one still needs doing.
    assert [item["id"] for item in annotate.annotation_queue(conn, "wesam")["queue"]] == [c]


def test_import_dry_run_writes_nothing_to_the_db(conn, tmp_path):
    _insert_image(conn, tmp_path, "a.jpg")
    old_csv = tmp_path / "annotatedInfo.csv"
    _write_legacy_csv(old_csv, [GOOD_ROW])

    result = annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, dry_run=True, review_path=None)
    assert result["imported"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM annotations").fetchone()["n"] == 0


def test_import_never_overwrites_and_is_safe_to_rerun(conn, tmp_path):
    a = _insert_image(conn, tmp_path, "a.jpg")
    old_csv = tmp_path / "annotatedInfo.csv"
    _write_legacy_csv(old_csv, [GOOD_ROW])
    annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, annotator="wesam", review_path=None)
    annotate.submit_annotation(conn, a, "wesam", {}, IMPORT_SCHEMA, unusable=True)

    again = annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, annotator="wesam", review_path=None)
    assert again["imported"] == 0
    assert again["already_annotated"] == 1
    assert annotate.get_annotation(conn, a, "wesam")["Shape"] == "UNUSABLE"


def test_import_skips_file_names_shared_by_several_images(conn, tmp_path):
    _insert_image(conn, tmp_path, "a.jpg", family="GPS")
    _insert_image(conn, tmp_path, "a.jpg", family="GOES")
    old_csv = tmp_path / "annotatedInfo.csv"
    _write_legacy_csv(old_csv, [GOOD_ROW])
    result = annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, review_path=None)
    assert result["ambiguous"] == 1
    assert result["imported"] == 0


def test_import_counts_values_that_dont_fit_the_current_schema(conn, tmp_path):
    _insert_image(conn, tmp_path, "a.jpg")
    old_csv = tmp_path / "annotatedInfo.csv"
    row = [*GOOD_ROW]
    row[3] = "['Patch', 'Plannar']"  # an old option since dropped
    _write_legacy_csv(old_csv, [row])
    result = annotate.import_legacy_csv(conn, old_csv, IMPORT_SCHEMA, review_path=None)
    assert result["imported"] == 1
    assert result["off_schema"] == {"Antennas": 1}


@pytest.mark.parametrize("problem", ["missing file", "no name column", "empty schema"])
def test_import_rejects_bad_input(conn, tmp_path, problem):
    old_csv = tmp_path / "annotatedInfo.csv"
    schema = IMPORT_SCHEMA
    if problem == "no name column":
        old_csv.write_text("Shape\nRectangular\n")
    elif problem == "empty schema":
        _write_legacy_csv(old_csv, [GOOD_ROW])
        schema = {}
    with pytest.raises(ValueError):
        annotate.import_legacy_csv(conn, old_csv, schema, review_path=None)
