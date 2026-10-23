# image_pipeline.py
# Runs every image source over all families in satellites.csv.
#
#   python image_pipeline.py                     # run all sources
#   python image_pipeline.py --only nasa,satnogs # run a subset
#   python image_pipeline.py --sequential        # one source at a time
#   python image_pipeline.py --list              # show available sources
#
# Each source is launched as its own subprocess (the standalone runners in
# sources/ already handle resume/skip logic, metadata, and rate limiting),
# with output prefixed by source name.
#
# Sources that hit different servers run in parallel. wikimedia and wikipedia
# both download from the upload.wikimedia.org CDN, so they share one
# sequential "lane" to avoid tripping its throttle twice as fast.

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Child scripts print ✓/✗/— which the default cp1252 stdout on Windows can't
# encode (crashes the printer thread when output is piped or redirected).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent
IMAGES_DIR  = PROJECT_DIR / "satellite_images"
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Lanes run in parallel; sources within a lane run sequentially.
# wikimedia, wikipedia and wikidata all download from the upload.wikimedia.org
# CDN, so they share one sequential lane to avoid tripping its throttle.
LANES = [
    ["satnogs"],
    ["nasa_images"],
    ["gunters_space"],
    ["flickr"],
    ["wikimedia", "wikipedia", "wikidata"],
]

ALL_SOURCES = [src for lane in LANES for src in lane]

# Short aliases accepted by --only
ALIASES = {"nasa": "nasa_images", "gunters": "gunters_space"}


def flickr_key_available():
    return bool(os.environ.get("FLICKR_API_KEY", "").strip()
                or (PROJECT_DIR / ".flickr_api_key").exists())

_print_lock = threading.Lock()


def count_images():
    if not IMAGES_DIR.exists():
        return 0
    return sum(
        1 for p in IMAGES_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        and not p.name.startswith("._")
    )


def run_source(name, results):
    """Run one source script as a subprocess, prefixing its output."""
    script = PROJECT_DIR / "sources" / f"{name}.py"
    with _print_lock:
        print(f"[{name}] starting...")

    proc = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    results[name] = {"proc": proc, "returncode": None}

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                with _print_lock:
                    print(f"[{name}] {line}")
        proc.wait()
    except Exception as e:
        # Never let a printer failure orphan the child process
        proc.terminate()
        proc.wait()
        with _print_lock:
            print(f"[{name}] pipeline error: {e}")

    results[name]["returncode"] = proc.returncode
    with _print_lock:
        status = "done" if proc.returncode == 0 else f"exited with code {proc.returncode}"
        print(f"[{name}] {status}")


def run_lane(lane, results):
    for name in lane:
        run_source(name, results)


def main():
    parser = argparse.ArgumentParser(description="Run all satellite image sources.")
    parser.add_argument("--only", help="comma-separated subset of sources to run")
    parser.add_argument("--sequential", action="store_true",
                        help="run everything one source at a time")
    parser.add_argument("--list", action="store_true", help="list available sources")
    args = parser.parse_args()

    if args.list:
        print("Available sources:", ", ".join(ALL_SOURCES))
        print("Aliases:", ", ".join(f"{k}={v}" for k, v in ALIASES.items()))
        return 0

    if not (PROJECT_DIR / "satellites.csv").exists():
        print("satellites.csv not found — run satellite_scraper.py first.")
        return 1

    selected = ALL_SOURCES
    if args.only:
        selected = []
        for raw in args.only.split(","):
            name = raw.strip().lower()
            name = ALIASES.get(name, name)
            if name not in ALL_SOURCES:
                print(f"Unknown source '{raw.strip()}'. Available: {', '.join(ALL_SOURCES)}")
                return 1
            selected.append(name)

    if "flickr" in selected and not flickr_key_available():
        print("Skipping flickr — no API key (set FLICKR_API_KEY or create "
              ".flickr_api_key; see sources/flickr.py header).")
        selected = [s for s in selected if s != "flickr"]
        if not selected:
            return 1

    if args.sequential:
        lanes = [[s] for s in selected]
        parallel = False
    else:
        lanes = [[s for s in lane if s in selected] for lane in LANES]
        lanes = [lane for lane in lanes if lane]
        parallel = len(lanes) > 1

    before = count_images()
    print(f"Sources: {', '.join(selected)}")
    print(f"Mode: {'parallel lanes' if parallel else 'sequential'}")
    print(f"Images on disk before run: {before}\n")

    start   = time.time()
    results = {}

    if parallel:
        threads = [
            threading.Thread(target=run_lane, args=(lane, results), daemon=True)
            for lane in lanes
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(timeout=1)
        except KeyboardInterrupt:
            print("\nInterrupted — stopping all sources (progress is saved, "
                  "rerun to resume)...")
            for info in results.values():
                if info["returncode"] is None:
                    info["proc"].terminate()
            return 130
    else:
        try:
            for lane in lanes:
                run_lane(lane, results)
        except KeyboardInterrupt:
            print("\nInterrupted — progress is saved, rerun to resume.")
            for info in results.values():
                if info["returncode"] is None:
                    info["proc"].terminate()
            return 130

    after   = count_images()
    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"Finished in {elapsed / 60:.1f} min")
    print(f"Images on disk: {before} -> {after}  (+{after - before})")
    for name in selected:
        code = results.get(name, {}).get("returncode")
        print(f"  {name}: {'ok' if code == 0 else f'exit code {code}'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
