# coverage_report.py
# Shows how well the image dataset covers the families in satellites.csv:
# per-family image counts split by source, which sources were tried but found
# nothing, and which sources haven't been attempted yet.
#
#   python coverage_report.py             # console summary + coverage_report.csv
#   python coverage_report.py --zeros     # also list every zero-image family

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT      = Path(__file__).resolve().parent
IMAGE_DIR = ROOT / "satellite_images"
OUT_CSV   = ROOT / "coverage_report.csv"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".gif"}

# source name -> (filename tag, no-images log file)
# gunters has no filename tag (legacy naming: FAMILY_01.jpg), so any image
# without another source's tag is attributed to it.
SOURCES = {
    "wikimedia": ("_wikimedia_", "no_images_found.txt"),
    "gunters":   (None,          "no_images_found_gunters.txt"),
    "nasa":      ("_nasa_",      "no_images_found_nasa.txt"),
    "satnogs":   ("_satnogs_",   "no_images_found_satnogs.txt"),
    "wikipedia": ("_wikipedia_", "no_images_found_wikipedia.txt"),
    "wikidata":  ("_wikidata_",  "no_images_found_wikidata.txt"),
    "flickr":    ("_flickr_",    "no_images_found_flickr.txt"),
}

TAGGED_SOURCES = [(name, tag) for name, (tag, _) in SOURCES.items() if tag]


def classify_source(filename):
    for name, tag in TAGGED_SOURCES:
        if tag in filename:
            return name
    return "gunters"


def load_families():
    families = set()
    with open(ROOT / "satellites.csv", newline="") as f:
        for row in csv.DictReader(f):
            name = row["FAMILY_NAME"].strip()
            if name:
                families.add(name)
    return sorted(families)


def load_tried():
    """family -> set of sources that ran and found nothing."""
    tried = defaultdict(set)
    for source, (_, log_name) in SOURCES.items():
        if not log_name:
            continue
        log = ROOT / log_name
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line:
                    tried[line].add(source)
    return tried


def count_images():
    """family -> {source: count}"""
    counts = defaultdict(lambda: defaultdict(int))
    if not IMAGE_DIR.exists():
        return counts
    for family_dir in IMAGE_DIR.iterdir():
        if not family_dir.is_dir():
            continue
        for f in family_dir.iterdir():
            if f.suffix.lower() in IMAGE_EXTENSIONS and not f.name.startswith("._"):
                counts[family_dir.name][classify_source(f.name)] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description="Dataset coverage report.")
    parser.add_argument("--zeros", action="store_true",
                        help="list every family with zero images")
    args = parser.parse_args()

    families = load_families()
    tried    = load_tried()
    counts   = count_images()

    source_names = list(SOURCES)
    rows = []
    for family in families:
        per_source = counts.get(family, {})
        total = sum(per_source.values())
        no_result = tried.get(family, set())
        untried = [s for s in source_names
                   if s not in no_result and not per_source.get(s)]
        rows.append({
            "family": family,
            "total": total,
            **{s: per_source.get(s, 0) for s in source_names},
            "tried_no_result": ";".join(sorted(no_result)),
            "untried_sources": ";".join(untried),
        })

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["family", "total", *source_names,
                           "tried_no_result", "untried_sources"])
        writer.writeheader()
        writer.writerows(rows)

    # ── Console summary ───────────────────────────────────────────────────
    total_images  = sum(r["total"] for r in rows)
    covered       = [r for r in rows if r["total"] > 0]
    zeros         = [r for r in rows if r["total"] == 0]
    fully_tried   = [r for r in zeros if not r["untried_sources"]]

    print("=" * 62)
    print("  COVERAGE REPORT")
    print("=" * 62)
    print(f"  Families in satellites.csv : {len(families)}")
    print(f"  Families with >=1 image    : {len(covered)}  "
          f"({len(covered) / len(families) * 100:.1f}%)")
    print(f"  Families with 0 images     : {len(zeros)}")
    print(f"    ...all sources exhausted : {len(fully_tried)}")
    print(f"  Total images               : {total_images}")
    print()

    print(f"  {'Source':<12} {'Images':>8} {'Families':>9}")
    print(f"  {'-'*12} {'-'*8} {'-'*9}")
    for s in source_names:
        n_img = sum(r[s] for r in rows)
        n_fam = sum(1 for r in rows if r[s] > 0)
        print(f"  {s:<12} {n_img:>8} {n_fam:>9}")
    print()

    bands = [("1 image", 1, 1), ("2-5", 2, 5), ("6-20", 6, 20),
             ("21-50", 21, 50), ("51+", 51, 10**9)]
    print("  Images-per-family distribution (covered families):")
    for label, lo, hi in bands:
        n = sum(1 for r in covered if lo <= r["total"] <= hi)
        print(f"    {label:<10} {n:>5}  {'#' * (n // 10)}")
    print()

    if args.zeros:
        print(f"  Zero-image families ({len(zeros)}):")
        for r in zeros:
            untried = r["untried_sources"] or "none left to try"
            print(f"    {r['family']:<35} untried: {untried}")
        print()

    print(f"  Full table: {OUT_CSV.name}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
