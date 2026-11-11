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

from scraper import master
from scraper.ml import screener
from scraper.ml.dataset import DEFAULT_OUT_DIR, portable_path
from scraper.stages.embed import vector_to_blob

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
    # Added after measuring on real scrapes: launch-site and ground-hardware
    # photos used to score as "spacecraft hardware" and pass. With these, 37 of
    # 56 junk images are caught instead of 26, with every usable one still kept.
    "a rocket standing on a launch pad",
    "a launch tower or service gantry",
    "a rocket payload fairing or nose cone",
    "a rocket stage inside a hangar",
    "a satellite dish or radio telescope on the ground",
    "an aerial photograph of land",
    "a coin, medal or statue",
    "a museum display case",
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


def _score_and_embed(image_paths, model, processor, device, text_features) -> tuple[list[float], list]:
    """Scores a batch of images. Returns (scores, features), both aligned with
    image_paths: 0.0 and None for an unreadable file. The features are CLIP's
    unit-length image features, the ones the scores are computed from. Same
    math as reference/sources/clip_filter.py."""
    import torch
    from PIL import Image

    images, valid_idx = [], []
    for i, path in enumerate(image_paths):
        try:
            with Image.open(path) as image:
                images.append(image.convert("RGB"))
            valid_idx.append(i)
        except Exception:
            pass

    scores: list[float] = [0.0] * len(image_paths)
    features: list = [None] * len(image_paths)
    if not images:
        return scores, features

    inputs = processor(images=images, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        image_features = _as_tensor(model.get_image_features(**inputs))
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        logits = model.logit_scale.exp() * image_features @ text_features.T
        probs = logits.softmax(dim=1)

    n_pos = len(POSITIVE_PROMPTS)
    vectors = image_features.float().cpu().tolist()
    for row, i in enumerate(valid_idx):
        positive_avg = probs[row, :n_pos].mean().item()
        negative_avg = probs[row, n_pos:].mean().item()
        scores[i] = positive_avg / (positive_avg + negative_avg)
        features[i] = vectors[row]

    return scores, features


def score_images(image_paths, model, processor, device, text_features) -> list[float]:
    """Score a batch of images. Returns scores aligned with image_paths (0.0 for
    unreadable files)."""
    return _score_and_embed(image_paths, model, processor, device, text_features)[0]


class ClipScorer:
    """The real scorer: CLIP, loaded once. Called with a batch of paths, it
    returns their prompt scores and keeps that batch's image features in
    last_features. The trained screener (scraper/ml/screener.py) reads those
    same features, so the second opinion costs no second model."""

    name = screener.EMBEDDING_MODEL  # what these features are filed under in the embeddings table

    def __init__(self):
        self.model, self.processor, self.device, self.device_label = load_model()
        self.text_features = encode_prompts(self.model, self.processor, self.device)
        self.last_features: list = []

    def __call__(self, paths) -> list[float]:
        scores, self.last_features = _score_and_embed(
            paths, self.model, self.processor, self.device, self.text_features
        )
        return scores

    def embed(self, images: list) -> list[list[float]]:
        """Image features alone, for scraper.stages.embed.embed_files: how the
        screener gets features for the kept and rejected images it trains on."""
        import torch

        inputs = self.processor(images=images, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            features = _as_tensor(self.model.get_image_features(**inputs)).float()
            features = features / features.norm(dim=-1, keepdim=True)
        return features.cpu().tolist()


def _build_real_scorer():
    return ClipScorer()


def _remember_features(conn: sqlite3.Connection, model: str, batch, features: list) -> None:
    """Files the batch's image features in the embeddings table, so training
    the screener later doesn't have to read these images again."""
    now = _now()
    conn.executemany(
        """INSERT INTO embeddings (sha256, model, dim, vector, created_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(sha256, model) DO NOTHING""",
        [
            (row["content_sha256"], model, len(vector), vector_to_blob(vector), now)
            for row, vector in zip(batch, features)
            if vector is not None and row["content_sha256"]
        ],
    )


def run_filter(
    conn: sqlite3.Connection,
    quarantine_dir: Path = Path("_quarantine"),
    families: list[str] | None = None,
    min_score: float = MIN_SCORE,
    scorer=None,
    dry_run: bool = False,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    learned: Optional[screener.Screener] = None,
    log: Optional[Callable[[str], None]] = None,
    screener_dir: Path = DEFAULT_OUT_DIR,
) -> dict:
    """Run the filter stage against every active scraped image (optionally
    scoped to `families`). Imported images (a data/ folder) are left out:
    people already vetted them, and files in data/ are never moved. Makes
    zero network calls -- never touches download.py or any source module.

    `scorer` is a callable: list[Path] -> list[float]. When None, loads the real
    CLIP pipeline (requires torch/transformers installed). Tests inject a fake
    scorer so the real model never loads.

    Two opinions, and either one sets an image aside:
    - The prompt score (below `min_score`). Always on.
    - The trained screener (scraper/ml/screener.py), learned from this
      project's Review decisions. On only once it has enough decisions and has
      passed its check; with the real scorer it is loaded here, and retrained
      first if enough new decisions have come in. An injected `scorer` gets no
      trained screener unless `learned` is passed too (the scorer then needs
      a `last_features` list, like ClipScorer's).

    Every image's prompt score is saved with it (images.filter_score): Review
    shows the most likely satellites first, and knows what hasn't been
    screened. The trained screener's margin is saved in images.learned_score
    (below 0: it set the image aside).

    `dry_run` computes scores and reports counts exactly as a real run would, but
    skips the file move and DB update -- lets `--profile`/threshold tuning happen
    without touching anything on disk. It doesn't retrain the screener either.

    `cancel` is checked between batches (web GUI stop button) -- a run stops
    cleanly after its current batch finishes and reports partial counts, same
    checkpoint granularity as run_scrape_job's per-family check.

    `on_progress(images_done, total_images)` fires after each batch, same
    shape as run_scrape_job's per-family progress callback.
    """
    if scorer is None:
        scorer = _build_real_scorer()
        if learned is None and hasattr(scorer, "embed"):
            learned = screener.prepare(conn, scorer, screener_dir, retrain=not dry_run, log=log, cancel=cancel)

    # Only what nobody has reviewed yet: a person's Keep isn't second-guessed by the model.
    query = (
        "SELECT id, family, path, content_sha256 FROM images"
        " WHERE status = 'active' AND origin = 'scraped' AND reviewed_at IS NULL"
    )
    params = ()
    if families:
        placeholders = ",".join("?" for _ in families)
        query += f" AND family IN ({placeholders})"
        params = tuple(families)

    rows = conn.execute(query, params).fetchall()
    counts = {"kept": 0, "rejected": 0, "missing_file": 0}
    if learned is not None:
        counts["rejected_by_trained_screener"] = 0  # of the rejected: the ones the prompts alone would have kept

    for batch_start in range(0, len(rows), BATCH_SIZE):
        if cancel is not None and cancel.is_set():
            break

        batch = rows[batch_start : batch_start + BATCH_SIZE]
        paths = [Path(row["path"]) for row in batch]
        scores = scorer(paths)
        features = getattr(scorer, "last_features", None) or [None] * len(batch)
        margins = learned.margins(features) if learned is not None else [None] * len(batch)
        if not dry_run and getattr(scorer, "name", None) == screener.EMBEDDING_MODEL:
            _remember_features(conn, scorer.name, batch, features)

        for row, path, score, margin in zip(batch, paths, scores, margins):
            if not path.exists():
                counts["missing_file"] += 1
                continue

            by_screener = margin is not None and margin < 0
            if score >= min_score and not by_screener:
                counts["kept"] += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE images SET filtered_at = ?, filter_score = ?, learned_score = ? WHERE id = ?",
                        (_now(), score, margin, row["id"]),
                    )
                continue

            counts["rejected"] += 1
            if by_screener and score >= min_score:
                counts["rejected_by_trained_screener"] += 1
            if dry_run:
                continue

            dest = master.free_path(quarantine_dir / row["family"] / path.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dest))
            conn.execute(
                "UPDATE images SET path = ?, status = 'rejected_filter', reject_reason = 'filter', filtered_at = ?,"
                " filter_score = ?, learned_score = ? WHERE id = ?",
                (portable_path(dest), _now(), score, margin, row["id"]),
            )
            master.remember_rejection(conn, row["id"], "filter")

        if on_progress:
            on_progress(min(batch_start + BATCH_SIZE, len(rows)), len(rows))

    conn.commit()
    return counts
