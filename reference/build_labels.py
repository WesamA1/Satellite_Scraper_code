# build_labels.py
# Builds image_labels.csv — one row per image with two-tier labels:
#
#   label_level = "variant" : the image maps to a specific satellite (its
#                             NORAD ID was recorded at download time by a
#                             NORAD-keyed source: satnogs / wikidata), so it
#                             gets that satellite's VARIANT_NAME.
#   label_level = "family"  : keyword-sourced image (wikimedia / wikipedia /
#                             nasa / gunters / flickr) — the picture can't be
#                             tied to one satellite, so the label stays at
#                             family granularity.
#
# Columns: path, family, source, norad_id, variant, label_level
#
#   python build_labels.py

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT      = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "satellite_images"
OUT_CSV   = ROOT / "image_labels.csv"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}

SOURCE_TAGS = ["wikimedia", "wikipedia", "wikidata", "satnogs", "nasa", "flickr", "render"]


def classify_source(filename):
    for tag in SOURCE_TAGS:
        if f"_{tag}_" in filename:
            return tag
    return "gunters"


def load_variant_table():
    """norad -> (family, variant) from satellites.csv."""
    table = {}
    with open(ROOT / "satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            try:
                norad = int(row["NORAD_CAT_ID"])
            except (ValueError, KeyError):
                continue
            table[norad] = (row["FAMILY_NAME"].strip(),
                            row.get("VARIANT_NAME", "").strip() or row["FAMILY_NAME"].strip())
    return table


def parse_metadata_norads(family_dir):
    """filename -> norad_id, parsed from every metadata_*.txt in the folder.

    Metadata files are blocks of 'key: value' lines separated by blank lines;
    NORAD-keyed sources (satnogs, wikidata) write a norad_id line per file.
    """
    norads = {}
    for meta in family_dir.glob("metadata_*.txt"):
        current_file = None
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                current_file = None if not line.strip() else current_file
                continue
            key, value = line.split(":", 1)
            key, value = key.strip().lower(), value.strip()
            if key == "file":
                current_file = value
            elif key == "norad_id" and current_file:
                try:
                    norads[current_file] = int(value)
                except ValueError:
                    pass
    return norads


def main():
    variant_table = load_variant_table()
    print(f"Variant table: {len(variant_table)} satellites")

    rows = []
    stats = Counter()
    variant_images = defaultdict(Counter)  # family -> variant -> images

    for family_dir in sorted(IMAGE_DIR.iterdir()):
        if not family_dir.is_dir():
            continue
        family = family_dir.name
        norads = parse_metadata_norads(family_dir)

        for f in sorted(family_dir.iterdir()):
            if f.suffix.lower() not in IMAGE_EXTENSIONS or f.name.startswith("._"):
                continue

            source = classify_source(f.name)
            norad  = norads.get(f.name)
            if norad is not None and norad in variant_table:
                _, variant = variant_table[norad]
                level = "variant"
            else:
                norad, variant, level = None, family, "family"

            rows.append({
                "path":        str(f.relative_to(ROOT)),
                "family":      family,
                "source":      source,
                "norad_id":    norad if norad is not None else "",
                "variant":     variant,
                "label_level": level,
            })
            stats[level] += 1
            if level == "variant" and variant != family:
                variant_images[family][variant] += 1

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["path", "family", "source", "norad_id", "variant", "label_level"])
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    print(f"\nWrote {OUT_CSV.name}: {total} images")
    print(f"  variant-level labels : {stats['variant']}"
          f"  ({stats['variant'] / total * 100:.1f}%)")
    print(f"  family-level labels  : {stats['family']}")

    if variant_images:
        print("\nVariant-labeled images in split families:")
        for family in sorted(variant_images):
            print(f"  {family}")
            for variant, n in variant_images[family].most_common():
                print(f"    {variant:<24} {n:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
