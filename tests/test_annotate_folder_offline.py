"""Offline tests for the annotate stage's folder mode -- the drop-in for the
old PyQt tool's setup (image folder in, CSV row + moved image out)."""

import ast
import csv
import json
from pathlib import Path

import pytest
from PIL import Image

from scraper.stages import annotate
from scraper.stages import annotate_folder as folder

SCHEMA = {
    "Body": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
    },
    "Other": {"Mass": ["Dropdown", ["Small", "Large"]]},
}
VALUES = {"Shape": "Rectangular", "Antennas": ["Dipole"], "Mass": "Large"}


def _image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)


@pytest.fixture
def setup(tmp_path):
    source, done = tmp_path / "images", tmp_path / "images_annotated"
    for name in ("b.png", "a.jpg", "C.jpeg"):
        _image(source / name)
    (source / ".DS_Store").write_text("x")
    (source / "notes.txt").write_text("x")
    (source / "subfolder").mkdir()
    config = {
        "mode": "folder",
        "imageSource": str(source),
        "imageDestination": str(done),
        "CSVDest": str(tmp_path / "annotatedInfo.csv"),
        "attributesPath": "",
    }
    return config


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


# ---- Settings ----


def test_config_defaults_to_database_mode(tmp_path):
    assert folder.load_source_config(tmp_path / "missing.json")["mode"] == "database"


def test_config_round_trip(setup, tmp_path):
    path = tmp_path / "annotation_source.json"
    folder.save_source_config(setup, path)
    assert folder.load_source_config(path) == setup


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "spreadsheet"},
        {"imageSource": "/nope/not/here"},
        {"imageDestination": ""},
        {"CSVDest": "/nope/not/here/annotatedInfo.csv"},
        {"attributesPath": "/nope/attributes.json"},
    ],
)
def test_config_rejects_bad_settings(setup, change):
    with pytest.raises(folder.FolderError):
        folder.validate_source_config({**setup, **change})


def test_config_rejects_same_source_and_destination(setup):
    with pytest.raises(folder.FolderError):
        folder.validate_source_config({**setup, "imageDestination": setup["imageSource"]})


def test_database_mode_doesnt_need_folders():
    assert folder.validate_source_config({"mode": "database"})["mode"] == "database"


def test_read_old_tool_settings(tmp_path):
    old = tmp_path / "Satellite code"
    old.mkdir()
    (old / "metadata.json").write_text(
        json.dumps({"imageSource": "C:/x/images", "imageDestination": "C:/x/done", "CSVDest": "C:/x/info.csv"})
    )
    (old / "attributes.json").write_text(json.dumps(SCHEMA))

    settings = folder.read_old_tool_settings(old)
    assert settings == {
        "mode": "folder",
        "imageSource": "C:/x/images",
        "imageDestination": "C:/x/done",
        "CSVDest": "C:/x/info.csv",
        "attributesPath": str(old / "attributes.json"),
    }
    assert folder.read_old_tool_settings(old / "metadata.json") == settings  # the file itself works too

    with pytest.raises(folder.FolderError):
        folder.read_old_tool_settings(tmp_path)


# ---- Queue + images ----


def test_queue_lists_only_images_sorted(setup):
    result = folder.folder_queue(setup)
    assert [item["name"] for item in result["queue"]] == ["a.jpg", "b.png", "C.jpeg"]
    assert result["remaining"] == 3


@pytest.mark.parametrize("name", ["../annotatedInfo.csv", "notes.txt", ".DS_Store", "missing.jpg", "subfolder"])
def test_image_path_only_serves_images_in_the_folder(setup, name):
    with pytest.raises(folder.FolderError):
        folder.image_path(setup, name)


# ---- Submit ----


def test_submit_writes_old_csv_layout_and_moves_the_image(setup):
    folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)

    header, rows = _rows(Path(setup["CSVDest"]))
    assert header == ["name", "Shape", "Antennas", "Mass"]  # what the old tool wrote for a new file
    assert rows[0]["name"] == "a.jpg"
    assert ast.literal_eval(rows[0]["Antennas"]) == ["Dipole"]
    assert not (Path(setup["imageSource"]) / "a.jpg").exists()
    assert (Path(setup["imageDestination"]) / "a.jpg").exists()
    assert folder.folder_queue(setup)["remaining"] == 2  # picks up where it left off


def test_submit_unusable(setup):
    folder.submit_folder_annotation(setup, "a.jpg", {}, SCHEMA, unusable=True)
    _, rows = _rows(Path(setup["CSVDest"]))
    assert {rows[0][a] for a in ("Shape", "Antennas", "Mass")} == {"UNUSABLE"}


def test_submit_follows_an_existing_csvs_column_order_and_old_names(setup):
    # An old-tool CSV: different column order, old "Antennae" name, an extra retired column.
    csv_path = Path(setup["CSVDest"])
    csv_path.write_text("name,Mass,Antennae,Retired,Shape\r\nold.jpg,Small,['Patch'],x,Cylindrical\r\n")

    folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)

    header, rows = _rows(csv_path)
    assert header == ["name", "Mass", "Antennae", "Retired", "Shape"]
    assert rows[0]["name"] == "old.jpg"
    assert rows[1] == {
        "name": "a.jpg",
        "Mass": "Large",
        "Antennae": "['Dipole']",
        "Retired": "",
        "Shape": "Rectangular",
    }


def test_submit_adds_a_column_for_a_new_attribute_without_shifting_rows(setup):
    csv_path = Path(setup["CSVDest"])
    csv_path.write_text("name,Shape,Antennas\r\nold.jpg,Cylindrical,['Patch']\r\n")

    folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)

    header, rows = _rows(csv_path)
    assert header == ["name", "Shape", "Antennas", "Mass"]
    assert rows[0] == {"name": "old.jpg", "Shape": "Cylindrical", "Antennas": "['Patch']", "Mass": ""}
    assert rows[1]["Mass"] == "Large"


def test_submit_keeps_a_cp1252_csvs_encoding(setup):
    csv_path = Path(setup["CSVDest"])
    csv_path.write_bytes(
        "name,Shape,Antennas,Mass\r\nold.jpg,Rectangular,['Patch'],Small \u2013 ish\r\n".encode("cp1252")
    )

    folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)

    text = csv_path.read_bytes().decode("cp1252")
    assert "Small \u2013 ish" in text
    assert text.endswith("a.jpg,Rectangular,['Dipole'],Large\r\n")


def test_submit_appends_after_a_file_missing_its_final_newline(setup):
    csv_path = Path(setup["CSVDest"])
    csv_path.write_text("name,Shape,Antennas,Mass\r\nold.jpg,Rectangular,['Patch'],Small")
    folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)
    _, rows = _rows(csv_path)
    assert [r["name"] for r in rows] == ["old.jpg", "a.jpg"]


def test_invalid_submit_changes_nothing(setup):
    with pytest.raises(annotate.AnnotationError):
        folder.submit_folder_annotation(setup, "a.jpg", {"Shape": "Rectangular"}, SCHEMA)
    assert (Path(setup["imageSource"]) / "a.jpg").exists()
    assert not Path(setup["CSVDest"]).exists()


def test_submit_refuses_to_overwrite_an_already_annotated_image(setup):
    _image(Path(setup["imageDestination"]) / "a.jpg")
    with pytest.raises(folder.FolderError):
        folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)
    assert (Path(setup["imageSource"]) / "a.jpg").exists()
    assert not Path(setup["CSVDest"]).exists()


def test_failed_csv_write_moves_the_image_back(setup):
    Path(setup["CSVDest"]).write_text("Shape,Mass\r\nRectangular,Small\r\n")  # no 'name' column
    with pytest.raises(folder.FolderError):
        folder.submit_folder_annotation(setup, "a.jpg", VALUES, SCHEMA)
    assert (Path(setup["imageSource"]) / "a.jpg").exists()
    assert not (Path(setup["imageDestination"]) / "a.jpg").exists()


def test_submit_writes_unknown_in_each_fields_own_format(setup):
    folder.submit_folder_annotation(setup, "a.jpg", {**VALUES, "Antennas": ["Unknown"], "Mass": "unknown"}, SCHEMA)
    _, rows = _rows(Path(setup["CSVDest"]))
    assert rows[0]["Antennas"] == "['Unknown']"  # a multi-select is still a list
    assert rows[0]["Mass"] == "Unknown"
