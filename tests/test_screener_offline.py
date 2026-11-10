"""Offline tests for the trained screener (scraper/ml/screener.py) and its
place in the filter stage.

No CLIP model is loaded: image features are made-up vectors written straight
into the embeddings table (or computed by a fake embedder from an image's
color). The fit itself is real, so these need torch, like the attribute-head
tests.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from scraper import cli
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import screener
from scraper.stages import embed, review
from scraper.stages import filter as filter_module

pytest.importorskip("torch")

DIM = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


_counter = iter(range(1, 1_000_000))


def _add(conn, family, vector=None, kept=False, reason=None, sha=None, path=None):
    """One image row, kept or rejected, with its features already cached."""
    n = next(_counter)
    sha = sha or f"sha-{n}"
    status, reviewed = ("active", _now()) if kept else ("rejected_filter", _now() if reason != "filter" else None)
    cursor = conn.execute(
        "INSERT INTO images (family, path, status, created_at, reviewed_at, reject_reason, content_sha256)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (family, str(path or f"img-{n}.jpg"), status, _now(), reviewed, None if kept else reason, sha),
    )
    if vector is not None:
        conn.execute(
            "INSERT OR IGNORE INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)",
            (sha, screener.EMBEDDING_MODEL, len(vector), embed.vector_to_blob(vector), _now()),
        )
    conn.commit()
    return cursor.lastrowid


def _vector(rng, *spikes, noise=0.05):
    """Mostly noise, with a 1.0 at each index in `spikes`."""
    vector = [rng.gauss(0, noise) for _ in range(DIM)]
    for index in spikes:
        vector[index] += 1.0
    return vector


SATELLITE, JUNK = 0, 1  # feature directions: "looks like a satellite", "looks like the usual junk"


def _review_history(conn, junk_direction=lambda family_index: JUNK, kept=400, rejected=120, families=6):
    """`kept` kept images over 40 families, and `rejected` "Not a satellite"
    rejections over `families` families. `junk_direction(i)` picks what the
    junk of family i looks like."""
    rng = random.Random(7)
    for i in range(kept):
        _add(conn, f"KEPT {i % 40}", _vector(rng, SATELLITE), kept=True)
    for i in range(rejected):
        family_index = i % families
        _add(
            conn,
            f"JUNKY {family_index}",
            _vector(rng, SATELLITE, junk_direction(family_index)),
            reason="not_a_satellite",
        )
    return rng


class FakeEmbedder:
    name = screener.EMBEDDING_MODEL

    def embed(self, images):
        return [[channel / 255 for channel in image.getpixel((0, 0))] for image in images]


# ---- Learning: not enough decisions yet ----


def test_it_stays_off_and_says_what_it_needs_until_there_are_enough_rejections(conn, tmp_path):
    rng = random.Random(1)
    for i in range(30):
        _add(conn, f"KEPT {i}", _vector(rng, SATELLITE), kept=True)
    for i in range(12):
        _add(conn, f"JUNKY {i % 3}", _vector(rng, JUNK), reason="not_a_satellite")

    info = screener.status(conn, tmp_path)

    assert (info["state"], info["kept"], info["rejected"], info["rejected_families"]) == ("learning", 30, 12, 3)
    assert info["needs"]["rejected"] == screener.MIN_REJECTS
    assert screener.train(conn, out_dir=tmp_path) is None
    assert screener.prepare(conn, FakeEmbedder(), tmp_path) is None
    assert not screener.screener_path(tmp_path).exists()
    assert '12 of 100 "Not a satellite" rejections' in screener.describe(info)


def test_plenty_of_rejections_from_too_few_families_is_still_learning(conn, tmp_path):
    _review_history(conn, families=2)

    info = screener.status(conn, tmp_path)

    assert (info["state"], info["rejected"], info["rejected_families"]) == ("learning", 120, 2)
    assert screener.train(conn, out_dir=tmp_path) is None


def test_it_learns_only_from_kept_images_and_not_a_satellite_rejections(conn):
    _add(conn, "A", kept=True, sha="a-kept-file")
    _add(conn, "A", reason="not_a_satellite")
    _add(conn, "A", reason="filter")  # the filter's own guess, not a person's
    _add(conn, "A", reason="several_satellites")  # a satellite photo, just not a usable one
    _add(conn, "A", reason="off_limits_source", sha="from-an-off-limits-site")
    _add(conn, "A", kept=True, sha="from-an-off-limits-site")  # the same file, kept: still never trained on
    _add(conn, "B", reason="not_a_satellite", sha="a-kept-file")  # same file as a kept image: counts as kept

    rows = screener.training_rows(conn)

    assert sorted((row["family"], row["label"]) for row in rows) == [("A", 0), ("A", 1)]


# ---- Trained: on only if it passes on families it didn't train on ----


def test_junk_that_looks_alike_across_families_turns_it_on(conn, tmp_path):
    rng = _review_history(conn)
    assert screener.status(conn, tmp_path)["state"] == "untrained"
    logs = []

    saved = screener.train(conn, out_dir=tmp_path, log=logs.append)

    assert saved["state"] == "on"
    assert saved["check"]["junk_caught_pct"] >= 90
    assert saved["check"]["kept_lost_pct"] <= screener.MAX_KEPT_LOST
    assert (saved["kept"], saved["rejected"], saved["rejected_families"]) == (400, 120, 6)
    assert json.loads(screener.screener_path(tmp_path).read_text())["state"] == "on"
    assert "so it is on" in logs[-1]

    info = screener.status(conn, tmp_path)
    assert info["state"] == "on" and info["trained_on"] == {"kept": 400, "rejected": 120}
    assert screener.describe(info).startswith("On: trained on 400 kept and 120 rejected images.")

    trained = screener.prepare(conn, out_dir=tmp_path)
    satellite, junk = _vector(rng, SATELLITE), _vector(rng, SATELLITE, JUNK)
    margins = trained.margins([satellite, junk, None, [0.0] * 3])
    assert margins[0] > 0 > margins[1]
    assert margins[2:] == [None, None]  # an unreadable file; features from some other model


def test_junk_that_differs_per_family_fails_the_check_and_stays_off(conn, tmp_path):
    # Every family's junk looks different, so what it learns says nothing
    # about a family it hasn't seen: exactly what the check is there to catch.
    _review_history(conn, junk_direction=lambda family_index: 2 + family_index)

    saved = screener.train(conn, out_dir=tmp_path)

    assert saved["state"] == "off"
    assert saved["check"]["junk_caught_pct"] < screener.MIN_JUNK_CAUGHT
    assert screener.prepare(conn, FakeEmbedder(), tmp_path) is None
    info = screener.status(conn, tmp_path)
    assert info["state"] == "off" and info["check"]["junk_total"] == 120
    assert screener.describe(info).startswith("Off (it didn't pass its check)")


def test_every_family_is_checked_as_a_family_it_never_trained_on(conn):
    _review_history(conn)
    rows = [row for row in screener.training_rows(conn)]

    fold, folds = screener._fold_of(rows)

    assert folds == screener.FOLDS
    assert set(fold) == {row["family"] for row in rows}  # a family is in one fold, whole
    with_junk = [fold[f"JUNKY {i}"] for i in range(6)]
    assert set(with_junk) == set(range(folds))  # so every fold has junk to be tested on


def test_training_reads_the_images_it_has_no_features_for(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "MIN_REJECTS", 10)
    monkeypatch.setattr(screener, "MIN_FAMILIES", 2)
    for i in range(40):  # kept: blue. junk: red. The fake embedder's features are the pixel color.
        kept = i < 28
        path = tmp_path / f"{i}.png"
        Image.new("RGB", (2, 2), color=(0, i, 200) if kept else (200, i, 0)).save(path)
        family = f"KEPT {i % 7}" if kept else f"JUNKY {i % 3}"
        _add(
            conn, family, kept=kept, reason=None if kept else "not_a_satellite", sha=embed.file_sha256(path), path=path
        )

    assert screener.train(conn, out_dir=tmp_path) is None  # nothing cached and nothing to read them with

    saved = screener.train(conn, FakeEmbedder(), tmp_path)

    assert (saved["kept"], saved["rejected"], saved["rejected_families"]) == (28, 12, 3)
    assert len(embed.load_embeddings(conn, screener.EMBEDDING_MODEL)) == 40


# ---- Retraining as decisions come in ----


def test_it_retrains_once_enough_new_decisions_have_been_made(conn, tmp_path):
    rng = _review_history(conn)
    first = screener.train(conn, out_dir=tmp_path)

    for _ in range(screener.RETRAIN_AFTER - 1):
        _add(conn, "JUNKY 0", _vector(rng, SATELLITE, JUNK), reason="not_a_satellite")
    assert screener.prepare(conn, FakeEmbedder(), tmp_path) is not None
    assert json.loads(screener.screener_path(tmp_path).read_text())["trained_at"] == first["trained_at"]

    _add(conn, "KEPT 0", _vector(rng, SATELLITE), kept=True)  # the 25th decision since
    assert screener.prepare(conn, FakeEmbedder(), tmp_path, retrain=False) is not None  # a dry run leaves it alone
    assert json.loads(screener.screener_path(tmp_path).read_text())["trained_at"] == first["trained_at"]

    assert screener.prepare(conn, FakeEmbedder(), tmp_path) is not None
    again = json.loads(screener.screener_path(tmp_path).read_text())
    assert again["trained_at"] != first["trained_at"]
    assert (again["kept"], again["rejected"]) == (401, 120 + screener.RETRAIN_AFTER - 1)


# ---- In the filter stage: either opinion sets an image aside ----


class FakeScorer:
    """Stands in for ClipScorer: a prompt score and features per image, read
    from the file name ("<score>_<satellite|junk>_<n>.png")."""

    name = screener.EMBEDDING_MODEL

    def __init__(self):
        self.last_features = []

    def __call__(self, paths):
        self.last_features = [[1.0, 0.0] if "satellite" in path.name else [0.0, 1.0] for path in paths]
        return [float(path.name.split("_")[0]) for path in paths]

    def embed(self, images):
        raise AssertionError("not needed here")


LEARNED = screener.Screener(weights=[1.0, -1.0], bias=0.0)  # margin +1 for "satellite" features, -1 for "junk"


def _waiting(conn, tmp_path, name, family="Family"):
    path = tmp_path / "images" / family / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)
    cursor = conn.execute(
        "INSERT INTO images (family, path, status, created_at, content_sha256) VALUES (?, ?, 'active', ?, ?)",
        (family, str(path), _now(), f"sha-{name}"),
    )
    conn.commit()
    return cursor.lastrowid, path


def test_the_filter_sets_aside_what_either_opinion_rejects(conn, tmp_path):
    good, good_path = _waiting(conn, tmp_path, "0.9_satellite_1.png")
    prompts_only, _ = _waiting(conn, tmp_path, "0.2_satellite_2.png")
    screener_only, screener_only_path = _waiting(conn, tmp_path, "0.8_junk_3.png")
    both, _ = _waiting(conn, tmp_path, "0.1_junk_4.png")

    counts = filter_module.run_filter(conn, tmp_path / "_quarantine", scorer=FakeScorer(), learned=LEARNED)

    assert counts == {"kept": 1, "rejected": 3, "missing_file": 0, "rejected_by_trained_screener": 1}
    rows = {row["id"]: row for row in conn.execute("SELECT * FROM images")}
    assert (rows[good]["status"], rows[good]["filter_score"], rows[good]["learned_score"]) == ("active", 0.9, 1.0)
    assert good_path.exists() and not screener_only_path.exists()
    for image_id in (prompts_only, screener_only, both):
        assert (rows[image_id]["status"], rows[image_id]["reject_reason"]) == ("rejected_filter", "filter")
    assert rows[screener_only]["learned_score"] == -1.0
    assert (tmp_path / "_quarantine" / "Family" / "0.8_junk_3.png").is_file()

    # Review shows which opinion it was, and "Keep after all" works on either.
    set_aside = {image["id"]: image["by_trained_screener"] for image in review.screened_out(conn)}
    assert set_aside == {prompts_only: False, screener_only: True, both: True}
    review.keep_after_all(conn, screener_only)
    assert conn.execute("SELECT status FROM images WHERE id = ?", (screener_only,)).fetchone()["status"] == "active"

    # The features it just computed are kept, so training later doesn't read these images again.
    assert set(embed.load_embeddings(conn, screener.EMBEDDING_MODEL)) == {
        "sha-0.9_satellite_1.png",
        "sha-0.2_satellite_2.png",
        "sha-0.8_junk_3.png",
        "sha-0.1_junk_4.png",
    }


def test_a_dry_run_counts_the_screeners_rejects_and_touches_nothing(conn, tmp_path):
    image_id, path = _waiting(conn, tmp_path, "0.8_junk_1.png")

    counts = filter_module.run_filter(
        conn, tmp_path / "_quarantine", scorer=FakeScorer(), learned=LEARNED, dry_run=True
    )

    assert counts == {"kept": 0, "rejected": 1, "missing_file": 0, "rejected_by_trained_screener": 1}
    row = conn.execute("SELECT status, learned_score FROM images WHERE id = ?", (image_id,)).fetchone()
    assert (row["status"], row["learned_score"], path.exists()) == ("active", None, True)
    assert embed.load_embeddings(conn, screener.EMBEDDING_MODEL) == {}


def test_without_a_trained_screener_the_prompts_decide_alone(conn, tmp_path):
    image_id, path = _waiting(conn, tmp_path, "0.8_junk_1.png")

    counts = filter_module.run_filter(conn, tmp_path / "_quarantine", scorer=FakeScorer())

    assert counts == {"kept": 1, "rejected": 0, "missing_file": 0}
    row = conn.execute("SELECT filter_score, learned_score FROM images WHERE id = ?", (image_id,)).fetchone()
    assert (row["filter_score"], row["learned_score"], path.exists()) == (0.8, None, True)


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_real_scorer_brings_the_trained_screener_with_it(conn, tmp_path, monkeypatch, dry_run):
    _waiting(conn, tmp_path, "0.8_junk_1.png")
    scorer, asked = FakeScorer(), {}

    def fake_prepare(conn, embedder, out_dir, retrain, log, cancel):
        asked.update(embedder=embedder, retrain=retrain)
        return LEARNED

    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: scorer)
    monkeypatch.setattr(screener, "prepare", fake_prepare)

    counts = filter_module.run_filter(conn, tmp_path / "_quarantine", dry_run=dry_run)

    assert counts["rejected_by_trained_screener"] == 1
    assert asked == {"embedder": scorer, "retrain": not dry_run}  # a dry run never retrains


def test_a_plain_function_scorer_gets_no_trained_screener(conn, tmp_path, monkeypatch):
    _waiting(conn, tmp_path, "0.8_junk_1.png")
    monkeypatch.setattr(filter_module, "_build_real_scorer", lambda: lambda paths: [0.8 for _ in paths])
    monkeypatch.setattr(screener, "prepare", lambda *args, **kwargs: pytest.fail("shouldn't be asked"))

    assert filter_module.run_filter(conn, tmp_path / "_quarantine") == {"kept": 1, "rejected": 0, "missing_file": 0}


# ---- Command line ----


def test_the_screener_command_says_where_it_stands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["--db-path", str(tmp_path / "test.db"), "screener"])

    assert result.exit_code == 0
    assert 'Learning: 0 of 100 "Not a satellite" rejections so far, from 0 of 5 families.' in result.stdout


def test_paths_stay_relative_to_the_training_folder():
    assert screener.screener_path(Path("training")) == Path("training") / "screener.json"
