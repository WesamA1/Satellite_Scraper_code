"""Offline tests for Phase 1 Task 1's filter stage.

Injects a fake scorer, so the real CLIP/torch pipeline is never loaded -- torch
does not need to be installed for these tests to pass. The pick_device() tests
below are the one exception: they inject a minimal fake `torch` module via
sys.modules instead, so the CUDA/MPS/DirectML/CPU fallback chain itself can be
exercised without a real torch install.
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages.filter import run_filter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _make_image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color="blue").save(path)


def _insert_active_image(conn, path: Path, family="Example Family"):
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES (?, ?, 'active', ?)",
        (family, str(path), _now()),
    )
    conn.commit()


def test_high_score_images_stay_active(conn, tmp_path):
    path = tmp_path / "satellite_images" / "Example Family" / "1.jpg"
    _make_image(path)
    _insert_active_image(conn, path)

    counts = run_filter(conn, quarantine_dir=tmp_path / "_quarantine", scorer=lambda paths: [0.9 for _ in paths])

    assert counts == {"kept": 1, "rejected": 0, "missing_file": 0}
    row = conn.execute("SELECT * FROM images").fetchone()
    assert row["status"] == "active"
    assert Path(row["path"]) == path
    assert path.exists()


def test_low_score_images_move_to_quarantine(conn, tmp_path):
    path = tmp_path / "satellite_images" / "Example Family" / "1.jpg"
    _make_image(path)
    _insert_active_image(conn, path)

    quarantine_dir = tmp_path / "_quarantine"
    counts = run_filter(conn, quarantine_dir=quarantine_dir, scorer=lambda paths: [0.1 for _ in paths])
    assert conn.execute("SELECT reject_reason FROM images").fetchone()["reject_reason"] == "filter"

    assert counts == {"kept": 0, "rejected": 1, "missing_file": 0}
    row = conn.execute("SELECT * FROM images").fetchone()
    assert row["status"] == "rejected_filter"
    assert not path.exists()
    new_path = Path(row["path"])
    assert new_path.exists()
    assert new_path == quarantine_dir / "Example Family" / "1.jpg"


def test_rerun_never_triggers_a_download(conn, tmp_path, monkeypatch):
    """The acceptance criterion: changing filter settings and re-running touches
    zero network calls. Fail the test if anything in download.py's modules is
    even imported as a result of running the filter stage."""
    import sys

    for mod_name in ("scraper.stages.download", "scraper.sources.wikimedia", "scraper.sources.nasa_images"):
        monkeypatch.setitem(sys.modules, mod_name, None)

    path = tmp_path / "satellite_images" / "Example Family" / "1.jpg"
    _make_image(path)
    _insert_active_image(conn, path)

    # Runs twice with different thresholds, as the acceptance criterion implies
    # ("changing filter settings and re-running").
    run_filter(conn, quarantine_dir=tmp_path / "_quarantine", min_score=0.9, scorer=lambda paths: [0.95 for _ in paths])
    counts = run_filter(
        conn, quarantine_dir=tmp_path / "_quarantine", min_score=0.3, scorer=lambda paths: [0.5 for _ in paths]
    )

    assert counts["kept"] == 1


def test_filter_can_be_scoped_to_one_family(conn, tmp_path):
    path_a = tmp_path / "satellite_images" / "Family A" / "1.jpg"
    path_b = tmp_path / "satellite_images" / "Family B" / "1.jpg"
    _make_image(path_a)
    _make_image(path_b)
    _insert_active_image(conn, path_a, family="Family A")
    _insert_active_image(conn, path_b, family="Family B")

    counts = run_filter(
        conn,
        quarantine_dir=tmp_path / "_quarantine",
        families=["Family A"],
        scorer=lambda paths: [0.1 for _ in paths],
    )

    assert counts == {"kept": 0, "rejected": 1, "missing_file": 0}
    status_a = conn.execute("SELECT status FROM images WHERE family = 'Family A'").fetchone()["status"]
    status_b = conn.execute("SELECT status FROM images WHERE family = 'Family B'").fetchone()["status"]
    assert status_a == "rejected_filter"
    assert status_b == "active"  # untouched -- not in scope


def test_dry_run_reports_but_does_not_move_or_update(conn, tmp_path):
    path = tmp_path / "satellite_images" / "Example Family" / "1.jpg"
    _make_image(path)
    _insert_active_image(conn, path)

    counts = run_filter(
        conn, quarantine_dir=tmp_path / "_quarantine", scorer=lambda paths: [0.1 for _ in paths], dry_run=True
    )

    assert counts == {"kept": 0, "rejected": 1, "missing_file": 0}
    row = conn.execute("SELECT * FROM images").fetchone()
    assert row["status"] == "active"  # untouched despite being counted as rejected
    assert path.exists()  # never moved
    assert not (tmp_path / "_quarantine").exists()


def test_on_progress_fires_once_per_batch(conn, tmp_path):
    from scraper.stages.filter import BATCH_SIZE

    for i in range(BATCH_SIZE + 5):  # two batches: BATCH_SIZE, then 5
        path = tmp_path / "satellite_images" / "Example Family" / f"{i}.jpg"
        _make_image(path)
        _insert_active_image(conn, path)

    calls = []
    run_filter(
        conn,
        quarantine_dir=tmp_path / "_quarantine",
        scorer=lambda paths: [0.9 for _ in paths],
        on_progress=lambda completed, total: calls.append((completed, total)),
    )

    assert calls == [(BATCH_SIZE, BATCH_SIZE + 5), (BATCH_SIZE + 5, BATCH_SIZE + 5)]


class _FakeTensor:
    def __init__(self, broken=False):
        self.broken = broken

    def __add__(self, other):
        if self.broken:
            raise RuntimeError("no kernel for this op")
        return self

    def cpu(self):
        return self


def _install_fake_torch(monkeypatch, *, cuda_available, mps_available, mps_broken=False):
    """CLAUDE.md requires an empirical smoke test per backend, not just
    is_available() -- mps_broken simulates is_available()=True but the actual
    tensor op failing, which pick_device() must treat as unusable."""
    fake_torch = types.ModuleType("torch")
    fake_torch.device = lambda name: f"device:{name}"
    fake_torch.zeros = lambda *a, **k: _FakeTensor(broken=mps_broken)
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: cuda_available, get_device_name=lambda i: "Fake GPU")
    fake_torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps_available))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torch_directml", None)  # force ImportError, same as not installed


def test_pick_device_prefers_mps_over_cpu_when_cuda_unavailable(monkeypatch):
    from scraper.stages.filter import pick_device

    _install_fake_torch(monkeypatch, cuda_available=False, mps_available=True)

    device, label = pick_device()

    assert label == "mps"
    assert device == "device:mps"


def test_pick_device_falls_through_to_cpu_when_mps_smoke_test_fails(monkeypatch):
    """is_available() alone isn't enough -- a real but kernel-missing MPS
    backend must not be picked, same empirical standard as CUDA."""
    from scraper.stages.filter import pick_device

    _install_fake_torch(monkeypatch, cuda_available=False, mps_available=True, mps_broken=True)

    device, label = pick_device()

    assert label == "cpu"
    assert device == "device:cpu"


def test_imported_images_are_never_filtered_or_moved(conn, tmp_path):
    imported = tmp_path / "data" / "images" / "SAT_01.jpg"
    _make_image(imported)
    conn.execute(
        "INSERT INTO images (family, path, status, created_at, origin) VALUES ('SAT', ?, 'active', ?, 'imported')",
        (str(imported), _now()),
    )
    scraped = tmp_path / "satellite_images" / "SAT" / "SAT_wikimedia_01.jpg"
    _make_image(scraped)
    _insert_active_image(conn, scraped, family="SAT")

    seen = []
    counts = run_filter(
        conn, quarantine_dir=tmp_path / "_quarantine", scorer=lambda paths: seen.extend(paths) or [0.0 for _ in paths]
    )

    assert seen == [scraped] and counts["rejected"] == 1  # only the scraped one was even scored
    assert imported.is_file()
    status = conn.execute("SELECT status FROM images WHERE origin = 'imported'").fetchone()["status"]
    assert status == "active"


def test_the_score_is_saved_with_every_image(conn, tmp_path):
    good = tmp_path / "satellite_images" / "Example Family" / "good.jpg"
    junk = tmp_path / "satellite_images" / "Example Family" / "junk.jpg"
    _make_image(good)
    _make_image(junk)
    _insert_active_image(conn, good)
    _insert_active_image(conn, junk)

    def scorer(paths):
        return [0.9 if path.name == "good.jpg" else 0.2 for path in paths]

    run_filter(conn, quarantine_dir=tmp_path / "_quarantine", scorer=scorer, dry_run=True)
    assert [row[0] for row in conn.execute("SELECT filter_score FROM images")] == [
        None,
        None,
    ]  # a dry run saves nothing

    run_filter(conn, quarantine_dir=tmp_path / "_quarantine", scorer=scorer)
    scores = {Path(row["path"]).name: row["filter_score"] for row in conn.execute("SELECT * FROM images")}
    assert scores == {"good.jpg": 0.9, "junk.jpg": 0.2}  # the rejected one too: "closest calls first"


def test_a_quarantined_path_inside_the_project_is_recorded_relative(conn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("satellite_images") / "Example Family" / "1.jpg"
    _make_image(path)
    _insert_active_image(conn, path)

    run_filter(conn, quarantine_dir=Path("_quarantine"), scorer=lambda paths: [0.1 for _ in paths])

    # Forward slashes, no drive letter: the same record works on Windows, Mac and Linux.
    assert conn.execute("SELECT path FROM images").fetchone()[0] == "_quarantine/Example Family/1.jpg"


def test_launch_site_and_ground_hardware_prompts_are_negative():
    from scraper.stages import filter as filter_module

    negatives = " ".join(filter_module.NEGATIVE_PROMPTS)
    for phrase in ("launch pad", "gantry", "fairing", "satellite dish", "aerial photograph"):
        assert phrase in negatives
    assert not set(filter_module.POSITIVE_PROMPTS) & set(filter_module.NEGATIVE_PROMPTS)
