"""Offline tests for fixing an old-tool CSV's shifted rows: a known order is
applied only when it is the one explanation for a row, and everything else
is left exactly as it was."""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import annotate, fix_csv

SHIPPED_SCHEMA = annotate.load_schema(Path(__file__).resolve().parent.parent / "attributes.json")

# The old tool's header, with its old column names.
OLD_HEADER = [
    "name",
    "Shape",
    "Solar Panel Configuration",
    "Docking/Grappling Fixtures",
    "Antennae",
    "Passive External Sensors",
    "Active External Sensor",
    "Windows or Optical Apertures",
    "Manufacturer",
    "Surface Finish",
    "Deployment Mechanism",
    "Insulation Type",
    "Mass",
    "Propellant Mechanism",
    "Color",
]
# One satellite's answers, by today's attribute names.
ANSWERS = {
    "Shape": "Rectangular",
    "Solar Panel Configuration": "Double Wing",
    "Docking Systems": "Docking Rings",
    "Antennas": "['Patch', 'Omni']",
    "Passive External Sensors": "['Camera']",
    "Active External Sensors": "['None']",
    "Windows or Optical Apertures": "['Circular']",
    "Manufacturer": "ESSA",
    "Surface Finish": "['Shiny']",
    "Insulation Type": "['Wrap']",
    "Deployment Mechanisms": "Boom Arm",
    "Mass": "Small",
    "Propulsion Mechanism": "['None']",
    "Color": "Gold",
}
HEADER_ATTRIBUTES = [annotate.LEGACY_COLUMN_ALIASES.get(column, column) for column in OLD_HEADER[1:]]
RIGHT_ROW = [ANSWERS[attribute] for attribute in HEADER_ATTRIBUTES]
SHIFTED_ROW = [ANSWERS[a] for a in fix_csv.KNOWN_ROW_ORDERS["windows-and-manufacturer-early"]["order"]]

# A small attribute list for the cases the real one doesn't produce.
SMALL = {
    "Things": {
        "Shape": ["Dropdown", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
        "Mass": ["Dropdown", ["Small", "Large"]],
        "Manufacturer": ["Text Box"],
    }
}
SMALL_HEADER = ["name", "Shape", "Antennas", "Mass", "Manufacturer"]


def _write(path: Path, header: list[str], rows: list[list[str]], encoding="utf-8") -> Path:
    with open(path, "w", newline="", encoding=encoding) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _rows(path: Path, encoding="utf-8") -> list[list[str]]:
    with open(path, newline="", encoding=encoding) as f:
        return list(csv.reader(f))


def test_the_known_order_matches_the_shipped_attribute_list():
    """A renamed attribute would quietly switch the fix off."""
    for fix in fix_csv.KNOWN_ROW_ORDERS.values():
        assert sorted(fix["order"]) == sorted(annotate.flat_attributes(SHIPPED_SCHEMA))


def test_a_shifted_row_is_put_back_and_the_rest_left_alone(tmp_path):
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["a.jpg", *RIGHT_ROW], ["b.jpg", *SHIFTED_ROW]])
    before = original.read_bytes()

    result = fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA, report_path=tmp_path / "report.csv")

    assert (result["rows"], result["fixed"], result["unchanged"], result["left"]) == (2, 1, 1, 0)
    assert result["by_fix"] == {"windows-and-manufacturer-early": 1}
    assert original.read_bytes() == before  # the original is never changed
    fixed = _rows(Path(result["out_path"]))
    assert Path(result["out_path"]) == tmp_path / "old_fixed.csv"
    assert fixed == [OLD_HEADER, ["a.jpg", *RIGHT_ROW], ["b.jpg", *RIGHT_ROW]]
    report = _rows(tmp_path / "report.csv")
    assert report[0] == fix_csv.REPORT_COLUMNS
    assert report[1][:4] == ["2", "b.jpg", "fixed", "windows-and-manufacturer-early"]


def test_fixing_the_fixed_copy_changes_nothing(tmp_path):
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["b.jpg", *SHIFTED_ROW]])
    first = fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA)
    again = fix_csv.fix_legacy_csv(Path(first["out_path"]), SHIPPED_SCHEMA, dry_run=True)
    assert (again["fixed"], again["left"], again["unchanged"]) == (0, 0, 1)


def test_a_dry_run_writes_nothing(tmp_path):
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["b.jpg", *SHIFTED_ROW]])
    result = fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA, report_path=tmp_path / "report.csv", dry_run=True)
    assert result["fixed"] == 1
    assert result["out_path"] is None and result["report_path"] is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.csv"]


def test_a_row_no_known_order_explains_is_left_and_reported(tmp_path):
    # Shifted (a solar answer under Manufacturer), but not in the known way:
    # under the known order the Shape cell would hold an antenna.
    odd = list(RIGHT_ROW)
    odd[HEADER_ATTRIBUTES.index("Manufacturer")] = "Double Wing"
    odd[HEADER_ATTRIBUTES.index("Shape")] = "Patch"
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["c.jpg", *odd]])

    result = fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA)

    assert (result["fixed"], result["left"]) == (0, 1)
    assert result["left_rows"][0]["name"] == "c.jpg"
    assert "no known fix" in result["left_rows"][0]["detail"]
    assert _rows(Path(result["out_path"]))[1] == ["c.jpg", *odd]


def test_moving_an_answer_under_free_text_is_not_a_fix(tmp_path):
    """ "Patch" under Mass looks shifted. Swapping Mass and Manufacturer would
    put it under a text box, which offers nothing: still another attribute's
    answer, so still out of place."""
    orders = {"mass-and-manufacturer-swapped": {"order": ["Shape", "Antennas", "Manufacturer", "Mass"]}}
    original = _write(tmp_path / "old.csv", SMALL_HEADER, [["d.jpg", "Rectangular", "['Dipole']", "Patch", "Large"]])
    result = fix_csv.fix_legacy_csv(original, SMALL, known_orders=orders)
    assert (result["fixed"], result["left"]) == (0, 1)


def test_two_known_orders_that_both_fit_fix_nothing(tmp_path):
    orders = {
        "one": {"order": ["Antennas", "Shape", "Mass", "Manufacturer"]},
        "two": {"order": ["Antennas", "Shape", "Manufacturer", "Mass"]},
    }
    # Antenna under Shape and shape under Antennas; Mass and Manufacturer both "Unknown".
    row = ["e.jpg", "['Patch']", "Cylindrical", "Unknown", "Unknown"]
    original = _write(tmp_path / "old.csv", SMALL_HEADER, [row])
    result = fix_csv.fix_legacy_csv(original, SMALL, known_orders=orders)
    assert (result["fixed"], result["left"]) == (0, 1)
    assert "more than one known fix" in result["left_rows"][0]["detail"]
    assert _rows(Path(result["out_path"]))[1] == row


def test_a_known_order_for_other_columns_is_not_tried(tmp_path):
    original = _write(tmp_path / "old.csv", SMALL_HEADER, [["f.jpg", "['Patch']", "Cylindrical", "Small", "ESSA"]])
    result = fix_csv.fix_legacy_csv(original, SMALL)  # the shipped orders are for the 14 real attributes
    assert result["fixes_tried"] == []
    assert result["fixes_not_for_this_csv"] == list(fix_csv.KNOWN_ROW_ORDERS)
    assert (result["fixed"], result["left"]) == (0, 1)


def test_a_short_row_is_left(tmp_path):
    orders = {"swapped": {"order": ["Antennas", "Shape", "Mass", "Manufacturer"]}}
    original = tmp_path / "old.csv"
    original.write_text("name,Shape,Antennas,Mass,Manufacturer\ng.jpg,['Patch'],Cylindrical,Small\n", encoding="utf-8")
    result = fix_csv.fix_legacy_csv(original, SMALL, known_orders=orders)
    assert (result["fixed"], result["left"]) == (0, 1)
    assert "4 cells" in result["left_rows"][0]["detail"]


def test_the_copy_keeps_the_originals_encoding(tmp_path):
    orders = {"swapped": {"order": ["Antennas", "Shape", "Mass", "Manufacturer"]}}
    row = ["h.jpg", "['Patch']", "Cylindrical", "Small", "Aérospatiale"]
    original = _write(tmp_path / "old.csv", SMALL_HEADER, [row], encoding="cp1252")
    result = fix_csv.fix_legacy_csv(original, SMALL, known_orders=orders)
    assert result["fixed"] == 1
    assert _rows(Path(result["out_path"]), encoding="cp1252")[1] == [
        "h.jpg",
        "Cylindrical",
        "['Patch']",
        "Small",
        "Aérospatiale",
    ]


def test_it_refuses_to_write_over_the_original(tmp_path):
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["b.jpg", *SHIFTED_ROW]])
    with pytest.raises(ValueError, match="over the original"):
        fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA, out_path=original)
    with pytest.raises(ValueError, match="No CSV"):
        fix_csv.fix_legacy_csv(tmp_path / "nope.csv", SHIPPED_SCHEMA)
    no_name = _write(tmp_path / "other.csv", ["file", "Shape"], [["x.jpg", "Rectangular"]])
    with pytest.raises(ValueError, match="no 'name' column"):
        fix_csv.fix_legacy_csv(no_name, SHIPPED_SCHEMA)


def test_the_importer_takes_the_fixed_row_it_used_to_skip(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    apply_migrations(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO images (family, path, status, reviewed_at, created_at) VALUES (?, ?, 'active', ?, ?)",
        ("ESSA", "satellite_images/ESSA/b.jpg", now, now),
    )
    conn.commit()
    original = _write(tmp_path / "old.csv", OLD_HEADER, [["b.jpg", *SHIFTED_ROW]])

    skipped = annotate.import_legacy_csv(conn, original, SHIPPED_SCHEMA, dry_run=True, review_path=None)
    assert (skipped["imported"], skipped["misaligned"]) == (0, 1)

    fixed = fix_csv.fix_legacy_csv(original, SHIPPED_SCHEMA)
    taken = annotate.import_legacy_csv(conn, Path(fixed["out_path"]), SHIPPED_SCHEMA, review_path=None)
    assert (taken["imported"], taken["misaligned"]) == (1, 0)
    saved = annotate.get_annotation(conn, 1, annotate.LEGACY_ANNOTATOR)
    assert saved["Solar Panel Configuration"] == ["Double Wing"]
    assert saved["Manufacturer"] == "ESSA"
    assert saved["Docking Systems"] == "Docking Rings"
    assert saved["Windows or Optical Apertures"] == ["Circular"]
    assert saved["Insulation Type"] == ["Wrap"]
    assert saved["Deployment Mechanisms"] == ["Boom Arm"]
    conn.close()
