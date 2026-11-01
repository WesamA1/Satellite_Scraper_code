# Ground Station, Phase 1: Foundation

Goal: make the pipeline easy to run, observe, and resume, and make license provenance enforced at the data layer. No new sources and no ML changes in this phase.

Gate to finish Phase 1: **the license gate is live on every record.**

## Context
This is a top-down rewrite starting from an older copy of the code. There is no existing database to migrate: design the schema fresh. Port proven logic from the newer version (family extraction, Wikimedia fixes, `nasa.py`, GPU smoke test) rather than rewriting it from memory.

## Tasks

### 0. Clean base and repo setup
- Leave `sources/gunters_space.py`, `gunters_sitemap_cache.xml`, `no_images_found_gunters.txt`, and the images it downloaded untouched for now (no moving, renaming, editing, deleting, or gitignoring). Confirm nothing in `scraper/` imports or reads them.
- Move hardcoded credentials out of `satellite_scraper.py` (Space-Track) and `sources/wikimedia.py` (Wikimedia bot) into `.env`, loaded with `python-dotenv`. Create `.env.example` with the same keys and blank values. Do this before the first commit.
- Create the `.gitignore` (replace any existing one) and show it to the user before committing. It must cover: `.env`, `.venv/`, `__pycache__/`, `*.pyc`, `.DS_Store`, image folders (`satellite_images/`, `rejected_images/`), database files, caches, and run logs. It must NOT ignore the Gunter's files (`sources/gunters_space.py`, `gunters_sitemap_cache.xml`, `no_images_found_gunters.txt`).
- `git init`, then before the first commit confirm with `git status` that `.env` and image folders are excluded and no credentials appear in tracked files.
- Lay out the new package structure (e.g. `scraper/` with `sources/`, `stages/`, `db/`, `cli.py`) before porting code into it.

### 1. Separate stages
Split the pipeline into independent stages that read and write through the database: `download`, `filter`, `embed` (stub for now), `export`.
- Re-running `filter` must never re-download images.
- Acceptance: changing filter settings and re-running touches zero network calls.

### 2. Design the SQLite schema
Tables: `images` (path, hash, dimensions, duplicate group placeholder), `sources` (source module, URL, retrieval date), `licenses` (license, author, attribution string), `predictions` (attribute, value, confidence, model version), `annotations` (attribute, value, annotator, guide version), `rate_limits` (host, cooldown until, last Retry-After).
- Include a schema version table so future changes can migrate cleanly.
- Acceptance: a run can be interrupted and resumed with no duplicate downloads.

### 3. License gate
- Capture full Commons `extmetadata` in `wikimedia.py` (license short name, license URL, artist/author, attribution required, credit, retrieval date).
- Capture equivalent fields in `nasa.py`.
- Any record without a machine-readable open license is rejected and logged with reason `no_license`.
- Exception: `gunters_space` may store images with the explicit license status `Unverified (no license metadata provided)`. This is not a license claim; source activation remains controlled by its `ENABLED` flag.
- Acceptance: a query for records with null license returns zero rows; rejection counts appear in the run report.

### 4. Persistent rate limit state
- Store per-host cooldown-until timestamps and last `Retry-After` in SQLite.
- On startup, respect any active cooldown instead of hitting the host immediately.
- Acceptance: stopping and restarting during a throttle window does not send requests to that host until the cooldown ends.

### 5. CLI
Use Typer (or Click). Subcommands: `scrape`, `filter`, `embed`, `export`, `status`, `doctor`, `retry-failed`.
Common flags: `--family`, `--source`, `--limit`, `--dry-run`, `--profile`.
- Profiles in YAML: `smoke` (5 families, 10 images each) and `full`.

### 6. `doctor` command
Checks: GPU via the existing smoke-test tensor op, credentials present (Space-Track, Wikimedia bot), API reachability, free disk space, key package versions. Prints a pass/fail table.

### 7. `status` command and run report
- Per family: done, pending, failed, image count. List families with zero images.
- Run report after each run: images added per source, rejections by reason.

### 8. Long-run quality of life
- Graceful Ctrl+C: finish the current item, checkpoint, exit cleanly.
- Progress bars with ETA per source (Rich or tqdm); detailed logs to a file.
- Optional finish/crash notification via Discord webhook or ntfy (URL from config).
- `retry-failed` reprocesses only errored items.

### 9. Dev tooling
- `requests-cache` enabled only in a dev profile.
- uv with a lockfile; Ruff plus pre-commit; CI running the offline fixture tests.
- Credentials via keyring or `.env`.

## Suggested order
0 (clean base) first, then 2 (schema) and 1 (stages), then 3 (license gate), 4 (rate limits), 5 to 7 (CLI, doctor, status), then 8 and 9.

## Out of scope for Phase 1
New sources, Openverse, Wikidata, DINOv3 or any model swap, annotation tooling.
