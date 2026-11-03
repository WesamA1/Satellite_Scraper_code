"""Offline tests for the first-pass model's training set: which answers become
labels, the family-grouped split, and building from the old CSV + the DB."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import dataset
from scraper.stages import annotate

SCHEMA = {
    "Highly-Distinct": {
        "Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]],
        "Antennas": ["Multi-Select", ["Patch", "Dipole"]],
        "Manufacturer": ["Text Box"],
    },
    "Non-Distinct": {
        "Mass": ["Dropdown", ["No Frame of Reference", "Small", "Large"]],
        "Has Logo": ["Check Button"],
    },
}
SHAPE, ANTENNAS, MASS, LOGO = (annotate.flat_attributes(SCHEMA)[n] for n in ("Shape", "Antennas", "Mass", "Has Logo"))

HEADER = "name,Shape,Antennae,Manufacturer,Mass\r\n"


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)
    return path


def _insert_image(conn, path: Path, family: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO images (family, path, status, reviewed_at, created_at) VALUES (?, ?, 'active', ?, ?)",
        (family, str(path), now, now),
    )
    conn.commit()
    return cur.lastrowid


# ---- Labels ----


@pytest.mark.parametrize(
    "spec,value,expected",
    [
        (SHAPE, "Cylindrical", ("Cylindrical", None)),
        (SHAPE, "Metallic and Blue", (None, "not_an_option")),  # free text typed into Other
        (SHAPE, ["Cylindrical"], (None, "not_an_option")),  # a list under a single-choice field
        (SHAPE, "Unknown", (None, "unknown")),
        (SHAPE, "Unknown (Black and White Image)", (None, "unknown")),
        (MASS, "No Frame of Reference", (None, "unknown")),
        (MASS, "UNUSABLE", (None, "blank")),
        (MASS, "", (None, "blank")),
        (MASS, None, (None, "blank")),
        (ANTENNAS, ["Dipole", "Patch"], (["Patch", "Dipole"], None)),  # option order
        (ANTENNAS, ["None"], ([], None)),  # looked, none there -- a real label
        (ANTENNAS, ["Unknown"], (None, "unknown")),
        (ANTENNAS, ["Patch", "Horn"], (None, "not_an_option")),
        (LOGO, "Present", ("Present", None)),
        (LOGO, "Unknown", (None, "unknown")),
    ],
)
def test_training_label(spec, value, expected):
    assert dataset.training_label(spec, value) == expected


def test_free_text_attributes_are_left_out():
    assert "Manufacturer" not in dataset.trainable_attributes(SCHEMA)
    assert set(dataset.trainable_attributes(SCHEMA)) == {"Shape", "Antennas", "Mass", "Has Logo"}


@pytest.mark.parametrize(
    "name,family",
    [
        ("3CAT_01.jpg", "3CAT"),
        ("A-1_wikimedia_11.jpg", "A-1"),
        ("GPS_IIF_02.png", "GPS_IIF"),
        ("starlink_demo_02.png", "STARLINK"),
        ("odd-name.jpg", "ODD-NAME"),
    ],
)
def test_family_from_name(name, family):
    assert dataset.family_from_name(name) == family


def test_split_is_stable_and_per_family():
    assert dataset.split_for("STARLINK") == dataset.split_for(" starlink ")
    splits = {dataset.split_for(f"FAMILY{i}") for i in range(200)}
    assert splits == {"train", "test"}
    share = sum(dataset.split_for(f"FAMILY{i}") == "test" for i in range(2000)) / 2000
    assert 0.15 < share < 0.25


# ---- Building ----


def _csv(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "annotatedInfo.csv"
    path.write_text(HEADER + "".join(r + "\r\n" for r in rows), encoding="utf-8")
    return path


def test_build_from_old_csv(tmp_path):
    images = tmp_path / "imagesAnnotated"
    for name in ("A_01.jpg", "A_02.jpg", "B_01.jpg", "SHIFT_01.jpg", "BAD_01.jpg"):
        _image(images / name)
    csv_path = _csv(
        tmp_path,
        [
            "A_01.jpg,Rectangular,['Patch'],Boeing,Small",
            "A_02.jpg,Unknown,['None'],Unknown,No Frame of Reference",
            "B_01.jpg,Metallic and Gold,\"['Dipole', 'Patch']\",x,Large",
            "SHIFT_01.jpg,Rectangular,['Patch'],x,Cylindrical",  # a Shape option under Mass
            "BAD_01.jpg,UNUSABLE,UNUSABLE,UNUSABLE,UNUSABLE",
            "GONE_01.jpg,Rectangular,['Patch'],x,Small",
        ],
    )

    out = tmp_path / "training"
    report = dataset.build_training_set(None, SCHEMA, csv_path, [images], out)

    examples = dataset.load_examples(out / "dataset.jsonl")
    by_name = {e["name"]: e for e in examples}
    assert set(by_name) == {"A_01.jpg", "A_02.jpg", "B_01.jpg"}
    assert by_name["A_01.jpg"]["labels"] == {"Shape": "Rectangular", "Antennas": ["Patch"], "Mass": "Small"}
    assert by_name["A_02.jpg"]["labels"] == {"Antennas": []}  # Unknown / No Frame of Reference ignored
    assert by_name["B_01.jpg"]["labels"] == {"Antennas": ["Patch", "Dipole"], "Mass": "Large"}
    assert by_name["A_01.jpg"]["family"] == "A" and by_name["A_01.jpg"]["source"] == dataset.SOURCE_CSV
    assert by_name["A_01.jpg"]["split"] == by_name["A_02.jpg"]["split"]  # same family, same side

    assert report["skipped_rows"] == {
        "values look column-shifted": 1,
        "marked unusable": 1,
        "no image file with this name": 1,
    }
    assert report["left_out_attributes"] == ["Manufacturer"]
    shape = report["attributes"]["Shape"]
    assert shape["ignored"] == {"unknown": 1, "not_an_option": 1}
    assert sum(shape["options"]["Rectangular"].values()) == 1
    assert sum(report["attributes"]["Antennas"]["options"]["Patch"].values()) == 2
    assert json.loads((out / "report.json").read_text())["examples"] == report["examples"]
    assert (out / "skipped.csv").read_text().count("\n") == 4  # header + 3 rows


def test_build_from_database_and_no_duplicates_with_imported_rows(conn, tmp_path):
    ours = _insert_image(conn, _image(tmp_path / "STARLINK" / "STARLINK_wikimedia_01.jpg"), "STARLINK")
    _insert_image(conn, _image(tmp_path / "IRIDIUM" / "IRIDIUM_01.jpg"), "IRIDIUM")  # matched by the import
    gone = _insert_image(conn, tmp_path / "GONE" / "GONE_01.jpg", "GONE")  # file never written
    values = {
        "Shape": "Cylindrical",
        "Antennas": ["Dipole"],
        "Manufacturer": "Unknown",
        "Mass": "Large",
        "Has Logo": "Present",
    }
    for image_id in (ours, gone):
        annotate.submit_annotation(conn, image_id, "alice", values, SCHEMA)
    csv_path = _csv(tmp_path, ["IRIDIUM_01.jpg,Rectangular,['Patch'],x,Small"])
    annotate.import_legacy_csv(conn, csv_path, SCHEMA, review_path=None)

    report = dataset.build_training_set(conn, SCHEMA, csv_path, [], out_dir=None)

    assert report["examples"]["train"] + report["examples"]["test"] == 2  # imported row isn't counted twice
    assert report["database_images_missing"] == 1
    assert report["sources"] == {dataset.SOURCE_DB: 2}
    assert sum(report["attributes"]["Has Logo"]["labeled"].values()) == 1  # only alice answered it


def test_csv_rows_find_images_through_the_database(conn, tmp_path):
    _insert_image(conn, _image(tmp_path / "lib" / "IRIDIUM_01.jpg"), "IRIDIUM NEXT")
    csv_path = _csv(tmp_path, ["IRIDIUM_01.jpg,Rectangular,['Patch'],x,Small"])
    out = tmp_path / "training"
    dataset.build_training_set(conn, SCHEMA, csv_path, [], out)
    (example,) = dataset.load_examples(out / "dataset.jsonl")
    assert example["family"] == "IRIDIUM NEXT"  # the DB's family beats the file-name guess
    assert Path(example["image"]).is_file()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"csvs": None},  # and no DB either
        {"test_share": 0},
        {"test_share": 1.5},
        {"schema": {"Cat": {"Notes": ["Text Box"]}}},
        {"image_dirs": [Path("/nope/not/here")]},
    ],
)
def test_build_rejects_bad_input(tmp_path, kwargs):
    args = {"schema": SCHEMA, "csvs": _csv(tmp_path, []), "image_dirs": [], "out_dir": None, **kwargs}
    with pytest.raises(dataset.DatasetError):
        dataset.build_training_set(None, **args)


# ---- Several annotators ----


def test_parse_csv_args(tmp_path):
    single = tmp_path / "annotatedInfo.csv"
    single.write_text(HEADER)
    assert dataset.parse_csv_args([str(single)]) == [(annotate.LEGACY_ANNOTATOR, single)]
    assert dataset.parse_csv_args([f"alice={single}", f"bob = {single}"]) == [("alice", single), ("bob", single)]
    assert dataset.parse_csv_args([str(tmp_path / "alice.csv"), str(tmp_path / "bob.csv")]) == [
        ("alice", tmp_path / "alice.csv"),
        ("bob", tmp_path / "bob.csv"),
    ]


@pytest.mark.parametrize(
    "values",
    [
        ["a/annotatedInfo.csv", "b/annotatedInfo.csv"],  # same file name, nobody named
        ["alice=a.csv", "alice=b.csv"],
        ["human_review=a.csv", "bob=b.csv"],  # reserved for the Review page
    ],
)
def test_parse_csv_args_rejects(values):
    with pytest.raises(dataset.DatasetError):
        dataset.parse_csv_args(values)


def test_each_annotators_csv_stays_separate(tmp_path):
    alice_dir, bob_dir = tmp_path / "alice" / "imagesAnnotated", tmp_path / "bob" / "imagesAnnotated"
    _image(alice_dir / "A_01.jpg")
    _image(bob_dir / "A_01.jpg")  # the same image, in both annotators' folders
    _image(alice_dir / "B_01.jpg")
    other = bob_dir / "B_01.jpg"  # same name, different picture
    other.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="red").save(other)
    alice = tmp_path / "alice" / "annotatedInfo.csv"
    alice.write_text(HEADER + "A_01.jpg,Rectangular,['Patch'],x,Small\r\nB_01.jpg,Cylindrical,['None'],x,Large\r\n")
    bob = tmp_path / "bob" / "annotatedInfo.csv"
    bob.write_text(HEADER + "A_01.jpg,Cylindrical,['Patch'],x,Small\r\n")

    out = tmp_path / "training"
    report = dataset.build_training_set(None, SCHEMA, [("alice", alice), ("bob", bob)], [alice_dir, bob_dir], out)

    examples = dataset.load_examples(out / "dataset.jsonl")
    shared = [e for e in examples if e["name"] == "A_01.jpg"]
    assert sorted((e["annotator"], e["labels"]["Shape"]) for e in shared) == [
        ("alice", "Rectangular"),
        ("bob", "Cylindrical"),  # disagreement kept for majority vote later
    ]
    assert shared[0]["split"] == shared[1]["split"]
    assert report["images_with_several_annotators"] == 1
    assert report["annotators"] == {"alice": 1, "bob": 1}
    assert report["skipped_rows_by_annotator"] == {"alice": {"several different image files share this name": 1}}
    assert "alice,several different image files share this name,B_01.jpg" in (out / "skipped.csv").read_text()
