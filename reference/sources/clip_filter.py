# sources/clip_filter.py
# Uses CLIP to filter downloaded satellite images.
# Removes images that are not actually photos of satellite hardware.
#
# Run this after wikimedia.py has finished downloading images.

import sys
import torch
from transformers import CLIPProcessor, CLIPModel
from PIL import Image
from pathlib import Path
import shutil
import json
from collections import defaultdict

# Windows consoles/pipes default to cp1252, which can't encode characters
# like ≥ that this script prints.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Configuration ─────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent  # repo root (one level above sources/)

IMAGE_DIR    = _ROOT / "satellite_images"
REJECTED_DIR = _ROOT / "rejected_images"
LOG_FILE     = _ROOT / "clip_filter_log.txt"
SUMMARY_FILE = _ROOT / "clip_filter_summary.json"
NO_IMAGES_FILE = _ROOT / "clip_filter_no_images.txt"

# Minimum confidence score to keep an image (0.0 - 1.0)
# Images scoring below this on the satellite prompt are rejected
MIN_SCORE = 0.55

# What we're looking for vs what we want to reject
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

# Supported image extensions
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}

# Images scored per forward pass. Text prompts are encoded once up front, so
# batching images is the main throughput lever (matters a lot on CPU).
BATCH_SIZE = 16


# ── Device Detection ──────────────────────────────────────────────────────────

def _cuda_actually_works():
    """
    torch.cuda.is_available() returns True even when the installed PyTorch
    has no kernels for the detected GPU's compute capability (e.g. a
    Blackwell card on a torch built only up to sm_90). Run a tiny op to
    confirm we can actually compute on the device before trusting it.
    """
    if not torch.cuda.is_available():
        return False
    try:
        x = torch.zeros(1, device="cuda")
        _ = (x + 1).cpu()
        return True
    except Exception as e:
        print(f"  CUDA detected but unusable -> falling back. ({e.__class__.__name__})")
        return False


def pick_device():
    """
    Pick the best available compute device.

    Priority:
      1. CUDA       — NVIDIA (or ROCm-built PyTorch, which also exposes
                      itself as 'cuda'). We verify with a smoke-test.
      2. DirectML   — Microsoft DirectML backend (torch_directml). Works on
                      any DX12 GPU including AMD Radeon / Ryzen iGPUs.
      3. CPU        — Always available. Slower but bulletproof.

    Note: the Ryzen AI NPU is NOT used here — PyTorch doesn't target it
    directly. NPU acceleration would require ONNX Runtime + the AMD
    Ryzen AI SDK, which is a much bigger change to this script.
    """
    if _cuda_actually_works():
        name = torch.cuda.get_device_name(0)
        return torch.device("cuda"), f"cuda ({name})"

    try:
        import torch_directml
        dml_device = torch_directml.device()
        return dml_device, f"directml ({torch_directml.device_name(0)})"
    except ImportError:
        pass
    except Exception:
        pass

    return torch.device("cpu"), "cpu"


# ── Load Model ────────────────────────────────────────────────────────────────

def load_model():
    """Load CLIP model and move to best available device.

    Returns (model, processor, device, device_label).
    """
    print("Loading CLIP model...")
    model     = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")

    device, label = pick_device()
    print(f"Using device: {label}")
    model = model.to(device)
    model.eval()

    return model, processor, device, label


# ── Score Images ──────────────────────────────────────────────────────────────

def _as_tensor(features):
    """get_text_features/get_image_features return a plain tensor on
    transformers 4.x but an output object with pooler_output on 5.x."""
    if isinstance(features, torch.Tensor):
        return features
    return features.pooler_output


def encode_prompts(model, processor, device):
    """Encode all text prompts once. The prompts never change, so re-encoding
    them for every image (the old behavior) just multiplied the runtime."""
    inputs = processor(
        text=POSITIVE_PROMPTS + NEGATIVE_PROMPTS,
        return_tensors="pt",
        padding=True,
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        text_features = _as_tensor(model.get_text_features(**inputs))
    return text_features / text_features.norm(dim=-1, keepdim=True)


def score_images(image_paths, model, processor, device, text_features):
    """Score a batch of images against the precomputed prompt embeddings.

    Returns a list of scores aligned with image_paths (0.0 for unreadable
    files). Score = fraction of softmax probability captured by the positive
    prompts, corrected for unequal group sizes: neutral image → 0.5,
    pure satellite hardware → 1.0. Matches the old per-image math exactly.
    """
    images, valid_idx = [], []
    for i, path in enumerate(image_paths):
        try:
            images.append(Image.open(path).convert("RGB"))
            valid_idx.append(i)
        except Exception as e:
            print(f"  Could not open {path.name}: {e}")

    scores = [0.0] * len(image_paths)
    if not images:
        return scores

    inputs = processor(images=images, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        image_features = _as_tensor(model.get_image_features(**inputs))
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        # Same computation as model(...).logits_per_image, without re-running
        # the text encoder every call.
        logits = model.logit_scale.exp() * image_features @ text_features.T
        probs  = logits.softmax(dim=1)

    n_pos = len(POSITIVE_PROMPTS)
    for row, i in enumerate(valid_idx):
        positive_avg = probs[row, :n_pos].mean().item()
        negative_avg = probs[row, n_pos:].mean().item()
        scores[i] = positive_avg / (positive_avg + negative_avg)

    return scores


def score_image(image_path, model, processor, device, text_features=None):
    """Score a single image (convenience wrapper around score_images)."""
    if text_features is None:
        text_features = encode_prompts(model, processor, device)
    return score_images([image_path], model, processor, device, text_features)[0]


# ── Summary Helpers ───────────────────────────────────────────────────────────

def _score_band(score):
    """Bucket a score into a human-readable confidence band."""
    if score >= 0.75:
        return "high (≥75%)"
    elif score >= 0.55:
        return "medium (55–74%)"
    else:
        return "low (40–54%)"


def print_summary(family_stats, kept, rejected, empty_families, removed_families, device_label):
    """Print a structured summary to stdout after filtering completes."""
    total = kept + rejected
    pass_rate = (kept / total * 100) if total else 0.0

    print()
    print("=" * 50)
    print("  CLIP FILTER SUMMARY")
    print("=" * 50)
    print(f"  Device used      : {device_label}")
    print(f"  Score threshold  : {MIN_SCORE:.0%}")
    print(f"  Total processed  : {total}")
    print(f"  Kept             : {kept}  ({pass_rate:.1f}%)")
    print(f"  Rejected         : {rejected}  ({100 - pass_rate:.1f}%)")
    print()

    # Per-family breakdown (only families that had at least one image)
    print(f"  {'Family':<30} {'Kept':>5} {'Drop':>5} {'Pass%':>6}  {'Avg score':>9}  {'Top score':>9}")
    print(f"  {'-'*30} {'-'*5} {'-'*5} {'-'*6}  {'-'*9}  {'-'*9}")

    for name, s in sorted(family_stats.items()):
        fam_total = s["kept"] + s["dropped"]
        fam_pass  = (s["kept"] / fam_total * 100) if fam_total else 0.0
        avg_score = (sum(s["scores"]) / len(s["scores"])) if s["scores"] else 0.0
        top_score = max(s["scores"]) if s["scores"] else 0.0
        print(f"  {name:<30} {s['kept']:>5} {s['dropped']:>5} {fam_pass:>5.1f}%  {avg_score:>8.1%}  {top_score:>8.1%}")

    # Families with no usable images after filtering
    if empty_families:
        print()
        print(f"  Families with no images remaining ({len(empty_families)}):")
        for name in sorted(empty_families):
            print(f"    - {name}")

    # Folders that were already empty before filtering started
    if removed_families:
        print()
        print(f"  Folders deleted (were already empty before run) ({len(removed_families)}):")
        for name in sorted(removed_families):
            print(f"    - {name}")

    print("=" * 50)
    print(f"  Log    : {LOG_FILE}")
    print(f"  Summary: {SUMMARY_FILE}")
    if empty_families or removed_families:
        print(f"  No-image families: {NO_IMAGES_FILE}")
    print("=" * 50)
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"IMAGE_DIR resolved to: {IMAGE_DIR.resolve()}")
    if not IMAGE_DIR.exists():
        print(f"Image directory not found: {IMAGE_DIR.resolve()}")
        raise SystemExit(1)

    model, processor, device_obj, device_label = load_model()
    text_features = encode_prompts(model, processor, device_obj)

    REJECTED_DIR.mkdir(exist_ok=True)

    kept     = 0
    rejected = 0
    log      = []

    # Per-family stats: kept count, dropped count, list of kept scores
    family_stats = defaultdict(lambda: {"kept": 0, "dropped": 0, "scores": []})

    # Families whose folder was deleted / recorded as having no images
    empty_families   = []  # had images, all were rejected
    removed_families = []  # folder existed but was empty before we started

    family_dirs = sorted([d for d in IMAGE_DIR.iterdir() if d.is_dir()])
    print(f"\nScanning {len(family_dirs)} family folders...\n")

    for family_dir in family_dirs:
        images = [
            f for f in family_dir.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS
            and not f.name.startswith("._")
        ]

        # Folder existed but had no images at all — delete it immediately
        if not images:
            removed_families.append(family_dir.name)
            try:
                family_dir.rmdir()
                print(f"[{family_dir.name}] empty folder — deleted")
            except OSError:
                # Non-empty due to non-image files; leave it alone
                print(f"[{family_dir.name}] no images but folder not empty — skipped")
            continue

        print(f"[{family_dir.name}] {len(images)} images")
        stats = family_stats[family_dir.name]

        for batch_start in range(0, len(images), BATCH_SIZE):
            batch  = images[batch_start:batch_start + BATCH_SIZE]
            scores = score_images(batch, model, processor, device_obj, text_features)

            for img_path, score in zip(batch, scores):
                if score >= MIN_SCORE:
                    status = "[KEEP]"
                    kept  += 1
                    stats["kept"]   += 1
                    stats["scores"].append(score)
                else:
                    status    = "[DROP]"
                    rejected += 1
                    stats["dropped"] += 1
                    reject_dest = REJECTED_DIR / family_dir.name / img_path.name
                    reject_dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(img_path), str(reject_dest))

                print(f"  {status} {img_path.name[:50]:<50} score: {score:.2%}")
                log.append(f"{status} {family_dir.name}/{img_path.name} - {score:.2%}")

        # After processing, delete the family folder if it's now empty
        remaining = [
            f for f in family_dir.iterdir()
            if f.suffix.lower() in IMAGE_EXTENSIONS
            and not f.name.startswith("._")
        ]
        if not remaining:
            empty_families.append(family_dir.name)
            try:
                # Remove any leftover non-image files then the dir itself
                shutil.rmtree(family_dir)
                print(f"  [{family_dir.name}] all images rejected — folder deleted")
            except Exception as e:
                print(f"  [{family_dir.name}] could not delete folder: {e}")

    # ── Write log ─────────────────────────────────────────────────────────────
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(log))

    # ── Write no-images file ──────────────────────────────────────────────────
    all_no_image = sorted(set(empty_families) | set(removed_families))
    if all_no_image:
        with open(NO_IMAGES_FILE, "w", encoding="utf-8") as f:
            f.write("# Families with no usable satellite images after CLIP filtering\n")
            f.write(f"# Generated by clip_filter.py  (threshold={MIN_SCORE:.0%})\n")
            f.write("#\n")
            f.write("# -- All-rejected (had images, none passed) --\n")
            for name in sorted(empty_families):
                f.write(f"ALL_REJECTED  {name}\n")
            f.write("#\n")
            f.write("# -- Pre-existing empty folders (deleted) --\n")
            for name in sorted(removed_families):
                f.write(f"WAS_EMPTY     {name}\n")

    # ── Write machine-readable JSON summary ───────────────────────────────────
    summary = {
        "device": device_label,
        "threshold": MIN_SCORE,
        "total_processed": kept + rejected,
        "kept": kept,
        "rejected": rejected,
        "pass_rate": round(kept / (kept + rejected) * 100, 2) if (kept + rejected) else 0,
        "families": {
            name: {
                "kept": s["kept"],
                "dropped": s["dropped"],
                "avg_score": round(sum(s["scores"]) / len(s["scores"]), 4) if s["scores"] else 0,
                "top_score": round(max(s["scores"]), 4) if s["scores"] else 0,
            }
            for name, s in family_stats.items()
        },
        "empty_after_filter": sorted(empty_families),
        "pre_existing_empty": sorted(removed_families),
    }
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # ── Print human-readable summary ──────────────────────────────────────────
    print_summary(family_stats, kept, rejected, empty_families, removed_families, device_label)