# Satellite Image Scraper

Builds an image dataset of satellite hardware covering ~1,600 satellite
families (from the Space-Track catalog), scraped from seven public sources,
auto-filtered with CLIP, and manually reviewable with a swipe GUI.

## Setup on a new machine

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     (Windows)
.venv/bin/python -m pip install -r requirements.txt             (macOS/Linux)
```

Python 3.10+ (developed on 3.14). tkinter must be available (bundled with
the python.org installer on Windows/macOS; on Ubuntu/Linux run
`sudo apt install python3-tk` first — it isn't installed via pip).

## Run everything from the control panel

```
.venv\Scripts\python.exe main.py
```

The panel runs the whole workflow in order, shows per-step last-run/stale
status, live dataset stats, and streams each tool's output:

| # | Step            | Script                   | Output                        |
|---|-----------------|--------------------------|-------------------------------|
| 1 | Fetch catalog   | satellite_scraper.py     | satellites.csv / .json        |
| 2 | Assign variants | variants.py              | VARIANT_NAME column           |
| 3 | Scrape images   | image_pipeline.py        | satellite_images/<FAMILY>/    |
| 4 | CLIP filter     | sources/clip_filter.py   | moves junk to rejected_images/|
| 5 | Manual review   | review_gui.py            | manual_review_log.csv         |
| 6 | Coverage report | coverage_report.py       | coverage_report.csv           |
| 7 | Build labels    | build_labels.py          | image_labels.csv              |
| 8 | Flatten images  | flatten_images.py        | flat_images/                  |

Every script also runs standalone with the same venv Python.

## Credentials

- **Space-Track** (step 1 only): username/password at the top of
  satellite_scraper.py. Included satellites.csv/json mean you can skip
  step 1 until you want a catalog refresh.
- **Wikimedia** (wikimedia source): bot credentials at the top of
  sources/wikimedia.py.
- **Flickr** (flickr source, optional): free API key via the FLICKR_API_KEY
  environment variable, a .flickr_api_key file, or the field in main.py's
  panel. The pipeline skips flickr automatically when no key is set.

## Notes

- All scrapers are resume-safe: rerunning skips already-downloaded families
  (no_images_found_*.txt record dead ends).
- review_gui.py: right/left arrows = keep/reject, D = duplicate,
  Z = undo, Q = quit. Suspected duplicates (perceptual hash) show an amber
  banner with comparison thumbnails; --dupes-first reviews them first.
- Rejects and duplicates are moved (never deleted) to rejected_images/ and
  duplicate_images/ so any decision can be undone.
