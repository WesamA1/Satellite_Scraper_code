"""The embed stage: one embedding per image file from a frozen, pretrained
image model (the "backbone"), cached in the embeddings table.

Phase 3, step 2. The attribute heads (step 3) train on these vectors instead
of on pixels -- that's what makes training take minutes on a laptop GPU and
work with only ~1,000 labeled images. Nothing here trains the backbone.

Backbones (BACKBONES), swappable with --model so they can be compared:
- dinov3-vitb16 (default): self-supervised on images alone, so its features
  track shape and structure (panels, antennas, docking hardware) -- most of
  the attribute list. Gated on Hugging Face: needs HF_TOKEN in .env from an
  account that accepted Meta's DINOv3 terms.
- siglip2-base: image-text trained (Apache 2.0, not gated); may do better on
  attributes people describe in words, like color. Worth comparing.

What gets embedded: every active image, plus every image in a training set
built by scraper.ml.dataset (the old tool's images live outside the images
table). Files are keyed by content hash, so a re-run only embeds new
images, and a moved or duplicated file isn't embedded twice.
"""

import hashlib
import sqlite3
import threading
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from dotenv import load_dotenv

load_dotenv()  # HF_TOKEN, read by huggingface_hub when downloading gated weights

BACKBONES = {
    "dinov3-vitb16": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "siglip2-base": "google/siglip2-base-patch16-224",
}
BACKBONE_LABELS = {
    "dinov3-vitb16": "DINOv3 (ViT-B/16)",
    "siglip2-base": "SigLIP 2 (base)",
}
DEFAULT_BACKBONE = "dinov3-vitb16"
DEFAULT_BATCH_SIZE = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vector_to_blob(vector) -> bytes:
    return array("f", [float(v) for v in vector]).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()


class HFBackbone:
    """A Hugging Face image model, frozen, on the best working device
    (same smoke-tested CUDA -> MPS -> DirectML -> CPU order as the filter)."""

    def __init__(self, name: str = DEFAULT_BACKBONE):
        if name not in BACKBONES:
            raise ValueError(f"Unknown backbone '{name}'. Pick one of: {', '.join(BACKBONES)}")
        from transformers import AutoImageProcessor, AutoModel

        from scraper.stages.filter import pick_device

        self.name = name
        try:
            self.processor = AutoImageProcessor.from_pretrained(BACKBONES[name])
            model = AutoModel.from_pretrained(BACKBONES[name])
        except OSError as e:
            if "gated" in str(e).lower() or "401" in str(e) or "403" in str(e):
                raise RuntimeError(
                    f"{BACKBONES[name]} is gated on Hugging Face. Add HF_TOKEN to .env -- a token from an account "
                    "that has accepted the model's terms on its Hugging Face page."
                ) from e
            raise
        self.device, self.device_label = pick_device()
        self.model = model.to(self.device)
        self.model.eval()

    def embed(self, images: list) -> list[list[float]]:
        import torch

        from scraper.stages.filter import _as_tensor

        inputs = self.processor(images=images, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            if hasattr(self.model, "get_image_features"):  # SigLIP-style: the image tower's pooled output
                features = _as_tensor(self.model.get_image_features(**inputs))
            else:  # DINO-style: the class token
                features = self.model(**inputs).pooler_output
            features = features.float()
            features = features / features.norm(dim=-1, keepdim=True)
        return features.cpu().tolist()


class _LazyBackbone:
    """Loads (and the first time, downloads) the real model only once a file
    actually needs embedding -- a re-run with nothing new stays instant."""

    def __init__(self, name: str):
        self.name = name
        self.loaded = None

    def embed(self, images: list) -> list[list[float]]:
        if self.loaded is None:
            self.loaded = HFBackbone(self.name)
        return self.loaded.embed(images)


def _image_paths(conn: sqlite3.Connection, training_set: Optional[Path]) -> list[Path]:
    paths = [Path(row["path"]) for row in conn.execute("SELECT path FROM images WHERE status = 'active'")]
    if training_set is not None:
        from scraper.ml.dataset import load_examples

        paths += [Path(example["image"]) for example in load_examples(training_set)]
    seen, unique = set(), []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def embed_files(
    conn: sqlite3.Connection,
    paths: Iterable[Path],
    backbone,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Embeds every file not already embedded with this backbone. `backbone`
    needs a .name and .embed(list of PIL images) -> list of vectors."""
    from PIL import Image

    counts = {"embedded": 0, "already_embedded": 0, "missing": 0, "unreadable": 0, "cancelled": False}
    done = {row["sha256"] for row in conn.execute("SELECT sha256 FROM embeddings WHERE model = ?", (backbone.name,))}

    todo: dict[str, Path] = {}
    for path in paths:
        if not path.is_file():
            counts["missing"] += 1
            continue
        sha = file_sha256(path)
        if sha in done or sha in todo:
            counts["already_embedded"] += 1
        else:
            todo[sha] = path

    items = list(todo.items())
    for start in range(0, len(items), batch_size):
        if cancel is not None and cancel.is_set():
            counts["cancelled"] = True
            break
        batch, images = [], []
        for sha, path in items[start : start + batch_size]:
            try:
                with Image.open(path) as img:
                    images.append(img.convert("RGB"))
                batch.append(sha)
            except Exception:
                counts["unreadable"] += 1
        if images:
            vectors = backbone.embed(images)
            now = _now()
            with conn:
                conn.executemany(
                    """INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(sha256, model) DO NOTHING""",
                    [(sha, backbone.name, len(v), vector_to_blob(v), now) for sha, v in zip(batch, vectors)],
                )
            counts["embedded"] += len(images)
        if on_progress:
            on_progress(min(start + batch_size, len(items)), len(items))
    return counts


def run_embed(
    conn: sqlite3.Connection,
    model: str = DEFAULT_BACKBONE,
    training_set: Optional[Path] = None,
    backbone=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Embeds every active image and every training-set image not embedded
    with this backbone yet."""
    if model not in BACKBONES and backbone is None:
        raise ValueError(f"Unknown backbone '{model}'. Pick one of: {', '.join(BACKBONES)}")
    paths = _image_paths(conn, training_set)
    backbone = backbone or _LazyBackbone(model)
    counts = embed_files(conn, paths, backbone, batch_size, cancel, on_progress)
    loaded = getattr(backbone, "loaded", backbone)
    return {"model": backbone.name, "images": len(paths), **counts, "device": getattr(loaded, "device_label", None)}


def load_embeddings(conn: sqlite3.Connection, model: str = DEFAULT_BACKBONE) -> dict[str, list[float]]:
    """sha256 -> vector for every file embedded with this backbone."""
    return {
        row["sha256"]: blob_to_vector(row["vector"])
        for row in conn.execute("SELECT sha256, vector FROM embeddings WHERE model = ?", (model,))
    }


if __name__ == "__main__":
    import sys

    from scraper.db.connection import get_connection
    from scraper.db.migrate import apply_migrations

    db = get_connection(Path("scraper.db"))
    apply_migrations(db)
    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BACKBONE
    print(run_embed(db, model=name, on_progress=lambda d, t: print(f"  {d}/{t}", end="\r")))
