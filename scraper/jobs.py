"""Phase 1 Task 8: the shared job-runner layer.

Both the CLI and the web GUI call these instead of looping/printing themselves,
so there's one place that knows how to run a scrape (with graceful interrupt and
progress reporting) rather than two slightly different copies.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from scraper import codegen, discovery, notify
from scraper import families as families_module
from scraper.sources import wikimedia
from scraper.stages import dedupe as dedupe_module
from scraper.stages import embed as embed_module
from scraper.stages import export as export_module
from scraper.stages import filter as filter_module
from scraper.stages import run_report as run_report_module
from scraper.stages.download import download_family
from scraper.stages.retry_failed import clear_negative_cache

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_scrape_job(
    conn: sqlite3.Connection,
    families: list[str],
    sources: tuple[str, ...],
    limit: Optional[int],
    image_dir: Path,
    log: LogFn = print,
    on_progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
    dev: bool = False,
    domain_hint: Optional[str] = "satellite",
    from_scratch: bool = False,
    on_status=None,
) -> dict:
    """Run download_family for each family (or, Phase 2, each free-text prompt)
    in turn.

    `on_status(family, source, source_index=None, source_total=None,
    item_index=None, item_total=None)` fires right before each source is
    attempted within the current family, and again for each item within
    that source -- see download_family's own docstring for why: a
    family-level counter alone can look frozen for minutes while one source
    (or one item within it) is slow or stuck, with no way to tell which.

    Checkpoints between families, never mid-download: a KeyboardInterrupt (CLI
    Ctrl+C) or a set `cancel` event (web GUI stop button) is only acted on after
    the current family finishes, then this returns the partial run report.
    Resuming needs no special handling -- re-running the same command is already
    safe (Tasks 1/3's resume-safety). `domain_hint` defaults to "satellite"
    (unchanged); pass None/"" for a fully generic, domain-agnostic prompt.

    `from_scratch` clears the negative cache for every family in scope (all
    sources, not just the ones selected for this run) before searching, so a
    family previously marked "no images found" gets searched again. It never
    touches already-downloaded images -- download_family's per-item
    is_already_fetched check still runs exactly as before, so nothing already
    on file gets re-downloaded or duplicated.
    """
    if from_scratch:
        cleared = sum(clear_negative_cache(conn, family=f) for f in families)
        log(f"Cleared {cleared} negative-cache entries for {len(families)} family(ies) -- searching all of them fresh.")

    session = wikimedia.make_session(dev=dev)
    if "wikimedia" in sources:
        if not wikimedia.login(session, conn):
            log("Wikimedia login failed; aborting.")
            return {"error": "login_failed"}

    since = _now()
    total = len(families)
    completed = 0
    images_found = 0
    interrupted = False

    try:
        for family_name in families:
            if cancel is not None and cancel.is_set():
                log(
                    f"Cancelled after {completed}/{total} families -- stopping cleanly. "
                    "Re-run the same command to resume."
                )
                interrupted = True
                break

            def on_source_start(source_module, index, total):
                if on_status:
                    on_status(family_name, source_module, source_index=index, source_total=total)

            def on_item_progress(source_module, item_index, item_total):
                if on_status:
                    on_status(family_name, source_module, item_index=item_index, item_total=item_total)

            # Tracked separately from images_found so the reconciliation
            # below adds only what on_source_done didn't already count --
            # real download_family calls fire it once per source and this
            # ends up equal to family_total (no double-count); a test
            # double that returns a result dict without calling it leaves
            # this at 0, so the reconciliation covers the whole family
            # exactly like it did before on_source_done existed.
            family_images_so_far = 0

            def on_source_done(source_module, downloaded):
                nonlocal images_found, family_images_so_far
                if downloaded:
                    images_found += downloaded
                    family_images_so_far += downloaded
                    if on_progress:
                        on_progress(completed, total, images_found)

            result = download_family(
                conn,
                session,
                family_name,
                image_dir=image_dir,
                sources=sources,
                limit=limit,
                domain_hint=domain_hint,
                cancel=cancel,
                on_source_start=on_source_start,
                on_item_progress=on_item_progress,
                on_source_done=on_source_done,
            )
            by_source = {
                src: r["downloaded"] for src, r in result.items() if isinstance(r, dict) and r.get("downloaded")
            }
            family_total = sum(by_source.values())
            images_found += family_total - family_images_so_far
            if family_total:
                breakdown = ", ".join(f"{src}:{n}" for src, n in by_source.items())
                log(f"{family_name}: +{family_total} image(s) ({breakdown})")
            else:
                log(f"{family_name}: no images found")
            completed += 1
            if on_progress:
                on_progress(completed, total, images_found)
    except KeyboardInterrupt:
        log(f"Interrupted after {completed}/{total} families -- stopping cleanly. Re-run the same command to resume.")
        interrupted = True

    report = run_report_module.build_run_report(conn, since)
    report["interrupted"] = interrupted
    report["completed"] = completed
    report["total"] = total

    log(
        f"Run report: images_by_source={report['images_by_source']} "
        f"rejections_by_reason={report['rejections_by_reason']}"
    )
    notify.notify(
        f"Scrape finished: {completed}/{total} families"
        + (" (interrupted)" if interrupted else "")
        + f". Images: {report['images_by_source']}"
    )
    return report


def run_filter_job(
    conn: sqlite3.Connection,
    families: Optional[list[str]],
    quarantine_dir: Path,
    min_score: float,
    dry_run: bool,
    log: LogFn = print,
    scorer=None,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    counts = filter_module.run_filter(
        conn,
        quarantine_dir=quarantine_dir,
        families=families,
        min_score=min_score,
        scorer=scorer,
        dry_run=dry_run,
        cancel=cancel,
        on_progress=on_progress,
    )
    log(f"Filter result: {counts}")
    return counts


def run_embed_job(
    conn: sqlite3.Connection,
    log: LogFn = print,
    model: str = embed_module.DEFAULT_BACKBONE,
    training_set: Optional[Path] = None,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    """Embeds active images, plus the training set's images -- the one passed
    in, or the default build-training-set output when it exists."""
    from scraper.ml import dataset as dataset_module

    if training_set is None and (dataset_module.DEFAULT_OUT_DIR / "dataset.jsonl").is_file():
        training_set = dataset_module.DEFAULT_OUT_DIR / "dataset.jsonl"
    if training_set is not None:
        log(f"Including the training set's images from {training_set}.")
    result = embed_module.run_embed(conn, model, training_set, cancel=cancel, on_progress=on_progress)
    device = f" on {result['device']}" if result["device"] else ""
    log(
        f"Embedded {result['embedded']} image(s) with {result['model']}{device}; "
        f"{result['already_embedded']} already embedded, {result['missing']} file(s) missing, "
        f"{result['unreadable']} unreadable{' (stopped early)' if result['cancelled'] else ''}."
    )
    return result


def run_build_training_set_job(
    conn: sqlite3.Connection,
    csvs: list[str],
    image_dirs: list[str],
    include_database: bool = True,
    log: LogFn = print,
    attributes_path: Optional[Path] = None,
) -> dict:
    """build-training-set from the Train page. `csvs` are NAME=PATH values."""
    from scraper.ml import dataset as dataset_module
    from scraper.stages import annotate as annotate_module

    log("Building the training set...")
    report = dataset_module.build_training_set(
        conn if include_database else None,
        annotate_module.load_schema(attributes_path or annotate_module.DEFAULT_SCHEMA_PATH),
        dataset_module.parse_csv_args(csvs),
        [Path(d) for d in image_dirs],
    )
    log(
        f"Training set: {report['examples']['train']} train / {report['examples']['test']} test examples "
        f"from {report['families']['train'] + report['families']['test']} families."
    )
    for annotator, reasons in report["skipped_rows_by_annotator"].items():
        log(f"  left out ({annotator}): " + ", ".join(f"{n} {r}" for r, n in reasons.items()))
    return report


def run_train_heads_job(
    conn: sqlite3.Connection,
    model: str = embed_module.DEFAULT_BACKBONE,
    log: LogFn = print,
    attributes_path: Optional[Path] = None,
) -> dict:
    """train-heads from the Train page."""
    from scraper.ml import heads as heads_module
    from scraper.stages import annotate as annotate_module

    log(f"Training attribute heads on {model} embeddings...")
    schema = annotate_module.load_schema(attributes_path or annotate_module.DEFAULT_SCHEMA_PATH)
    report = heads_module.run_training(conn, schema, backbone=model)
    log(f"Trained {len(report['attributes'])} attribute(s); saved to {report['saved_to']}.")
    for name, why in report["skipped_attributes"].items():
        log(f"  {name}: not trained ({why})")
    return report


def run_dedupe_job(conn: sqlite3.Connection, families: Optional[list[str]], log: LogFn = print) -> dict:
    result = dedupe_module.run_dedupe(conn, families=families)
    log(f"Removed {result['identical_copies_removed']} identical copy(ies) within a family (quarantined, not deleted).")
    if result["families_to_choose"]:
        log(
            f"{result['families_to_choose']} file(s) were found under more than one family -- "
            "Review will ask which family each one belongs to."
        )
    log(f"Hashed {result['hashed']} image(s); {result['grouped_images']} in {result['groups']} look-alike group(s).")
    return result


def run_export_job(conn: sqlite3.Connection, out_path: Path, log: LogFn = print) -> int:
    count = export_module.run_export(conn, out_path=out_path)
    log(f"Export: wrote {count} row(s) to {out_path}.")
    return count


def run_discovery_job(
    conn: sqlite3.Connection, prompt: str, log: LogFn = print, client=None, max_uses: int = 5
) -> list[int]:
    """Phase 2: finds candidate sources for `prompt` via Claude's web search
    tool and persists them as pending review rows. `client` defaults to a real
    anthropic.Anthropic() only if not injected -- tests always inject a fake."""
    log(f"Searching for candidate sources: {prompt!r}")
    candidates = discovery.find_candidate_sites(prompt, client=client, max_uses=max_uses)
    ids = discovery.persist_candidates(conn, prompt, candidates)
    log(f"Found {len(candidates)} candidate(s): {[c.get('name') for c in candidates]}")
    return ids


def run_auto_discovery_job(
    conn: sqlite3.Connection,
    catalog: Path,
    batch_size: int,
    log: LogFn = print,
    client=None,
    max_uses: int = 5,
    domain_hint: str = "satellite",
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[ProgressFn] = None,
) -> dict:
    """Phase 2: works through the satellite family catalog on its own,
    generating one search prompt per family instead of requiring a manually
    typed prompt. Every other Discovery gate is unchanged -- still only finds
    candidate source websites, still needs human approve/reject and manual
    promote. One family's API failure doesn't abort the batch, same as
    run_scrape_job's per-family loop.

    `cancel` is checked between families (web GUI stop button), same
    checkpoint granularity as run_scrape_job. `on_progress` fires after each
    family in the batch, same shape too."""
    families = families_module.load_families(catalog)
    discovery.seed_queue(conn, families)
    batch = discovery.next_batch(conn, batch_size)

    if not batch:
        log("Discovery queue is empty -- every family has already been searched.")
        return {"searched": 0, "failed": 0, "candidates_found": 0}

    searched = 0
    failed = 0
    total_candidates = 0
    for idx, family in enumerate(batch, start=1):
        if cancel is not None and cancel.is_set():
            log(f"Cancelled after {searched + failed}/{len(batch)} families -- stopping cleanly.")
            break

        prompt = f"open-licensed image sources for {family} {domain_hint}".strip()
        try:
            candidates = discovery.find_candidate_sites(prompt, client=client, max_uses=max_uses)
            discovery.persist_candidates(conn, prompt, candidates)
            # Only a successful search marks the family searched -- a failure
            # (e.g. a missing/invalid ANTHROPIC_API_KEY) leaves it in the
            # queue so the next batch retries it, rather than recording a
            # false "searched, found nothing".
            discovery.mark_searched(conn, family, len(candidates))
            searched += 1
            total_candidates += len(candidates)
            log(f"{family}: found {len(candidates)} candidate(s).")
        except Exception as e:
            failed += 1
            log(f"{family}: failed -- {e.__class__.__name__}: {e}")

        if on_progress:
            on_progress(idx, len(batch))

    status = discovery.queue_status(conn)
    log(
        f"Batch done: {searched} family(ies) searched, {failed} failed, {total_candidates} candidate(s) found. "
        f"{status['remaining']} remaining in queue."
    )
    return {"searched": searched, "failed": failed, "candidates_found": total_candidates}


def run_codegen_job(conn: sqlite3.Connection, candidate_id: int, log: LogFn = print, client=None) -> str:
    """Phase 2: drafts an integration module for an approved candidate. Writes
    the draft to scraper/sources/candidates/ for review -- never imports or
    enables it. That only happens through scraper.promotion.promote(), after a
    human reads the file."""
    row = conn.execute("SELECT * FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"No candidate with id {candidate_id}")
    if row["status"] not in ("approved", "code_generated"):
        raise ValueError(
            f"Candidate {candidate_id} has status {row['status']!r} -- "
            "codegen needs it approved first (or already code_generated, to regenerate)."
        )

    log(f"Generating integration module for {row['name']!r}...")
    source_text = codegen.generate_source_module(dict(row), client=client)
    path = codegen.write_candidate_module(conn, candidate_id, source_text, row["name"])
    log(f"Draft written to {path} -- review it before promoting.")
    return source_text
