# Satellite Image Pipeline (Ground Station)

## What this project is
A Python pipeline that builds an ML training dataset of satellite images, organized by satellite family (~1,628 families derived from Space-Track data). Images are later annotated by humans on visual attributes and handed to a separate recognition model owned by another team. Identifying specific satellites is NOT in scope here.

Ground Station is a top-down rewrite. The current phase brief is `docs/scraper3-phase1.md`. Read it before starting work.

## Non-negotiable rules
- **Licensing is a human call, not a runtime gate.** A human decides whether a candidate source's licensing is trustworthy during Discovery review — reading the generated source code (and, where useful, visiting the site itself) before promoting it. `scraper/license_gate.py` does not reject records at scrape time; `license_short_name`/`license_url`/etc. are still recorded as-is (empty included) for provenance in the licenses table and export manifest, they just don't block ingestion. Source modules should still write real, honest per-item data when it exists, never a guessed/hallucinated value — the human review this depends on needs that field to be truthful.
- **No general web scraping** (Google Images, Bing, arbitrary sites). License provenance there is unreliable.
- **Be polite to APIs.** Wikimedia CDN throttle windows last minutes. Use long backoffs, honor `Retry-After`, never shorten sleeps or add rapid retry loops.
- **One machine scrapes at a time.** The Windows desktop, the MacBook, and the Ubuntu scraping machines share one Wikimedia bot account and its rate limit.
- **No credentials in code or git.** Read them from `.env` (gitignored) via `python-dotenv`. Keys: `SPACETRACK_USERNAME`, `SPACETRACK_PASSWORD`, `WIKIMEDIA_BOT_USERNAME`, `WIKIMEDIA_BOT_PASSWORD`. `.env.example` lists them with blank values. If you find a hardcoded credential, move it to `.env` before any commit.
- Ask before deleting files, images, or data, or changing the database schema destructively.

## Machines
- **Windows home desktop (primary dev and scraping):** Python 3.14, NVIDIA RTX 5070 (Blackwell, 12 GB VRAM). Install PyTorch from the CUDA 12.8 (or newer) wheel index; older CUDA builds lack Blackwell kernels. 12 GB is plenty for ingest-time embedding and attribute-head training; keep VLM fine-tuning on the lab box. Shell is PowerShell; venv activation is `.venv\Scripts\Activate.ps1`.
- **MacBook (Apple M4, testing):** PyTorch uses MPS. If a dependency lacks Python 3.14 wheels on macOS, pin 3.12 or 3.13 via uv.
- **Linux lab GPU box (training only):** VLM fine-tuning and heavy training happen here, never on the laptops.
- **Ubuntu scraping machines (additional hosts, separate from the lab GPU box):** Shell is bash; venv activation is `source .venv/bin/activate`. `python3-tk` must be installed via apt for `review_gui.py`/`main.py` (tkinter isn't pip-installable on Linux). GPU presence varies by machine — run the same empirical CUDA smoke test; falls back to CPU if no NVIDIA GPU is present.
- Code must run on all of these. Use `pathlib.Path` everywhere; no hardcoded separators or drive letters.
- GPU checks must be empirical: `torch.cuda.is_available()` can return True when kernels fail on Blackwell, and MPS can be missing ops. Run a smoke-test tensor op per backend. Device priority: CUDA, then MPS, then DirectML, then CPU.

## Repository layout
- `scraper/`: new 3.0 package (`sources/`, `stages/`, `db/`, `cli.py`). All new code goes here.
- `reference/`: the newer pre-3.0 code, kept read-only for porting. Do not import from it or edit it.
- `docs/`: phase briefs and design notes.
- `tests/`: offline tests against fixture data.
- Images, databases, logs, and `_quarantine/` live on disk and are gitignored.

## Port from `reference/`, don't reinvent
These pieces took real debugging. Port their logic into `scraper/` instead of rewriting from scratch:
- `satellite_scraper.py`: `get_family_name()` uses three stages (parenthetical hints, manual overrides, suffix stripping). Do not simplify to naive regex; it inflates family counts.
- `sources/base.py`: `ImageSource` abstract base class and `ImageRecord` dataclass (does not exist yet in `reference/` — design fresh in `scraper/`, following the patterns of the other sources below).
- `sources/wikimedia.py`: bot-password auth, rate limiting with long backoffs, resume, negative cache, `get_search_term()`. Must send a descriptive User-Agent or Wikimedia returns 403.
- `sources/nasa_images.py`: `images-api.nasa.gov`, `NON_PUBLIC_DOMAIN_MARKERS` filtering, exponential backoff.
- `sources/clip_filter.py`: `_cuda_actually_works()` smoke test and the CUDA/DirectML/CPU fallback chain.

## Conventions
- New sources subclass `ImageSource`, ship offline tests (`tests/test_<source>_offline.py`), and include an `if __name__ == "__main__":` smoke test.
- Noise word lists target only genuinely generic terms (logo, patch, chart). Space or location words cause false negatives.
- Work iteratively: run, observe, fix the specific failure. Keep changes small and reviewable.
- Propose a plan before large changes. Run the offline tests before saying a task is done.

## Roadmap
1. **Foundation (current):** clean base, schema, separate stages, license gate, persistent rate limits, CLI, doctor, status. See `docs/scraper3-phase1.md`.
2. **Sources:** SpaceX, ESA, NASA Flickr, Openverse adapter, Wikimedia category walker, Wikidata linking (NORAD/COSPAR IDs), near-duplicate grouping.
3. **ML + annotation:** frozen DINOv3 backbone with multi-label attribute heads, calibration and conformal prediction, Label Studio or CVAT pre-annotation.
4. **Scale and release:** active learning loop, versioned releases with license manifests, group-aware train/test splits.

Work on one phase at a time. Do not start a later phase unless asked.
