"""Offline tests for the embed stage: which files get embedded, the content-hash
cache, and the stored vectors. A fake backbone stands in for the real model,
so nothing is downloaded."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import embed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeBackbone:
    """Embeds an image as its average color, normalized -- distinct per color."""

    def __init__(self, name="fake"):
        self.name = name
        self.calls = 0

    def embed(self, images):
        self.calls += 1
        vectors = []
        for image in images:
            r, g, b = image.resize((1, 1)).getpixel((0, 0))
            norm = (r * r + g * g + b * b) ** 0.5 or 1.0
            vectors.append([r / norm, g / norm, b / norm])
        return vectors


def _image(path: Path, color=(0, 0, 255)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color=color).save(path)
    return path


def _insert(conn, path: Path, status="active"):
    conn.execute(
        "INSERT INTO images (family, path, status, created_at) VALUES ('F', ?, ?, ?)", (str(path), status, _now())
    )
    conn.commit()


def _training_set(tmp_path: Path, paths: list[Path]) -> Path:
    out = tmp_path / "training" / "dataset.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps({"image": str(p), "labels": {}}) + "\n" for p in paths))
    return out


def test_embeds_active_and_training_set_images(conn, tmp_path):
    _insert(conn, _image(tmp_path / "db" / "blue.jpg"))
    _insert(conn, _image(tmp_path / "db" / "rejected.jpg", (9, 9, 9)), status="rejected_filter")
    old = _image(tmp_path / "old" / "red.png", (255, 0, 0))
    training = _training_set(tmp_path, [old, tmp_path / "old" / "gone.png"])
    backbone = FakeBackbone()

    result = embed.run_embed(conn, training_set=training, backbone=backbone)

    assert result["embedded"] == 2 and result["missing"] == 1
    vectors = embed.load_embeddings(conn, "fake")
    assert vectors[embed.file_sha256(old)] == pytest.approx([1.0, 0.0, 0.0])
    assert embed.file_sha256(tmp_path / "db" / "rejected.jpg") not in vectors


def test_rerun_only_embeds_new_files_and_same_content_once(conn, tmp_path):
    _insert(conn, _image(tmp_path / "a.jpg"))
    _insert(conn, _image(tmp_path / "copy_of_a.jpg"))  # identical bytes
    backbone = FakeBackbone()

    first = embed.run_embed(conn, backbone=backbone)
    assert (first["embedded"], first["already_embedded"]) == (1, 1)

    _insert(conn, _image(tmp_path / "b.jpg", (0, 255, 0)))
    second = embed.run_embed(conn, backbone=backbone)
    assert (second["embedded"], second["already_embedded"]) == (1, 2)


def test_moved_file_keeps_its_embedding(conn, tmp_path):
    original = _image(tmp_path / "images" / "sat.jpg")
    training = _training_set(tmp_path, [original])
    embed.run_embed(conn, training_set=training, backbone=FakeBackbone())

    moved = tmp_path / "images_annotated" / "sat.jpg"  # what the old tool's folder mode does
    moved.parent.mkdir()
    original.rename(moved)
    result = embed.run_embed(conn, training_set=_training_set(tmp_path, [moved]), backbone=FakeBackbone())
    assert (result["embedded"], result["already_embedded"]) == (0, 1)


def test_unreadable_files_are_counted_not_fatal(conn, tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not an image")
    _insert(conn, broken)
    _insert(conn, _image(tmp_path / "ok.jpg"))
    result = embed.run_embed(conn, backbone=FakeBackbone())
    assert (result["embedded"], result["unreadable"]) == (1, 1)


def test_backbones_are_cached_separately(conn, tmp_path):
    _insert(conn, _image(tmp_path / "a.jpg"))
    embed.run_embed(conn, backbone=FakeBackbone("one"))
    result = embed.run_embed(conn, backbone=FakeBackbone("two"))
    assert result["embedded"] == 1
    assert len(embed.load_embeddings(conn, "one")) == len(embed.load_embeddings(conn, "two")) == 1


def test_model_isnt_loaded_when_nothing_needs_embedding(conn, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tried to load the real model")

    monkeypatch.setattr(embed, "HFBackbone", refuse)
    result = embed.run_embed(conn)  # no images at all
    assert result["embedded"] == 0 and result["device"] is None


def test_batches_report_progress_and_stop_on_cancel(conn, tmp_path):
    for i in range(5):
        _insert(conn, _image(tmp_path / f"{i}.jpg", (i * 40, 0, 0)))
    progress = []
    result = embed.run_embed(conn, backbone=FakeBackbone(), batch_size=2, on_progress=lambda d, t: progress.append(d))
    assert progress == [2, 4, 5] and result["embedded"] == 5

    cancel = threading.Event()
    cancel.set()
    _insert(conn, _image(tmp_path / "late.jpg", (0, 0, 7)))
    stopped = embed.run_embed(conn, backbone=FakeBackbone(), cancel=cancel)
    assert stopped["cancelled"] and stopped["embedded"] == 0


def test_vector_blob_round_trip():
    vector = [0.25, -1.5, 3.0]
    assert embed.blob_to_vector(embed.vector_to_blob(vector)) == vector


def test_unknown_backbone_is_rejected(conn):
    with pytest.raises(ValueError):
        embed.run_embed(conn, model="resnet-from-somewhere")


def test_every_backbone_has_a_label():
    assert list(embed.BACKBONE_LABELS) == list(embed.BACKBONES)
    assert next(iter(embed.BACKBONES)) == embed.DEFAULT_BACKBONE  # listed first in the pickers
