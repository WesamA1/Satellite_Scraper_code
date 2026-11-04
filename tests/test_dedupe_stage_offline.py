"""Offline tests for the dedupe stage. Pure PIL image generation -- no
network, no real satellite imagery."""

import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import dedupe


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _make_pattern_image(path: Path, seed: int, tag: str = ""):
    """A deterministic, non-uniform grayscale pattern -- same seed always
    hashes the same, different seeds hash differently. A solid-color image
    would dhash to all-zero bits regardless of color, which is useless for
    telling "genuinely different photo" apart from "exact duplicate". A
    linear formula (e.g. adding a seed-dependent constant to every pixel)
    isn't good enough either -- it mostly preserves the relative ordering
    between neighboring pixels that dhash actually looks at, so different
    seeds can still collide within MAX_HAMMING by accident. A per-seed PRNG
    has no such relationship between seeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 32
    rng = random.Random(seed)
    pixels = [rng.randint(0, 255) for _ in range(size * size)]
    img = Image.new("L", (size, size))
    img.putdata(pixels)
    img.convert("RGB").save(path)
    if tag:
        # Same pixels, different bytes: a look-alike, not an identical file
        # (JPEG readers ignore anything after the end-of-image marker).
        path.write_bytes(path.read_bytes() + tag.encode())


def _insert_active_image(conn, path: Path, family="STARLINK"):
    cur = conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, str(path), _now()),
    )
    conn.commit()
    return cur.lastrowid


def test_compute_phash_is_deterministic(tmp_path):
    path = tmp_path / "a.jpg"
    _make_pattern_image(path, seed=1)

    assert dedupe.compute_phash(path) == dedupe.compute_phash(path)


def test_compute_phash_returns_none_for_unreadable_file(tmp_path):
    path = tmp_path / "missing.jpg"
    assert dedupe.compute_phash(path) is None


def test_hamming_distance_zero_for_identical_hashes():
    assert dedupe._hamming_distance("00ff", "00ff") == 0


def test_hamming_distance_counts_differing_bits():
    assert dedupe._hamming_distance("0000", "0001") == 1
    assert dedupe._hamming_distance("0000", "000f") == 4


def test_run_dedupe_hashes_every_unhashed_active_image(conn, tmp_path):
    path_a = tmp_path / "STARLINK" / "a.jpg"
    path_b = tmp_path / "STARLINK" / "b.jpg"
    _make_pattern_image(path_a, seed=1)
    _make_pattern_image(path_b, seed=2)
    _insert_active_image(conn, path_a)
    _insert_active_image(conn, path_b)

    result = dedupe.run_dedupe(conn)

    assert result["hashed"] == 2
    rows = conn.execute("SELECT perceptual_hash FROM images").fetchall()
    assert all(row["perceptual_hash"] is not None for row in rows)


def test_run_dedupe_groups_exact_duplicates_across_families(conn, tmp_path):
    path_a = tmp_path / "STARLINK" / "a.jpg"
    path_b = tmp_path / "ONEWEB" / "b.jpg"  # same image, different family
    _make_pattern_image(path_a, seed=1)
    _make_pattern_image(path_b, seed=1)
    id_a = _insert_active_image(conn, path_a, family="STARLINK")
    id_b = _insert_active_image(conn, path_b, family="ONEWEB")

    dedupe.run_dedupe(conn)

    row_a = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_a,)).fetchone()
    row_b = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_b,)).fetchone()
    assert row_a["duplicate_group_id"] is not None
    assert row_a["duplicate_group_id"] == row_b["duplicate_group_id"]


def test_run_dedupe_leaves_distinct_images_ungrouped(conn, tmp_path):
    path_a = tmp_path / "STARLINK" / "a.jpg"
    path_b = tmp_path / "STARLINK" / "b.jpg"
    _make_pattern_image(path_a, seed=1)
    _make_pattern_image(path_b, seed=99)
    id_a = _insert_active_image(conn, path_a)
    id_b = _insert_active_image(conn, path_b)

    result = dedupe.run_dedupe(conn)

    row_a = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_a,)).fetchone()
    row_b = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_b,)).fetchone()
    assert row_a["duplicate_group_id"] is None
    assert row_b["duplicate_group_id"] is None
    assert result["groups"] == 0


def test_run_dedupe_is_scoped_to_requested_families_without_clobbering_others(conn, tmp_path):
    starlink_a = tmp_path / "STARLINK" / "a.jpg"
    starlink_b = tmp_path / "STARLINK" / "b.jpg"
    oneweb_a = tmp_path / "ONEWEB" / "a.jpg"
    oneweb_b = tmp_path / "ONEWEB" / "b.jpg"
    for path in (starlink_a, starlink_b, oneweb_a, oneweb_b):
        _make_pattern_image(path, seed=1, tag=str(path))  # all look alike -- would group if considered together
    _insert_active_image(conn, starlink_a, family="STARLINK")
    _insert_active_image(conn, starlink_b, family="STARLINK")
    _insert_active_image(conn, oneweb_a, family="ONEWEB")
    id_oneweb_b = _insert_active_image(conn, oneweb_b, family="ONEWEB")

    dedupe.run_dedupe(conn, families=["ONEWEB"])
    first_group = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_oneweb_b,)).fetchone()[
        "duplicate_group_id"
    ]
    assert first_group is not None

    # A later scoped run for a different family must not wipe ONEWEB's grouping.
    dedupe.run_dedupe(conn, families=["STARLINK"])
    second_group = conn.execute("SELECT duplicate_group_id FROM images WHERE id = ?", (id_oneweb_b,)).fetchone()[
        "duplicate_group_id"
    ]
    assert second_group == first_group


# ---- Identical files ----


def _images(conn):
    return {
        r["id"]: (r["family"], r["status"], Path(r["path"]))
        for r in conn.execute("SELECT id, family, status, path FROM images")
    }


def test_identical_files_in_one_family_are_quarantined_automatically(conn, tmp_path):
    first = _insert_active_image(conn, _write(tmp_path / "imgs" / "STARLINK" / "a.jpg", seed=1), "STARLINK")
    copy = _insert_active_image(conn, _write(tmp_path / "imgs" / "STARLINK" / "b.jpg", seed=1), "STARLINK")
    look_alike = tmp_path / "imgs" / "STARLINK" / "c.jpg"
    _make_pattern_image(look_alike, seed=1, tag="different bytes")
    look_alike_id = _insert_active_image(conn, look_alike, "STARLINK")

    result = dedupe.run_dedupe(conn, quarantine_dir=tmp_path / "_quarantine")

    images = _images(conn)
    assert result["identical_copies_removed"] == 1 and result["families_to_choose"] == 0
    assert images[first][1] == "active"  # first downloaded is kept
    assert images[copy][1] == "rejected_duplicate"
    assert images[copy][2].is_file() and "_quarantine" in str(images[copy][2])  # moved, not deleted
    assert images[look_alike_id][1] == "active"  # same look, different file: a human decides
    assert result["groups"] == 1  # ...and it's still grouped with the kept copy for Review


def test_annotated_copy_is_the_one_kept(conn, tmp_path):
    first = _insert_active_image(conn, _write(tmp_path / "STARLINK" / "a.jpg", seed=1), "STARLINK")
    annotated = _insert_active_image(conn, _write(tmp_path / "STARLINK" / "b.jpg", seed=1), "STARLINK")
    conn.execute(
        "INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at) "
        "VALUES (?, 'Shape', '\"Cubical\"', 'alice', 'v1', ?)",
        (annotated, _now()),
    )
    conn.commit()
    dedupe.run_dedupe(conn, quarantine_dir=tmp_path / "_quarantine")
    images = _images(conn)
    assert images[annotated][1] == "active" and images[first][1] == "rejected_duplicate"


def test_identical_files_across_families_are_left_for_review(conn, tmp_path):
    a = _insert_active_image(conn, _write(tmp_path / "STARLINK" / "a.jpg", seed=1), "STARLINK")
    b = _insert_active_image(conn, _write(tmp_path / "ONEWEB" / "a.jpg", seed=1), "ONEWEB")
    result = dedupe.run_dedupe(conn, quarantine_dir=tmp_path / "_quarantine")
    assert result["identical_copies_removed"] == 0 and result["families_to_choose"] == 1
    assert {i: v[1] for i, v in _images(conn).items()} == {a: "active", b: "active"}


def _write(path: Path, seed: int) -> Path:
    _make_pattern_image(path, seed)
    return path
