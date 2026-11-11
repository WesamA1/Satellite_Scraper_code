"""Offline tests for importing a data/ folder: imported images join the
database, their answers come in, and an imported copy of a file always wins
over a scraped one (keeping its license record). No network."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import import_data, review

SCHEMA = {"Body": {"Shape": ["DropdownOther", ["Rectangular", "Cylindrical"]]}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder (cwd) with a database and an empty data/ folder."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "images").mkdir(parents=True)
    conn = get_connection(tmp_path / "scraper.db")
    apply_migrations(conn)
    yield tmp_path, conn
    conn.close()


def _picture(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (6, 6), color=color).save(path)
    return path


def _old_csv(root: Path, rows: dict[str, str]):
    (root / "data" / "annotations.csv").write_text(
        "name,Shape\r\n" + "".join(f"{name},{shape}\r\n" for name, shape in rows.items())
    )


def _scraped(conn, path: Path, family: str, licensed: bool = True) -> int:
    now = datetime.now(timezone.utc).isoformat()
    image_id = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, path.as_posix(), now),
    ).lastrowid
    if licensed:
        conn.execute(
            "INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at)"
            " VALUES (?, 'wikimedia', ?, 'https://commons.example/x', ?)",
            (image_id, f"File:{path.name}", now),
        )
        conn.execute(
            "INSERT INTO licenses (image_id, license_short_name, verified_at) VALUES (?, 'CC BY-SA 4.0', ?)",
            (image_id, now),
        )
    conn.commit()
    return image_id


def _row(conn, image_id):
    return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


def _id(conn, path: str) -> int:
    return conn.execute("SELECT id FROM images WHERE path = ?", (path,)).fetchone()["id"]


def test_imports_images_and_their_answers(project):
    root, conn = project
    _picture(root / "data" / "images" / "AEHF_wikimedia_08.jpg", "blue")
    _picture(root / "data" / "images" / "sub" / "ACE_01.jpg", "red")
    _old_csv(root, {"AEHF_wikimedia_08.jpg": "Cylindrical", "ACE_01.jpg": "Rectangular"})
    # A scraped file with the same name but a different picture: not who the CSV row is about.
    scraped = _scraped(conn, _picture(root / "satellite_images" / "AEHF" / "AEHF_wikimedia_08.jpg", "green"), "AEHF")

    result = import_data.import_data_folder(conn, SCHEMA, Path("data"))

    assert result["images_added"] == 2 and result["annotations"]["legacy_miat"]["imported"] == 2
    imported = _row(conn, _id(conn, "data/images/AEHF_wikimedia_08.jpg"))
    assert (imported["origin"], imported["family"], imported["status"]) == ("imported", "AEHF", "active")
    assert imported["reviewed_at"] is not None  # already looked at by people
    assert _id(conn, "data/images/sub/ACE_01.jpg")
    value = conn.execute("SELECT value FROM annotations WHERE image_id = ?", (imported["id"],)).fetchone()["value"]
    assert json.loads(value) == "Cylindrical"
    assert conn.execute("SELECT COUNT(*) AS n FROM annotations WHERE image_id = ?", (scraped,)).fetchone()["n"] == 0

    again = import_data.import_data_folder(conn, SCHEMA, Path("data"))
    assert again["images_added"] == 0 and again["annotations"]["legacy_miat"]["already_annotated"] == 2


def test_imported_copy_wins_and_takes_the_license(project):
    root, conn = project
    _picture(root / "data" / "images" / "AEHF_01.jpg", "blue")
    scraped = _scraped(conn, _picture(root / "satellite_images" / "AEHF" / "AEHF_wikimedia_03.jpg", "blue"), "AEHF")
    _old_csv(root, {"AEHF_01.jpg": "Cylindrical"})

    result = import_data.import_data_folder(conn, SCHEMA, Path("data"), quarantine_dir=root / "_quarantine")

    kept = _id(conn, "data/images/AEHF_01.jpg")
    assert result["identical_copies_removed"] == 1
    assert _row(conn, kept)["status"] == "active"
    assert _row(conn, scraped)["status"] == "rejected_duplicate"
    assert Path(_row(conn, scraped)["path"]).is_file() and "_quarantine" in _row(conn, scraped)["path"]
    license_row = conn.execute("SELECT image_id, license_short_name FROM licenses").fetchone()
    assert (license_row["image_id"], license_row["license_short_name"]) == (kept, "CC BY-SA 4.0")
    assert conn.execute("SELECT image_id FROM sources").fetchone()["image_id"] == kept


def test_a_set_aside_copy_from_data_stays_in_data(project):
    root, conn = project
    for name in ("SAT_01.jpg", "SAT_02.jpg"):  # one picture saved twice in the data folder
        _picture(root / "data" / "images" / name, "blue")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), quarantine_dir=root / "_quarantine")

    statuses = sorted(r["status"] for r in conn.execute("SELECT status FROM images"))
    assert statuses == ["active", "rejected_duplicate"]
    # The extra copy leaves the master set but stays in data/ (a wipe empties _quarantine/).
    assert (root / "data" / "images" / "SAT_01.jpg").is_file()
    assert (root / "data" / "_rejected" / "SAT" / "SAT_02.jpg").is_file()
    assert not (root / "_quarantine").exists()


def test_review_starts_on_the_imported_family_and_keeps_the_imported_copy(project):
    root, conn = project
    _picture(root / "data" / "images" / "DX_01.jpg", "blue")
    scraped = _scraped(
        conn, _picture(root / "satellite_images" / "AII-BRAVO" / "AII-BRAVO_nasa_01.jpg", "blue"), "AII-BRAVO"
    )
    result = import_data.import_data_folder(conn, SCHEMA, Path("data"), quarantine_dir=root / "_quarantine")
    assert result["families_to_choose"] == 1

    (item,) = review.review_queue(conn)  # the scraped copy is the unreviewed one
    assert item["id"] == scraped
    assert item["family_choice"]["preselected"] == "DX"

    outcome = review.choose_family(conn, scraped, "AII-BRAVO", root / "_quarantine", root / "satellites.csv")
    imported = _id(conn, "data/images/DX_01.jpg")
    assert outcome["kept"] == imported  # the imported copy wins even though the scraped one was on screen
    assert _row(conn, imported)["family"] == "AII-BRAVO" and _row(conn, imported)["path"] == "data/images/DX_01.jpg"
    assert conn.execute("SELECT image_id FROM licenses").fetchone()["image_id"] == imported


def test_imported_only_copies_under_two_names_arent_asked_about(project):
    root, conn = project
    _picture(root / "data" / "images" / "ELEKTRO_01.jpg", "blue")
    _picture(root / "data" / "images" / "ELEKTRO-L_01.jpg", "blue")
    result = import_data.import_data_folder(conn, SCHEMA, Path("data"))
    assert result["families_to_choose"] == 0 and result["identical_copies_removed"] == 0


def test_dry_run_writes_nothing(project):
    root, conn = project
    _picture(root / "data" / "images" / "SAT_01.jpg", "blue")
    result = import_data.import_data_folder(conn, SCHEMA, Path("data"), dry_run=True)
    assert result["images_added"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"] == 0


def test_needs_an_images_folder(project, tmp_path):
    with pytest.raises(ValueError, match="images"):
        import_data.import_data_folder(project[1], SCHEMA, tmp_path / "elsewhere")
