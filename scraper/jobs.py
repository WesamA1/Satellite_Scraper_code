"""Phase 1 Task 8: the shared job-runner layer.

Both the CLI and the web GUI call these instead of looping/printing themselves,
so there's one place that knows how to run a scrape (with graceful interrupt and
progress reporting) rather than two slightly different copies.
"""

import sqlite3
import threading
import time
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
from scraper.stages import scrape_runs as scrape_runs_module
from scraper.stages.download import download_family
from scraper.stages.retry_failed import clear_negative_cache

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_file(conn: sqlite3.Connection) -> Optional[Path]:
    """The database file behind `conn`, so each lane can open its own
    connection (sqlite3 connections can't be shared across threads)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


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
    on_lanes=None,
) -> dict:
    """Scrape `families` (or, Phase 2, free-text prompts) from `sources`.

    Each site runs in its own lane (see scraper/stages/lanes.py): one worker
    per site going through the families, all at the same time, each still
    one request at a time with its usual pacing. Wikimedia, Wikidata and the
    Wikipedias share one lane (one bot account, one rate limit). A site that
    keeps erroring stops only its own lane.

    `on_progress(completed, total, images_found)`: completed is the number
    of families every lane has finished. `on_lanes(list of lane snapshots)`
    fires whenever a lane moves on (family, source, item, state).
    `on_status(family, source, source_index=None, source_total=None,
    item_index=None, item_total=None)` is the single-lane "currently on"
    readout, and only fires when there's one lane.

    Stopping (a set `cancel` event, or Ctrl+C) lets each lane finish its
    current family, then returns the partial run report. Resuming needs no
    special handling: re-running is safe, since nothing already downloaded
    is fetched again. `domain_hint` defaults to "satellite"; pass None/"" for
    a fully generic prompt. `from_scratch` clears the negative cache for
    every family in scope (all sources) before searching, so families
    previously marked "no images found" get searched again; it never
    touches downloaded images.
    """
    from scraper.db.connection import get_connection
    from scraper.stages import lanes as lanes_module

    if from_scratch:
        cleared = sum(clear_negative_cache(conn, family=f) for f in families)
        log(f"Cleared {cleared} negative-cache entries for {len(families)} family(ies) -- searching all of them fresh.")

    session = wikimedia.make_session(dev=dev)
    if "wikimedia" in sources:
        if not wikimedia.login(session, conn):
            log("Wikimedia login failed; aborting.")
            return {"error": "login_failed"}

    db_file = _db_file(conn)
    lane_groups = lanes_module.group_sources(sources)
    if db_file is None and len(lane_groups) > 1:
        lane_groups = {"all": tuple(sources)}  # no file for extra connections: one site after another
    single = len(lane_groups) == 1
    if not single:
        log(
            f"Running {len(lane_groups)} lanes side by side: "
            + ", ".join(
                name if members == (name,) else f"{name} ({', '.join(members)})"
                for name, members in lane_groups.items()
            )
            + "."
        )

    cancel = cancel or threading.Event()
    since = _now()
    total = len(families)
    run_id = scrape_runs_module.start(conn, families, sources, since)
    progress_lock = threading.Lock()
    last_progress = {"completed": -1}

    def report_progress(force: bool = False) -> None:
        if not on_progress:
            return
        completed = run.families_done()
        with progress_lock:
            if not force and completed == last_progress["completed"]:
                return
            last_progress["completed"] = completed
        on_progress(completed, total, run.images_found)

    def lanes_changed(snapshot: list[dict]) -> None:
        if on_lanes and not single:
            on_lanes(snapshot)

    run = lanes_module.LaneRun(lane_groups, families, cancel, log, on_change=lanes_changed)

    def run_family(lane, family_name, lane_conn, lane_session):
        started: dict[str, float] = {}
        so_far = {"images": 0}

        def on_source_start(source_module, index, source_total):
            started[source_module] = time.monotonic()
            run.changed(lane, source=source_module, item="")
            if single and on_status:
                on_status(family_name, source_module, source_index=index, source_total=source_total)

        def on_item_progress(source_module, item_index, item_total):
            run.changed(lane, item=f"{item_index} / {item_total}")
            if single and on_status:
                on_status(family_name, source_module, item_index=item_index, item_total=item_total)

        def on_source_done(source_module, downloaded):
            if source_module in started:
                elapsed = time.monotonic() - started.pop(source_module)
                lane.source_seconds[source_module] = lane.source_seconds.get(source_module, 0.0) + elapsed
            if downloaded:
                so_far["images"] += downloaded
                run.add_images(lane, downloaded)
                report_progress(force=True)

        result = download_family(
            lane_conn,
            lane_session,
            family_name,
            image_dir=image_dir,
            sources=lane.sources,
            limit=limit,
            domain_hint=domain_hint,
            cancel=cancel,
            on_source_start=on_source_start,
            on_item_progress=on_item_progress,
            on_source_done=on_source_done,
        )
        by_source = {src: r["downloaded"] for src, r in result.items() if isinstance(r, dict) and r.get("downloaded")}
        family_total = sum(by_source.values())
        if family_total > so_far["images"]:  # a source that didn't report through on_source_done
            run.add_images(lane, family_total - so_far["images"])
        prefix = "" if single else f"[{lane.name}] "
        if family_total:
            breakdown = ", ".join(f"{src}:{n}" for src, n in by_source.items())
            log(f"{prefix}{family_name}: +{family_total} image(s) ({breakdown})")
        elif single:
            log(f"{family_name}: no images found")  # with lanes, only finds and errors are logged

    def open_lane(lane):
        if single:
            return conn, session
        lane_conn = get_connection(db_file)
        lane_session = session if lane.name == "wikimedia" else wikimedia.make_session(dev=dev)
        return lane_conn, lane_session

    def close_lane(lane, handles):
        lane_conn, lane_session = handles
        if lane_conn is not conn:
            lane_conn.close()
        if lane_session is not session:
            lane_session.close()

    run.on_family_done = lambda lane: report_progress(force=single)
    interrupted = False
    try:
        run.run(open_lane, close_lane, run_family)
    except KeyboardInterrupt:
        interrupted = True
        log(
            f"Interrupted after {run.families_done()}/{total} families -- stopping cleanly. "
            "Re-run the same command to resume."
        )
    except Exception as e:
        scrape_runs_module.finish(
            conn,
            run_id,
            "failed",
            since,
            run.families_done(),
            error=f"{e.__class__.__name__}: {e}",
            extras=run.per_source_extras(),
        )
        raise

    completed = run.families_done()
    if not interrupted and cancel.is_set() and not run.all_finished():
        interrupted = True
        log(f"Cancelled after {completed}/{total} families -- stopping cleanly. Re-run the same command to resume.")
    gave_up = all(lane.state == "gave_up" for lane in run.lanes.values())
    status = "stopped" if interrupted else ("failed" if gave_up and run.lanes else "finished")
    scrape_runs_module.finish(
        conn, run_id, status, since, completed, error=run.error_summary(), extras=run.per_source_extras()
    )
    report = run_report_module.build_run_report(conn, since)
    report["interrupted"] = interrupted
    report["completed"] = completed
    report["total"] = total
    report["lanes"] = [lane.snapshot() for lane in run.lanes.values()]
    if run.error_summary():
        log(f"Some sources had trouble: {run.error_summary()}.")

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
        log=log,
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


def run_prefill_job(
    conn: sqlite3.Connection,
    log: LogFn = print,
    model: Optional[str] = None,
    include_annotated: bool = False,
    redo: bool = False,
    cancel: Optional[threading.Event] = None,
    on_progress: Optional[ProgressFn] = None,
    attributes_path: Optional[Path] = None,
) -> dict:
    """Pre-fill from the Train page or the CLI: the model's guesses for every
    image waiting for annotation, saved ahead of time."""
    from scraper.ml import prefill as prefill_module
    from scraper.stages import annotate as annotate_module

    schema = annotate_module.load_schema(attributes_path or annotate_module.DEFAULT_SCHEMA_PATH)
    result = prefill_module.run_prefill(
        conn,
        schema,
        preferred=model,
        include_annotated=include_annotated,
        redo=redo,
        cancel=cancel,
        on_progress=on_progress,
        log=log,
    )
    for line in prefill_module.format_result(result).splitlines():
        log(line)
    return result


def run_import_data_job(
    conn: sqlite3.Connection,
    data_dir: Path,
    dry_run: bool = False,
    log: LogFn = print,
    attributes_path: Optional[Path] = None,
) -> dict:
    """import-data from the Annotate page."""
    from scraper.stages import annotate as annotate_module
    from scraper.stages import import_data as import_module

    log(f"{'Checking' if dry_run else 'Importing'} {data_dir}/...")
    schema = annotate_module.load_schema(attributes_path or annotate_module.DEFAULT_SCHEMA_PATH)
    result = import_module.import_data_folder(conn, schema, data_dir, dry_run=dry_run)
    for line in import_module.format_result(result):
        log(line)
    return result


def run_add_images_job(
    conn: sqlite3.Connection,
    folder: Path,
    source_name: str,
    license_short_name: str,
    log: LogFn = print,
    **options,
) -> dict:
    """Adds images from an outside source (scraper/stages/add_images.py)."""
    from scraper.stages import add_images

    log(f"Adding images from {folder} as {add_images.source_key(source_name)}...")
    result = add_images.add_folder(conn, Path(folder), source_name, license_short_name, **options)
    for line in add_images.format_result(result).splitlines():
        log(line)
    return result


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
    """Phase 2: finds candidate sources for `prompt` (Claude's web search, or
    a local model with SearXNG -- see discovery.provider()) and persists them
    as pending review rows. `client` defaults to a real anthropic.Anthropic()
    only if not injected -- tests always inject a fake."""
    log(f"Searching for candidate sources: {prompt!r}")
    candidates = discovery.find_candidate_sites(prompt, client=client, max_uses=max_uses, conn=conn, log=log)
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
    only_missing: bool = False,
) -> dict:
    """Phase 2: works through the satellite family catalog on its own,
    generating one search prompt per family instead of requiring a manually
    typed prompt. Every other Discovery gate is unchanged -- still only finds
    candidate source websites, still needs human approve/reject and manual
    promote. One family's API failure doesn't abort the batch, same as
    run_scrape_job's per-family loop.

    `cancel` is checked between families (web GUI stop button), same
    checkpoint granularity as run_scrape_job. `on_progress` fires after each
    family in the batch, same shape too. `only_missing` searches only
    families that have no images yet, so searches go to the gaps."""
    families = families_module.load_families(catalog)
    discovery.seed_queue(conn, families)
    only = discovery.families_without_images(conn, families) if only_missing else None
    batch = discovery.next_batch(conn, batch_size, only)

    if not batch:
        if only_missing:
            log("Every family without images has already been searched.")
        else:
            log("Discovery queue is empty -- every family has already been searched.")
        return {"searched": 0, "failed": 0, "candidates_found": 0}
    if only_missing:
        log(f"Searching families with no images yet ({len(only)} in the catalog).")

    searched = 0
    failed = 0
    total_candidates = 0
    for idx, family in enumerate(batch, start=1):
        if cancel is not None and cancel.is_set():
            log(f"Cancelled after {searched + failed}/{len(batch)} families -- stopping cleanly.")
            break

        prompt = f"open-licensed image sources for {family} {domain_hint}".strip()
        try:
            candidates = discovery.find_candidate_sites(
                prompt, client=client, max_uses=max_uses, conn=conn, log=lambda m, f=family: log(f"{f}: {m}")
            )
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
