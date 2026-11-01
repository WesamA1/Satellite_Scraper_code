"""Phase 1 Task 1: the filter stage.

Ported from reference/sources/clip_filter.py: device chain, model loading,
prompt-ensemble scoring, same MIN_SCORE/BATCH_SIZE. Makes zero network calls --
torch/transformers are imported lazily (inside the functions that need them) so
this module is importable, and its orchestration testable via an injected
`scorer`, without torch installed at all.

DB-driven instead of folder-scanning: reads images WHERE status='active', moves
rejects into _quarantine/ (not rejected_images/) and updates the DB row instead of
writing clip_filter_log.txt/summary.json.
"""

import shutil
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

MIN_SCORE = 0.55
BATCH_SIZE = 16

POSITIVE_PROMPTS = [
    "a photo of a satellite in space",
    "a photo of a satellite in a cleanroom",
    "a photo of spacecraft hardware",
    "a photo of a satellite being assembled",
    "a photo of a satellite inside a rocket fairing",
    "a photo of a satellite scale model or mockup",
    "a photo of satellite solar panels or antenna hardware",
]

NEGATIVE_PROMPTS = [
    "a photo of Earth from space",
    "a diagram or chart",
    "a photo of people",
    "a postage stamp",
    "a photo of a building",
    "a photo of an airplane",
    "a night sky with stars",
    "a photo of a cat or animal",
    "a false color or infrared satellite image of Earth",
    "a radar image of Earth's surface",
    "a mission patch or logo",
    "a rocket launching with fire and smoke",
    "a launchpad or launch facility",
    "an infographic or size comparison chart",
    "a map showing satellite ground tracks or coverage",
    "a newspaper clipping or magazine page with text",
    "the interior of a space station",
    "a photo of the Moon or Mars surface",
    "a technical drawing or blueprint",
    "a screenshot of a computer interface or software",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cuda_actually_works() -> bool:
    """torch.cuda.is_available() can return True with no usable kernels for the
    detected GPU (e.g. a Blackwell card on an older torch build) -- confirm with
    a real tensor op."""
    import torch

    if not torch.cuda.is_available():
        return False
    try:
        x = torch.zeros(1, device="cuda")
        _ = (x + 1).cpu()
        return True
    except Exception:
        return False


def _mps_actually_works() -> bool:
    """torch.backends.mps.is_available() doesn't guarantee every op this stage
    uses has an MPS kernel -- confirm with a real tensor op, same empirical
    check as CUDA (CLAUDE.md: "MPS can be missing ops")."""
    import torch

    if not (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
        return False
    try:
        x = torch.zeros(1, device="mps")
        _ = (x + 1).cpu()
        return True
    except Exception:
        return False


def pick_device():
    """CUDA (smoke-tested) -> MPS (smoke-tested) -> DirectML -> CPU, per
    CLAUDE.md's device priority. Returns (device, label)."""
    import torch

    if _cuda_actually_works():
        return torch.device("cuda"), f"cuda ({torch.cuda.get_device_name(0)})"

    if _mps_actually_works():
        return torch.device("mps"), "mps"

    try:
        import torch_directml

        dml_device = torch_directml.device()
        return dml_device, f"directml ({torch_directml.device_name(0)})"
    except ImportError:
        pass
    except Exception:
        pass

    return torch.device("cpu"), "cpu"


def load_model():
    """Load CLIP model/processor and move to the best available device.

    Returns (model, processor, device, device_label).
    """
    from transformers import CLIPModel, CLIPProcessor

    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

    device, label = pick_device()
    model = model.to(device)
    model.eval()

    return model, processor, device, label


def _as_tensor(features):
    import torch

    if isinstance(features, torch.Tensor):
        return features
    return features.pooler_output


def encode_prompts(model, processor, device):
    import torch

    inputs = processor(text=POSITIVE_PROMPTS + NEGATIVE_PROMPTS, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        text_features = _as_tensor(model.get_text_features(**inputs))
    return text_features / text_features.norm(dim=-1, keepdim=True)


def score_images(image_paths, model, processor, device, text_features) -> list[float]:
    """Score a batch of images. Returns scores aligned with image_paths (0.0 for
    unreadable files). Same math as reference/sources/clip_filter.py."""
    import torch
    from PIL import Image

    images, valid_idx = [], []
    for i, path in enumerate(image_paths):
        try:
            images.append(Image.open(path).convert("RGB"))
            valid_idx.append(i)
        except Exception:
            pass

    scores = [0.0] * len(image_paths)
    if not images:
        return scores

    inputs = processor(images=images, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        image_features = _as_tensor(model.get_image_features(**inputs))
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        logits = model.logit_scale.exp() * image_features @ text_features.T
        probs = logits.softmax(dim=1)

    n_pos = len(POSITIVE_PROMPTS)
    for row, i in enumerate(valid_idx):
        positive_avg = probs[row, :n_pos].mean().item()
        negative_avg = probs[row, n_pos:].mean().item()
        scores[i] = positive_avg / (positive_avg + negative_avg)

    return scores


def _build_real_scorer():
    model, processor, device, _label = load_model()
    text_features = encode_prompts(model, processor, device)
    return lambda paths: score_images(paths, model, processor, device, text_features)


def run_filter(
    conn: sqlite3.Connection,
    quarantine_dir: Path = Path("_quarantine"),
    families: list[str] | None = None,
    min_score: float = MIN_SCORE,
    scorer=None,
    dry_run: bool = False,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Run the filter stage against every active image (optionally scoped to
    `families`). Makes zero network calls -- never touches download.py or any
    source module.

    `scorer` is a callable: list[Path] -> list[float]. When None, loads the real
    CLIP pipeline (requires torch/transformers installed). Tests inject a fake
    scorer so the real model never loads.

    `dry_run` computes scores and reports counts exactly as a real run would, but
    skips the file move and DB update -- lets `--profile`/threshold tuning happen
    without touching anything on disk.

    `cancel` is checked between batches (web GUI stop button) -- a run stops
    cleanly after its current batch finishes and reports partial counts, same
    checkpoint granularity as run_scrape_job's per-family check.

    `on_progress(images_done, total_images)` fires after each batch, same
    shape as run_scrape_job's per-family progress callback.
    """
    if scorer is None:
        scorer = _build_real_scorer()

    query = "SELECT id, family, path FROM images WHERE status = 'active'"
    params = ()
    if families:
        placeholders = ",".join("?" for _ in families)
        query += f" AND family IN ({placeholders})"
        params = tuple(families)

    rows = conn.execute(query, params).fetchall()
    counts = {"kept": 0, "rejected": 0, "missing_file": 0}

    for batch_start in range(0, len(rows), BATCH_SIZE):
        if cancel is not None and cancel.is_set():
            break

        batch = rows[batch_start : batch_start + BATCH_SIZE]
        paths = [Path(row["path"]) for row in batch]
        scores = scorer(paths)

        for row, path, score in zip(batch, paths, scores):
            if not path.exists():
                counts["missing_file"] += 1
                continue

            if score >= min_score:
                counts["kept"] += 1
                continue

            counts["rejected"] += 1
            if dry_run:
                continue

            dest = quarantine_dir / row["family"] / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            conn.execute(
                "UPDATE images SET path = ?, status = 'rejected_filter' WHERE id = ?",
                (str(dest), row["id"]),
            )

        if on_progress:
            on_progress(min(batch_start + BATCH_SIZE, len(rows)), len(rows))

    conn.commit()
    return counts
