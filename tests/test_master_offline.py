"""Offline tests for the master set: the ledger in data/ledger.db, Keep moving
images home to data/images/<family>/, rebuilding after a wipe, sorting the
home into family folders, the sorting check and its fixes, adding images
from an outside source, and the ledger export. No network."""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scraper import ledger, master, wipe
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import add_images, import_data, overview, review
from scraper.stages.filter import run_filter
from scraper.web import app as web_app

SCHEMA = {"Body": {"Shape": ["Dropdown", ["Cubical", "Cylindrical"]]}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project folder (cwd) with a database; its ledger is tmp/data/ledger.db."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "satellites.csv").write_text(
        "NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n1,a,SAT,SAT\n2,b,OTHER,OTHER\n3,c,GPS,GPS\n"
    )
    conn = get_connection(tmp_path / "scraper.db")
    apply_migrations(conn)
    yield tmp_path, conn
    conn.close()


def _picture(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color=color).save(path)
    return path


def _now():
    return datetime.now(timezone.utc).isoformat()


def _scraped(conn, path: Path, family: str, source="nasa", license_name="Public domain") -> int:
    image_id = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)", (family, str(path), _now())
    ).lastrowid
    conn.execute(
        "INSERT INTO sources (image_id, source_module, external_id, source_url, retrieved_at) VALUES (?, ?, ?, ?, ?)",
        (image_id, source, f"{source}-{image_id}", f"https://{source}.example/{image_id}", _now()),
    )
    conn.execute(
        "INSERT INTO licenses (image_id, license_short_name, verified_at) VALUES (?, ?, ?)",
        (image_id, license_name, _now()),
    )
    conn.commit()
    return image_id


def _row(conn, image_id):
    return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


# ---- The ledger and Keep ----


def test_the_ledger_lives_in_data_next_to_the_database(project):
    root, conn = project
    assert ledger.data_dir(conn).resolve() == (root / "data").resolve()
    assert (root / "data" / "ledger.db").is_file()
    memory = get_connection(Path(":memory:"))
    assert ledger.counts(memory)["kept"] == 0  # an in-memory database gets an in-memory ledger
    memory.close()


def test_keep_moves_home_and_reject_is_remembered_with_its_reason(project):
    root, conn = project
    kept = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "SAT_nasa_01.jpg", "red"), "SAT")
    gone = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "SAT_nasa_02.jpg", "blue"), "SAT")

    review.decide(conn, kept, "keep")
    review.decide(conn, gone, "reject", root / "_quarantine", "several_satellites")

    assert _row(conn, kept)["path"] == "data/images/SAT/SAT_nasa_01.jpg"
    assert (root / "data" / "images" / "SAT" / "SAT_nasa_01.jpg").is_file()
    entry = ledger.entry(conn, _row(conn, kept)["content_sha256"])
    assert (entry["state"], entry["path"], entry["decided_by"]) == ("kept", "images/SAT/SAT_nasa_01.jpg", "review")
    (src,) = ledger.sources_for(conn, entry["sha256"])
    assert (src["source"], src["license_short_name"]) == ("nasa", "Public domain")
    rejected = ledger.entry(conn, _row(conn, gone)["content_sha256"])
    assert (rejected["state"], rejected["reason"]) == ("rejected", "several_satellites")
    assert (root / "_quarantine" / "SAT" / "SAT_nasa_02.jpg").is_file()


def test_rejecting_an_image_in_data_keeps_the_file_in_data(project):
    root, conn = project
    _picture(root / "data" / "images" / "SAT" / "SAT_01.jpg", "red")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    (image_id,) = [r["id"] for r in conn.execute("SELECT id FROM images")]
    review.decide(conn, image_id, "reject", root / "_quarantine", "not_recognizable")
    assert (root / "data" / "_rejected" / "SAT" / "SAT_01.jpg").is_file()  # a wipe empties _quarantine/, not data/
    assert not (root / "_quarantine").exists()


def test_a_set_aside_image_is_recorded_with_a_path_that_works_on_any_machine(project):
    root, conn = project
    _picture(root / "data" / "images" / "SAT" / "SAT_01.jpg", "red")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    (image_id,) = [r["id"] for r in conn.execute("SELECT id FROM images")]
    review.decide(conn, image_id, "reject", root / "_quarantine", "not_recognizable")
    assert _row(conn, image_id)["path"] == "data/_rejected/SAT/SAT_01.jpg"  # relative to the project, forward slashes


def test_putting_a_set_aside_image_back(project):
    """Put back: the file returns to data/images/<family>/, the image is
    active again, and the ledger says kept -- so the same picture arriving
    later is "already in the set", and a later sync doesn't reject it again."""
    tmp, conn = project
    off_limits = _scraped(conn, _picture(tmp / "satellite_images" / "SAT" / "a.png", "red"), "SAT", source="banned")
    review.quarantine_image(conn, off_limits, "rejected_filter", tmp / "_quarantine", "off_limits_source")
    import_data.import_data_folder(conn, SCHEMA, _picture(tmp / "data" / "images" / "SAT_01.png", "red").parents[1])
    ours = conn.execute("SELECT * FROM images WHERE origin = 'imported'").fetchone()
    assert ours["reject_reason"] == "rejected_before" and "_rejected" in ours["path"]

    with pytest.raises(ValueError):  # set aside from a scrape, not from the master set
        master.put_back(conn, off_limits)
    path = master.put_back(conn, ours["id"])
    assert path.is_file() and path.resolve().parent == (ledger.home(conn) / "SAT").resolve()
    row = _row(conn, ours["id"])
    assert (row["status"], row["reject_reason"]) == ("active", None) and row["reviewed_at"]
    entry = ledger.entry(conn, row["content_sha256"])
    assert (entry["state"], entry["reason"], entry["path"]) == ("kept", None, "images/SAT/SAT_01.png")
    with pytest.raises(ValueError):  # already back
        master.put_back(conn, ours["id"])

    master.sync_ledger(conn)  # the rejected scrape copy is still in the database
    assert ledger.entry(conn, row["content_sha256"])["state"] == "kept"
    again = _scraped(conn, _picture(tmp / "satellite_images" / "SAT" / "b.png", "red"), "SAT")
    assert master.check_arrival(conn, again, tmp / "_quarantine") == "already_kept"
    # "Unverified ..." isn't a license on record.
    conn.execute("UPDATE ledger.ledger_sources SET license_short_name = 'Unverified (no license metadata provided)'")
    assert ledger.counts(conn)["license_unrecorded"] == 1
    assert master.check_home(conn)["license_unrecorded"]["count"] == 1


def test_a_look_alike_marked_duplicate_is_remembered(project):
    root, conn = project
    image_id = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "a.jpg", "green"), "SAT")
    review.decide(conn, image_id, "duplicate", root / "_quarantine")
    entry = ledger.entry(conn, _row(conn, image_id)["content_sha256"])
    assert (entry["state"], entry["reason"]) == ("rejected", "duplicate")


def test_the_filter_leaves_reviewed_images_alone(project):
    root, conn = project
    reviewed = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "a.jpg", "red"), "SAT")
    conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), reviewed))
    new = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "b.jpg", "blue"), "SAT")
    conn.commit()
    counts = run_filter(conn, root / "_quarantine", scorer=lambda paths: [0.0 for _ in paths])
    assert counts["rejected"] == 1
    assert _row(conn, reviewed)["status"] == "active" and _row(conn, new)["status"] == "rejected_filter"
    assert ledger.entry(conn, _row(conn, new)["content_sha256"])["reason"] == "filter"


def test_the_filter_never_overwrites_a_file_already_set_aside(project):
    root, conn = project
    earlier = _picture(root / "_quarantine" / "SAT" / "a.jpg", "green")
    before = earlier.read_bytes()
    new = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "a.jpg", "blue"), "SAT")
    run_filter(conn, root / "_quarantine", scorer=lambda paths: [0.0 for _ in paths])
    assert earlier.read_bytes() == before
    assert Path(_row(conn, new)["path"]).name == "a_2.jpg" and Path(_row(conn, new)["path"]).is_file()


def test_the_sorting_check_follows_a_linked_data_folder(project):
    """The check resolves each folder once (not each file); a master set
    reached through a link still counts as home."""
    root, conn = project
    real = root / "elsewhere"
    _picture(real / "images" / "SAT" / "SAT_01.jpg", "red")
    try:
        (root / "data" / "images").symlink_to(real / "images", target_is_directory=True)
    except OSError:
        pytest.skip("this machine doesn't allow links")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    check = master.check_home(conn)
    assert not any(check[kind]["count"] for kind in ("wrong_folder", "outside_home", "untracked", "missing_file"))
    assert master.organize_home(conn, dry_run=True)["to_move"] == 0


# ---- Surviving a wipe ----


def test_after_a_wipe_import_rebuilds_families_licenses_and_rejections(project):
    root, conn = project
    # A scraped image kept under GPS although its file name says SAT, with its license.
    kept = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "SAT_nasa_01.jpg", "red"), "SAT")
    conn.execute("UPDATE images SET family = 'GPS' WHERE id = ?", (kept,))
    conn.commit()
    review.decide(conn, kept, "keep")
    # A file in data/ that was later rejected, then turned up in data/images/ again.
    old = _picture(root / "data" / "images" / "SAT" / "SAT_07.jpg", "blue")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    old_id = conn.execute("SELECT id FROM images WHERE path LIKE '%SAT_07.jpg'").fetchone()["id"]
    review.decide(conn, old_id, "reject", root / "_quarantine", "several_satellites")
    _picture(old, "blue")

    wipe.wipe_images_and_data(conn, root / "satellite_images", root / "_quarantine")
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    result = import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")

    assert result["rejected_before"] == 1
    back = conn.execute("SELECT * FROM images WHERE path = 'data/images/GPS/SAT_nasa_01.jpg'").fetchone()
    assert back["family"] == "GPS" and back["status"] == "active"
    lic = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (back["id"],)).fetchone()
    src = conn.execute("SELECT source_module FROM sources WHERE image_id = ?", (back["id"],)).fetchone()
    assert (lic[0], src[0]) == ("Public domain", "nasa")
    again = conn.execute("SELECT status, reject_reason FROM images WHERE path LIKE '%SAT_07%'").fetchone()
    assert (again["status"], again["reject_reason"]) == ("rejected_filter", "rejected_before")


# ---- Sorting the home ----


def test_organize_sorts_a_flat_folder_into_family_folders(project):
    root, conn = project
    _picture(root / "data" / "images" / "SAT_01.jpg", "red")
    _picture(root / "data" / "images" / "GPS_wikimedia_02.jpg", "blue")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    _picture(root / "data" / "images" / "OTHER_03.jpg", "green")  # not in the database yet

    dry = master.organize_home(conn)
    assert (dry["to_move"], dry["files"], dry["dry_run"]) == (3, 3, True)
    assert (root / "data" / "images" / "SAT_01.jpg").is_file()

    master.organize_home(conn, dry_run=False)
    assert {p.relative_to(root / "data" / "images").as_posix() for p in (root / "data" / "images").rglob("*.jpg")} == {
        "SAT/SAT_01.jpg",
        "GPS/GPS_wikimedia_02.jpg",
        "OTHER/OTHER_03.jpg",
    }
    paths = {r["path"] for r in conn.execute("SELECT path FROM images")}
    assert paths == {"data/images/SAT/SAT_01.jpg", "data/images/GPS/GPS_wikimedia_02.jpg"}
    sha = conn.execute("SELECT content_sha256 FROM images WHERE family = 'SAT'").fetchone()[0]
    assert ledger.entry(conn, sha)["path"] == "images/SAT/SAT_01.jpg"
    assert master.organize_home(conn)["to_move"] == 0


def test_the_sorting_check_finds_each_problem_and_fixes_it(project):
    root, conn = project
    # In data/images/ but in the wrong family folder.
    _picture(root / "data" / "images" / "GPS" / "SAT_01.jpg", "red")
    import_data.import_data_folder(conn, SCHEMA, Path("data"), root / "_quarantine")
    conn.execute("UPDATE images SET family = 'SAT'")
    conn.commit()
    # Kept before the master set existed: still in the inbox.
    outside = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "x.jpg", "blue"), "SAT")
    conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), outside))
    # The same picture twice in one family, and once more under another family.
    for name, fam in (("SAT/d1.jpg", "SAT"), ("SAT/d2.jpg", "SAT")):
        _scraped(conn, _picture(root / "satellite_images" / name, "yellow"), fam)
    two_a = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "t.jpg", "purple"), "SAT")
    two_b = _scraped(conn, _picture(root / "satellite_images" / "OTHER" / "t.jpg", "purple"), "OTHER")
    # A file nobody recorded, and a record whose file is gone.
    _picture(root / "data" / "images" / "SAT" / "stray.jpg", "white")
    gone = _scraped(conn, root / "satellite_images" / "SAT" / "gone.jpg", "SAT")
    conn.commit()
    for image_id in [r["id"] for r in conn.execute("SELECT id FROM images")]:
        master.image_hashes(conn, image_id)

    check = master.check_home(conn)
    counts = {kind: issue["count"] for kind, issue in check.items() if issue["count"]}
    assert counts["wrong_folder"] == 1 and counts["outside_home"] == 1 and counts["stored_twice"] == 1
    assert counts["two_families"] == 1 and counts["untracked"] == 1 and counts["missing_file"] == 1
    assert counts["not_in_ledger"] >= 4
    assert check["missing_file"]["items"][0]["id"] == gone and not check["missing_file"]["fixable"]

    for kind in ("wrong_folder", "outside_home", "stored_twice", "untracked", "not_in_ledger"):
        master.fix(conn, kind, root / "_quarantine")
    review.choose_family(conn, two_a, "OTHER", root / "_quarantine", root / "satellites.csv")

    after = {kind: issue["count"] for kind, issue in master.check_home(conn).items() if issue["count"]}
    assert set(after) <= {"missing_file", "license_unrecorded"}
    assert (root / "data" / "images" / "SAT" / "SAT_01.jpg").is_file()
    assert (root / "data" / "images" / "SAT" / "x.jpg").is_file()
    assert {_row(conn, two_a)["status"], _row(conn, two_b)["status"]} == {"active", "rejected_duplicate"}
    with pytest.raises(ValueError):
        master.fix(conn, "missing_file")


def test_sync_seeds_a_fresh_ledger_from_the_database(project):
    root, conn = project
    kept = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "k.jpg", "red"), "SAT")
    conn.execute("UPDATE images SET reviewed_at = ? WHERE id = ?", (_now(), kept))
    _scraped(conn, _picture(root / "satellite_images" / "SAT" / "w.jpg", "blue"), "SAT")
    off = _scraped(conn, _picture(root / "_quarantine" / "SAT" / "g.jpg", "green"), "SAT", source="gunters_space")
    conn.execute(
        "UPDATE images SET status = 'rejected_filter', reject_reason = 'off_limits_source' WHERE id = ?", (off,)
    )
    conn.commit()
    assert master.sync_ledger(conn, by="seed") == {"kept": 1, "waiting": 1, "rejected": 1, "copies": 0, "no_file": 0}
    c = ledger.counts(conn)
    assert (c["kept"], c["waiting"], c["rejected"], c["rejected_reasons"]) == (1, 1, 1, {"off_limits_source": 1})


# ---- Adding images from outside ----


def test_add_images_with_subfolders_a_manifest_and_the_ledger_checks(project):
    root, conn = project
    batch = root / "esa_batch"
    _picture(batch / "SAT" / "one.png", "red")
    _picture(batch / "GPS" / "two.png", "blue")
    _picture(batch / "loose.png", "green")  # no family
    _picture(batch / "NEWSAT" / "three.png", (10, 20, 30))
    (batch / "notes.txt").write_text("not an image")
    manifest = root / "manifest.csv"
    with open(manifest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "family", "license", "source_url"])
        w.writerow(["GPS/two.png", "", "CC BY-SA 3.0 IGO", "https://esa.example/two"])

    dry = add_images.add_folder(conn, batch, "ESA archive", "", manifest=manifest, dry_run=True)
    assert (dry["added"], dry["no_license"], dry["no_family"]) == (1, 2, 1)
    assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0

    result = add_images.add_folder(conn, batch, "ESA archive", "CC BY 4.0", manifest=manifest)
    assert (result["found"], result["added"], result["no_family"]) == (4, 3, 1)
    assert result["unknown_families"] == ["NEWSAT"]
    assert (batch / "SAT" / "one.png").is_file()  # copied, never moved
    two = conn.execute("SELECT i.id, i.path FROM images i WHERE i.family = 'GPS'").fetchone()
    lic = conn.execute("SELECT license_short_name FROM licenses WHERE image_id = ?", (two["id"],)).fetchone()[0]
    src = conn.execute("SELECT source_module, source_url FROM sources WHERE image_id = ?", (two["id"],)).fetchone()
    assert lic == "CC BY-SA 3.0 IGO" and tuple(src) == ("outside:esa_archive", "https://esa.example/two")
    assert Path(two["path"]).parent.name == "GPS" and "satellite_images" in two["path"]

    again = add_images.add_folder(conn, batch, "ESA archive", "CC BY 4.0", manifest=manifest)
    assert again["already_added"] == 3 and again["added"] == 0
    assert "Added 0 of 4" in add_images.format_result(again)


def test_added_images_meet_the_ledger_and_can_skip_review(project):
    root, conn = project
    rejected = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "r.jpg", "red"), "SAT")
    review.decide(conn, rejected, "reject", root / "_quarantine", "not_a_satellite")
    kept = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "k.jpg", "blue"), "SAT")
    review.decide(conn, kept, "keep")

    batch = root / "batch"
    _picture(batch / "a.jpg", "red")  # the rejected picture
    _picture(batch / "b.jpg", "blue")  # already in the master set
    _picture(batch / "c.jpg", "green")  # new
    result = add_images.add_folder(conn, batch, "Lab photos", "CC0", family="SAT", already_reviewed=True)

    assert (result["rejected_before"], result["already_in_master_set"], result["kept"]) == (1, 1, 1)
    assert (root / "data" / "images" / "SAT" / "c.jpg").is_file()
    sha_blue = _row(conn, kept)["content_sha256"]
    assert {s["source"] for s in ledger.sources_for(conn, sha_blue)} == {"nasa", "outside:lab_photos"}
    with pytest.raises(ValueError, match="name"):
        add_images.source_key("  ")


# ---- Export, web and Status ----


def test_ledger_export(project):
    root, conn = project
    image_id = _scraped(conn, _picture(root / "satellite_images" / "SAT" / "k.jpg", "red"), "SAT")
    review.decide(conn, image_id, "keep")
    out = root / "export" / "ledger.csv"
    assert ledger.export_csv(conn, out) == 1
    (row,) = list(csv.DictReader(open(out)))
    assert (row["state"], row["family"], row["sources"], row["licenses"]) == ("kept", "SAT", "nasa", "Public domain")


def test_master_endpoints_and_status_alert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "web.db"
    web_app._jobs.clear()
    _picture(tmp_path / "data" / "images" / "SAT_01.jpg", "red")
    with TestClient(web_app.app) as client:
        data = client.get("/api/master").json()
        assert set(data) == {"ledger", "ledger_file", "home", "check", "organize"}
        assert data["check"]["untracked"]["count"] == 1
        assert client.post("/api/jobs/add-images", json={"folder": "nope", "source_name": "x"}).status_code == 400
        assert client.post("/api/master/fix", json={"kind": "missing_file"}).status_code == 400
        assert client.get("/api/master/ledger.csv").status_code == 200
        assert 'id="card-master"' in client.get("/data").text
        alerts = client.get("/api/overview").json()["alerts"]
        assert any("master set needs sorting" in a["text"] and a["link"] == "/data" for a in alerts)
        # Adding a folder starts a job (its options reach it) and a duplicate scan waits its turn.
        _picture(tmp_path / "batch" / "SAT" / "one.png", "blue")
        started = client.post(
            "/api/jobs/add-images",
            json={"folder": "batch", "source_name": "ESA archive", "license": "CC BY 4.0", "author": "ESA"},
        )
        assert started.status_code == 200
        entry = web_app._jobs[started.json()["job_id"]]
        deadline = datetime.now(timezone.utc).timestamp() + 5
        while entry["status"] == "running" and datetime.now(timezone.utc).timestamp() < deadline:
            entry["cancel"].wait(0.02)
        assert entry["status"] == "done"
        web_app._new_job("Import", "review")
        assert client.post("/api/jobs/dedupe", json={}).status_code == 409
        web_app._jobs.clear()
    conn = get_connection(tmp_path / "web.db")
    assert conn.execute("SELECT COUNT(*) FROM images WHERE family = 'SAT'").fetchone()[0] == 1
    assert not [a for a in overview.alerts(conn, {"waiting_for_filter": 0, "waiting_for_review": 0}, [], [], None, {})]
    conn.close()
