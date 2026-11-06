# Satellite Image Scraper -- Ground Station

Builds an ML training dataset of satellite images, organized by satellite
family (~1,600 families from the Space-Track catalog), with license
provenance enforced at the data layer -- every image is backed by a
machine-readable open license (Wikimedia Commons CC, NASA public domain) or
it never gets a database record at all.

The active code lives in `scraper/` (CLI + web GUI, DB-backed). `reference/`
holds the pre-3.0 pipeline this was rewritten from -- kept read-only, for
porting logic from, not for running.

## Setup on a new machine

**Recommended: [uv](https://docs.astral.sh/uv/)**

```bash
uv sync --all-groups
```

This reads `pyproject.toml`/`uv.lock` and creates `.venv` with every runtime
and dev dependency pinned to the exact versions CI runs against.

**Fallback: plain `pip`**

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     (Windows)
.venv/bin/python -m pip install -r requirements.txt             (macOS/Linux)
```

Python 3.10+ (developed on 3.14). On Ubuntu/Linux, `python3-tk` is needed only
if you're also running the legacy `reference/review_gui.py`/`main.py` (not the
new CLI or web GUI, which don't use tkinter): `sudo apt install python3-tk`.

## Credentials

Copy `.env.example` to `.env` and fill in:

```
SPACETRACK_USERNAME=
SPACETRACK_PASSWORD=
WIKIMEDIA_BOT_USERNAME=
WIKIMEDIA_BOT_PASSWORD=
```

Never commit `.env` (it's gitignored) and never hardcode these in source --
both `scraper/` and `reference/` read them via `python-dotenv`.

Optional, for Task 8's finish/crash notifications -- set one or both in `.env`:
`DISCORD_WEBHOOK_URL`, `NTFY_URL`. Neither is required; notification is a
silent no-op if unset.

Optional, for Phase 2's source discovery/codegen (below) -- `ANTHROPIC_API_KEY`,
a normal Console API key (billed per-use). This is **not** a claude.ai/Claude
Code subscription login -- Anthropic's terms prohibit third-party apps from
offering that, and this project doesn't. Core scraping works with none of this
section set.

Optional, for the attribute model's default image model (DINOv3, gated on
Hugging Face) -- `HF_TOKEN`, a read token from an account that has accepted
DINOv3's terms. Running `hf auth login` once on the machine works too.

## Running the pipeline

### CLI

```bash
.venv/bin/python -m scraper.cli --help
```

| Command        | Does                                                          |
|----------------|----------------------------------------------------------------|
| `scrape`       | Search + license-gate + download. `--family`, `--source`, `--limit`, `--profile smoke\|full`, `--dry-run`, `--dev` (cached session, local testing only) |
| `filter`       | CLIP-based auto-filter on already-downloaded images. `--family`, `--min-score`, `--dry-run` |
| `embed`        | Embeds active + training-set images with a frozen image model (`--model dinov3-vitb16\|siglip2-base`); skips ones already done |
| `export`       | Writes `export/manifest.csv` (license-complete record per active image) |
| `status`       | Per-family done/pending/failed + zero-image families          |
| `doctor`       | GPU/credentials/API-reachability/disk/package checks, pass/fail table |
| `retry-failed` | Clears the negative-cache for `--family`/`--source` and re-scrapes them |
| `discover`     | Phase 2: finds candidate image sources for `--prompt` via Claude's web search |
| `approve` / `reject` | Phase 2: review a candidate from `discover`                   |
| `codegen`      | Phase 2: drafts an integration module for an approved candidate -- writes a file, never enables it |
| `promote`      | Phase 2: copies a reviewed module into `scraper/sources/` -- **read the code yourself first** |
| `sources`      | Lists registered sources and discovered candidates             |
| `import-annotations` | Brings the old annotation tool's CSV into the database. `--dry-run`, `--annotator` |
| `build-training-set` | Labeled examples for the attribute model. `--csv NAME=PATH` (repeatable, one per annotator), `--images DIR` (repeatable) |
| `train-heads`  | Trains + scores the attribute model on held-out satellite families. `--model` |
| `train`        | All three training steps in one go, from the `data/` folder. `--data`, `--model` |
| `prefill`      | Saves the trained model's guesses for every image waiting for annotation. `--include-annotated`, `--redo` |
| `import-data`  | Adds `data/`'s images + answers to the database; identical scraped copies merge into them. `--dry-run` |
| `serve`        | Launches the web GUI (below)                                   |

Every command is resume-safe -- re-running never re-downloads or re-fetches
anything already in `scraper.db`. Ctrl+C during `scrape` lets each lane finish
its current family, then stops cleanly; re-running the same command picks up
where it left off.

`scrape` runs each site in its own **lane**, all side by side, so a full run
takes about as long as the slowest site rather than every site added up. Each
lane still makes one request at a time with the usual pacing and backoffs.
Wikimedia, Wikidata and the Wikipedias share one lane, since they share the
bot account's rate limit. A site that errors waits two minutes and tries that
family once more, then moves on; after five failures in a row its lane gives
up and the rest carry on (the next run tries it again). The Scrape page shows
one row per lane, and each run's summary on the Status page records time
spent per source.

`scrape --family` is really "search prompt" -- it was never validated against
the satellite catalog, so any text works. `--domain-hint satellite` (the
default) gets appended to the query and biases relevance filtering toward
satellite-shaped results; pass `--domain-hint ""` for a fully generic prompt.

### Web GUI

**One-click launch:** double-click `launch_gui.bat` (Windows), `launch_gui.command`
(macOS), or run `./launch_gui.sh` (Linux/Ubuntu -- or double-click it, if your
file manager has "allow executing as program" set). No terminal, no typing an
IP -- it starts the server and opens your browser straight to it.

**From a terminal:**

```bash
.venv/bin/python -m scraper.cli serve --host 0.0.0.0 --port 8000
```

Your browser opens automatically once the server is up (pass `--no-browser` to
skip that, e.g. on a headless box). You land on a title screen first, then a
real multi-page app: Scrape/Filter/Export (the pipeline), Review
(duplicate detection + per-image variant labeling), Annotate/Attributes/Train
model/Data (labeling, the attribute model, and importing/exporting annotations, below), Status/Doctor (monitor),
and Discovery/API Keys (sources) -- live log + progress streaming over SSE and
a stop button for in-progress scrapes. `--host 0.0.0.0` makes it reachable
from another device on the same network (e.g. checking progress from a phone
while it runs headless on an Ubuntu scraping box); the default `127.0.0.1`
keeps it local-only. The app has no authentication of its own, so treat
`--host 0.0.0.0` as "anyone on this network can trigger scrapes" -- that's why
API key writes are only ever accepted from a request that's actually local.

## Annotating and the attribute model

**Annotate** labels each image's visible attributes, one category at a time;
**Attributes** edits the attribute list (`attributes.json`). The Annotate
page's Setup box picks where images come from:

- **Scraper database** -- reviewed images, which stay where they are.
- **Old tool folders** -- the old desktop tool's image folder, annotated
  folder and CSV, used exactly as it did: each submit adds a CSV row and
  moves the image. Point "Fill in from old tool" at the folder it ran from.

Every attribute has an **Unknown** button for "can't tell from the image";
"None" means "looked, and it isn't there".

**Train model** (or the commands) trains the first-pass model that will
pre-fill annotations:

1. **Training set** -- old-tool CSVs (one per annotator) plus their image
   folders, and Ground Station's own annotations. Output goes in `training/`
   (gitignored).
2. **Embed** -- runs the images through DINOv3 (default) or SigLIP 2.
3. **Train and score** -- one classifier per attribute, scored on satellite
   families it never saw, against an always-the-most-common-answer baseline.
4. **Pre-fill guesses** -- works out the model's guesses for every image
   waiting for annotation in one batch. With **Auto-fill every image** ticked
   on the Annotate page, each image then opens already filled in, without
   waiting for the image model. Guesses are never submitted; annotators check
   every field. A retrained model needs a new pre-fill run (images that
   already have the current model's guesses are skipped).

On the Windows GPU machine, install `torchvision` from the same CUDA index as
torch (`pip install torchvision --index-url https://download.pytorch.org/whl/cu128`),
or pip may swap in a CPU build.

### The `data/` folder

Annotated images and their CSVs live in `data/` (gitignored -- it's copied
between machines, never committed):

```
data/
  annotations.csv            one CSV (the old tool's annotatedInfo.csv), or...
  annotations/alice.csv      ...one per annotator, named by the file
  images/                    every image the CSVs name (subfolders are fine)
```

`build-training-set`, `train` and the Train model page all use it by default.
`import-data` (or Import on the Data page) adds it to the
database as well, so its images share one pool with scraped ones: when a
scraped image is the same file as an imported one, the imported copy is kept
(with the scraped copy's license record) and the scraped copy is
quarantined. Files in `data/` are never moved.

### Training on a bigger machine

Training doesn't need the scraping setup -- just the code and `data/`:

1. `git clone` the repo and `uv sync --all-groups` (on the GPU box, install
   torch and torchvision from the CUDA index first).
2. Copy `data/` into the project folder, or pass `--data` to point at it.
3. `hf auth login` once, for DINOv3.
4. `uv run python -m scraper.cli train` -- builds the training set, embeds
   it, and trains and scores the heads, printing the scores.
5. Copy `training/heads_<model>.pt` (the trained model, a few hundred KB)
   back to the machine that annotates.

The training set stores image paths relative to the project and each image's
SHA-256, so it lines up with the images wherever the folder is copied.
Embeddings are kept per machine (in its `scraper.db`) and are rebuilt there.

## Adding a new source (Phase 2)

1. **Discover**: `scraper discover --prompt "..."` (or the Discovery card)
   uses Claude's web search to propose candidates, each with an assessment of
   whether it exposes *machine-readable, per-image* license data -- the only
   thing that actually matters here, not a general "images are free" claim.
2. **Review**: `scraper sources --pending` / `approve <id>` / `reject <id>`
   (or the GUI's Approve/Reject buttons).
3. **Generate**: `scraper codegen <id>` drafts an integration module matching
   `scraper/sources/base.py`'s contract, written to
   `scraper/sources/candidates/<slug>.py` (gitignored -- a draft, not yet
   real). Nothing is imported or run.
4. **Read it.** This step doesn't have a command because it's the point:
   open the file and actually read what was generated, especially its
   license-extraction logic.
5. **Promote**: `scraper promote <id>` checks the module has the required
   shape (not correctness -- just that the functions exist), copies it into
   `scraper/sources/`, and registers it in `enabled_sources.yaml`. Restart
   `scrape`/`serve` for it to actually be usable as a `--source`.

## Dev tooling

- **Lint**: `.venv/bin/ruff check scraper/ tests/`
- **Pre-commit**: `.venv/bin/pre-commit install` once per checkout, then ruff
  runs automatically on `git commit`.
- **Tests**: `.venv/bin/pytest tests/ -v` (or `uv run pytest`) -- all offline, no network/GPU
  required (CI runs these on every push/PR via `.github/workflows/ci.yml`).
- **`--dev` flag** on `scrape`: caches HTTP responses locally
  (`requests-cache`) for faster iteration while developing. Never use this for
  a real scrape -- a cached response masking real API/rate-limit state cuts
  against being polite to the APIs we depend on.

## Legacy pipeline (`reference/`, pre-3.0)

Kept for porting logic from -- do not run this for real data collection; use
the CLI/web GUI above instead. `main.py` was the old control panel:

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

`sources/gunters_space.py` (repo root, not `reference/`) is never run, ported,
or read by anything -- left in place untouched per project policy.
