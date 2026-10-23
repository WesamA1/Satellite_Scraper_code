# flatten_images.py
# Copies every satellite image into one flat folder (originals are untouched).
#
#   python flatten_images.py                  # copies into flat_images/
#   python flatten_images.py --dest my_dir    # custom destination
#
# Files are named <FAMILY>__<original_name> to avoid collisions, and a
# flat_images_manifest.csv maps every copy back to its source path.

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT      = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "satellite_images"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}


def main():
    parser = argparse.ArgumentParser(description="Copy all images into one flat folder.")
    parser.add_argument("--dest", default="flat_images",
                        help="destination folder (default: flat_images)")
    args = parser.parse_args()

    dest_dir = ROOT / args.dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dest_dir / "flat_images_manifest.csv"

    if not IMAGE_DIR.exists():
        print(f"Image directory not found: {IMAGE_DIR}")
        return 1

    copied  = 0
    skipped = 0
    rows    = []

    for family_dir in sorted(IMAGE_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        for f in sorted(family_dir.iterdir()):
            if f.suffix.lower() not in IMAGE_EXTENSIONS or f.name.startswith("._"):
                continue

            flat_name = f"{family_dir.name}__{f.name}"
            dest      = dest_dir / flat_name

            # Same flat name from a previous run with identical size — skip
            if dest.exists() and dest.stat().st_size == f.stat().st_size:
                skipped += 1
                rows.append({"flat_name": flat_name, "family": family_dir.name,
                             "source_path": str(f.relative_to(ROOT))})
                continue

            shutil.copy2(f, dest)
            copied += 1
            rows.append({"flat_name": flat_name, "family": family_dir.name,
                         "source_path": str(f.relative_to(ROOT))})

    with open(manifest_path, "w", newline="", encoding="utf-8") as mf:
        writer = csv.DictWriter(mf, fieldnames=["flat_name", "family", "source_path"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Copied {copied} images ({skipped} already present) -> {dest_dir}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
