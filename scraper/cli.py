"""Phase 1 Task 5 + 8: the Typer CLI.

Subcommands: scrape, filter, embed, export, status, doctor, refresh-catalog, retry-failed, serve.
"""

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import typer
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from scraper import catalog as catalog_module
from scraper import desktop, jobs, logging_setup
from scraper import doctor as doctor_module
from scraper import families as families_module
from scraper import profiles as profiles_module
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.stages import embed as embed_module
from scraper.stages import status as status_module
from scraper.stages.retry_failed import clear_negative_cache

app = typer.Typer(help="Satellite image pipeline, Ground Station.")

_logger = logging.getLogger("scraper.cli")


def _console_and_file_log(message: str) -> None:
    typer.echo(message)
    _logger.info(message)


@app.callback()
def main(
    ctx: typer.Context,
    db_path: Path = typer.Option(Path("scraper.db"), "--db-path", help="Path to the SQLite database."),
):
    logging_setup.configure_logging()
    conn = get_connection(db_path)
    apply_migrations(conn)
    ctx.obj = conn


def _resolve_families(family: Optional[str], profile_cfg: dict, catalog: Path) -> list[str]:
    if family:
        return [f.strip() for f in family.split(",") if f.strip()]
    all_families = families_module.load_families(catalog)
    family_limit = profile_cfg.get("family_limit")
    return all_families[:family_limit] if family_limit else all_families


def _run_scrape_with_progress(
    conn,
    families: list[str],
    sources: tuple[str, ...],
    limit,
    image_dir,
    dev: bool,
    domain_hint: Optional[str] = "satellite",
) -> dict:
    """CLI wrapper around jobs.run_scrape_job: adds a Rich progress bar (ETA) and
    routes Ctrl+C through the job's own graceful-interrupt handling."""
    with Progress(
        TextColumn("[bold]{task.fields[family]}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
    ) as progress:
        task_id = progress.add_task("scrape", total=len(families), family="starting...")

        def on_progress(completed: int, total: int, images_found: int) -> None:
            label = families[completed - 1] if 0 < completed <= len(families) else ""
            progress.update(task_id, completed=completed, total=total, family=label)

        return jobs.run_scrape_job(
            conn,
            families,
            sources,
            limit,
            image_dir,
            log=_console_and_file_log,
            on_progress=on_progress,
            dev=dev,
            domain_hint=domain_hint,
        )


@app.command()
def scrape(
    ctx: typer.Context,
    family: Optional[str] = typer.Option(None, "--family", help="Comma-separated family names."),
    source: str = typer.Option("wikimedia,nasa", "--source", help="Comma-separated sources."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Max images per family per source."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    profile: Optional[str] = typer.Option(None, "--profile", help="smoke|full"),
    dev: bool = typer.Option(False, "--dev", help="Use a cached session (requests-cache) -- dev/testing only."),
    domain_hint: str = typer.Option(
        "satellite", "--domain-hint", help='Appended to the search query; pass "" for a fully generic prompt.'
    ),
    catalog: Path = typer.Option(Path("satellites.csv"), "--catalog"),
    image_dir: Path = typer.Option(Path("satellite_images"), "--image-dir"),
):
    conn = ctx.obj
    sources = tuple(s.strip() for s in source.split(",") if s.strip())
    profile_cfg = profiles_module.load_profile(profile) if profile else {}
    effective_limit = limit if limit is not None else profile_cfg.get("image_limit")
    target_families = _resolve_families(family, profile_cfg, catalog)
    effective_domain_hint = domain_hint or None

    if dry_run:
        typer.echo(
            f"[dry-run] would scrape {len(target_families)} family(ies) from "
            f"sources={sources}, limit={effective_limit}, domain_hint={effective_domain_hint!r}"
        )
        for family_name in target_families:
            typer.echo(f"  - {family_name}")
        return

    report = _run_scrape_with_progress(
        conn, target_families, sources, effective_limit, image_dir, dev, domain_hint=effective_domain_hint
    )
    if report.get("error") == "login_failed":
        raise typer.Exit(code=1)

    typer.echo("\n--- Run report ---")
    typer.echo(f"Images added per source: {report['images_by_source']}")
    typer.echo(f"Rejections by reason:    {report['rejections_by_reason']}")


@app.command("filter")
def filter_cmd(
    ctx: typer.Context,
    family: Optional[str] = typer.Option(None, "--family", help="Comma-separated family names."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    min_score: float = typer.Option(None, "--min-score"),
    quarantine_dir: Path = typer.Option(Path("_quarantine"), "--quarantine-dir"),
):
    from scraper.stages import filter as filter_module

    conn = ctx.obj
    target_families = [f.strip() for f in family.split(",") if f.strip()] if family else None
    effective_min_score = min_score if min_score is not None else filter_module.MIN_SCORE
    counts = jobs.run_filter_job(
        conn, target_families, quarantine_dir, effective_min_score, dry_run, log=_console_and_file_log
    )
    typer.echo(counts)


@app.command("screener")
def screener_cmd(
    ctx: typer.Context,
    train: bool = typer.Option(False, "--train", help="Train it now instead of waiting for the next screening."),
):
    """The trained screener: where it stands, or train it now."""
    from scraper.ml import screener

    conn = ctx.obj
    if train:
        from scraper.stages import filter as filter_module

        if screener.train(conn, filter_module._build_real_scorer(), log=_console_and_file_log) is None:
            typer.echo("Not enough decisions to train on yet.")
    typer.echo(screener.describe(screener.status(conn)))


@app.command()
def embed(
    ctx: typer.Context,
    model: str = typer.Option(
        embed_module.DEFAULT_BACKBONE, "--model", help=f"Image model: {' or '.join(embed_module.BACKBONES)}."
    ),
    training_set: Optional[Path] = typer.Option(
        None, "--training-set", help="dataset.jsonl to embed too (default: training/dataset.jsonl if it exists)."
    ),
):
    """Embed images with a frozen pretrained model, for the attribute heads to train on."""
    try:
        jobs.run_embed_job(ctx.obj, log=_console_and_file_log, model=model, training_set=training_set)
    except ValueError as e:
        raise typer.BadParameter(str(e))


@app.command()
def prefill(
    ctx: typer.Context,
    model: Optional[str] = typer.Option(
        None, "--model", help="Image model whose trained heads to use (default: the most recently trained)."
    ),
    include_annotated: bool = typer.Option(
        False, "--include-annotated", help="Guess for images that are already annotated too."
    ),
    redo: bool = typer.Option(False, "--redo", help="Guess again for images that already have this model's guesses."),
):
    """Save the model's guesses for every image waiting for annotation, so the Annotate page's Auto-fill is instant."""
    from scraper.ml.predict import NoModelError

    try:
        jobs.run_prefill_job(
            ctx.obj, log=_console_and_file_log, model=model, include_annotated=include_annotated, redo=redo
        )
    except NoModelError as e:
        raise typer.BadParameter(str(e))


@app.command("discover-auto")
def discover_auto(
    ctx: typer.Context,
    batch_size: int = typer.Option(5, "--batch-size", help="Families to search this run."),
    max_uses: int = typer.Option(5, "--max-uses", help="Max web searches per family."),
    only_missing: bool = typer.Option(False, "--only-missing", help="Only families with no images yet."),
    catalog: Path = typer.Option(Path("satellites.csv"), "--catalog"),
):
    """Phase 2: search for candidate sources family by family, through the catalog."""
    result = jobs.run_auto_discovery_job(
        ctx.obj, catalog, batch_size, log=_console_and_file_log, max_uses=max_uses, only_missing=only_missing
    )
    typer.echo(f"\n{result['candidates_found']} candidate(s) to review with: python -m scraper.cli sources --pending")


master_app = typer.Typer(help="The master set: one copy of each kept image in data/images/<family>/, and the ledger.")
app.add_typer(master_app, name="master")


@master_app.command("check")
def master_check(
    ctx: typer.Context,
    fix: Optional[str] = typer.Option(None, "--fix", help="Fix one kind of problem, e.g. wrong_folder."),
):
    """List what keeps the master set from being one copy of each image, in the right family folder."""
    from scraper import master

    if fix:
        typer.echo(f"Fixing {fix}: {master.fix(ctx.obj, fix)}")
    for kind, issue in master.check_home(ctx.obj).items():
        if issue["count"]:
            fixable = f"  (fix: --fix {kind})" if issue["fixable"] else ""
            typer.echo(f"{issue['count']:>6}  {issue['label']}{fixable}")


@master_app.command("organize")
def master_organize(
    ctx: typer.Context, apply: bool = typer.Option(False, "--apply", help="Move the files (default: only count).")
):
    """Sort data/images/ into one folder per family."""
    from scraper import master

    result = master.organize_home(ctx.obj, dry_run=not apply)
    verb = "Moved" if apply else "Would move"
    typer.echo(f"{verb} {result['to_move']} of {result['files']} file(s) into {result['families']} family folder(s).")


@master_app.command("sync")
def master_sync(ctx: typer.Context):
    """Record every image in the database in the ledger (seeds a fresh ledger)."""
    from scraper import ledger, master

    typer.echo(f"Synced: {master.sync_ledger(ctx.obj, by='sync')}")
    typer.echo(f"Ledger now: {ledger.counts(ctx.obj)}")


@master_app.command("put-back")
def master_put_back(
    ctx: typer.Context,
    image_ids: Optional[list[int]] = typer.Argument(None, help="Image ids to put back."),
    reason: Optional[str] = typer.Option(None, "--reason", help="Put back every image set aside for this reason."),
    family: Optional[str] = typer.Option(None, "--family", help="File them under this family instead."),
):
    """Put images set aside from the master set (data/_rejected/) back into it."""
    from scraper import master

    conn = ctx.obj
    ids = list(image_ids or [])
    if reason:
        rows = conn.execute("SELECT id FROM images WHERE status != 'active' AND reject_reason = ?", (reason,))
        ids += [r["id"] for r in rows.fetchall() if r["id"] not in ids]
    if not ids:
        typer.echo("Nothing to put back: give image ids or --reason.")
        raise typer.Exit(1)
    done = 0
    for image_id in ids:
        try:
            path = master.put_back(conn, image_id, family)
        except ValueError as exc:
            typer.echo(f"  skipped: {exc}")
            continue
        done += 1
        typer.echo(f"  {image_id}: {path}")
    typer.echo(f"Put {done} of {len(ids)} image(s) back into the master set.")


@master_app.command("export")
def master_export(ctx: typer.Context, out: Path = typer.Option(Path("export/ledger.csv"), "--out")):
    """Write the ledger as a CSV (one row per image, with its sources and licenses)."""
    from scraper import ledger

    typer.echo(f"Wrote {ledger.export_csv(ctx.obj, out)} row(s) to {out}.")


@app.command("add-images")
def add_images_cmd(
    ctx: typer.Context,
    folder: Path = typer.Argument(..., help="Folder of images (subfolders can be family names)."),
    source: str = typer.Option(..., "--source", help="Name of where the images came from."),
    license: str = typer.Option("", "--license", help="License for the batch, e.g. 'CC BY 4.0' or 'Public domain'."),
    license_url: str = typer.Option("", "--license-url"),
    author: str = typer.Option("", "--author"),
    credit: str = typer.Option("", "--credit"),
    source_url: str = typer.Option("", "--source-url"),
    family: Optional[str] = typer.Option(None, "--family", help="One family for every image."),
    manifest: Optional[Path] = typer.Option(None, "--manifest", help="CSV with per-file details (a 'file' column)."),
    reviewed: bool = typer.Option(False, "--reviewed", help="Already reviewed: straight into the master set."),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Add images from an outside source, with their license."""
    try:
        jobs.run_add_images_job(
            ctx.obj,
            folder,
            source,
            license,
            log=_console_and_file_log,
            license_url=license_url,
            author=author,
            credit=credit,
            source_url=source_url,
            family=family,
            manifest=manifest,
            already_reviewed=reviewed,
            dry_run=dry_run,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))


@app.command()
def export(
    ctx: typer.Context,
    out_path: Path = typer.Option(Path("export/manifest.csv"), "--out"),
):
    jobs.run_export_job(ctx.obj, out_path, log=_console_and_file_log)


@app.command("import-annotations")
def import_annotations_cmd(
    ctx: typer.Context,
    csv_path: Path = typer.Argument(..., help="The old annotation tool's CSV (annotatedInfo.csv)."),
    annotator: str = typer.Option("legacy_miat", "--annotator", help="Annotator name to file the rows under."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would happen without writing to the DB."),
    attributes: Path = typer.Option(Path("attributes.json"), "--attributes"),
    review_out: Path = typer.Option(Path("export/legacy_import_review.csv"), "--review-out"),
    no_review_file: bool = typer.Option(False, "--no-review-file", help="Don't write the skipped-rows file."),
):
    """Import annotations made with the old PyQt annotation tool."""
    from scraper.stages import annotate as annotate_module

    try:
        result = annotate_module.import_legacy_csv(
            ctx.obj,
            csv_path,
            annotate_module.load_schema(attributes),
            annotator,
            dry_run,
            None if no_review_file else review_out,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))

    prefix = "[dry run] would import" if dry_run else "Imported"
    _console_and_file_log(f"{prefix} {result['imported']} of {result['rows']} rows as '{result['annotator']}'.")
    for key, label in (
        ("unmatched", "no matching image"),
        ("ambiguous", "file name matches several images"),
        ("misaligned", "values in the wrong column"),
        ("already_annotated", "already annotated"),
    ):
        if result[key]:
            _console_and_file_log(f"  skipped {result[key]}: {label}")
    if result["unmapped_columns"]:
        _console_and_file_log(f"  columns not imported: {', '.join(result['unmapped_columns'])}")
    for attr, count in result["off_schema"].items():
        _console_and_file_log(f"  {attr}: {count} value(s) kept as-is that don't fit the current attribute list")
    if result["review_path"]:
        _console_and_file_log(f"  skipped rows written to {result['review_path']}")


def _training_sources(data: Path, csv_paths: list[str], images: list[Path]):
    """--csv/--images when given; otherwise whatever is in the data folder."""
    from scraper.ml import dataset as dataset_module

    if csv_paths or images:
        return dataset_module.parse_csv_args(csv_paths), images
    csvs, image_dirs = dataset_module.data_sources(data)
    if csvs or image_dirs:
        _console_and_file_log(f"Using {data}/: {len(csvs)} annotation CSV(s), {len(image_dirs)} image folder(s).")
    return csvs, image_dirs


_DATA_HELP = (
    "Folder with annotations.csv (or annotations/<annotator>.csv) and images/, used when --csv/--images aren't."
)


@app.command("import-data")
def import_data_cmd(
    ctx: typer.Context,
    data: Path = typer.Option(Path("data"), "--data", help=_DATA_HELP),
    attributes: Path = typer.Option(Path("attributes.json"), "--attributes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only count what would be added."),
):
    """Add a data folder's images and annotations to the database, merging
    identical copies (the imported copy is kept)."""
    from scraper.stages import annotate as annotate_module
    from scraper.stages import import_data as import_module

    try:
        result = import_module.import_data_folder(
            ctx.obj, annotate_module.load_schema(attributes), data, dry_run=dry_run
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))
    for line in import_module.format_result(result):
        _console_and_file_log(line)


@app.command("build-training-set")
def build_training_set_cmd(
    ctx: typer.Context,
    data: Path = typer.Option(Path("data"), "--data", help=_DATA_HELP),
    csv_paths: list[str] = typer.Option(
        [],
        "--csv",
        help="An old-tool CSV, as NAME=PATH to say whose it is (repeatable: one per annotator).",
    ),
    images: list[Path] = typer.Option(
        [], "--images", help="Folder holding the old CSV's images (repeatable; searched recursively)."
    ),
    no_database: bool = typer.Option(False, "--no-database", help="Leave out annotations made in Ground Station."),
    attributes: Path = typer.Option(Path("attributes.json"), "--attributes"),
    out_dir: Path = typer.Option(Path("training"), "--out", help="Where dataset.jsonl and report.json go."),
    test_share: float = typer.Option(0.2, "--test-share", help="Share of satellite families held out for testing."),
):
    """Build the training set for the first-pass attribute model from annotator data."""
    from scraper.ml import dataset as dataset_module
    from scraper.stages import annotate as annotate_module

    try:
        csvs, image_dirs = _training_sources(data, csv_paths, images)
        report = dataset_module.build_training_set(
            None if no_database else ctx.obj,
            annotate_module.load_schema(attributes),
            csvs,
            image_dirs,
            out_dir,
            test_share,
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))

    typer.echo(dataset_module.format_report(report))
    _console_and_file_log(
        f"Training set: {report['examples']['train']} train / {report['examples']['test']} test examples "
        f"written to {out_dir}"
    )


@app.command("train-heads")
def train_heads_cmd(
    ctx: typer.Context,
    model: str = typer.Option(
        embed_module.DEFAULT_BACKBONE, "--model", help="Which image model's embeddings to train on."
    ),
    training_set: Path = typer.Option(Path("training/dataset.jsonl"), "--training-set"),
    attributes: Path = typer.Option(Path("attributes.json"), "--attributes"),
    out_dir: Path = typer.Option(Path("training"), "--out", help="Where heads_<model>.pt/.json go."),
):
    """Train the attribute heads on embedded images and score them on held-out families."""
    from scraper.ml import heads as heads_module
    from scraper.stages import annotate as annotate_module

    try:
        report = heads_module.run_training(
            ctx.obj, annotate_module.load_schema(attributes), training_set, model, out_dir
        )
    except ValueError as e:
        raise typer.BadParameter(str(e))
    typer.echo(heads_module.format_report(report))
    _console_and_file_log(f"Attribute heads for {model} saved to {report['saved_to']}")


@app.command("train")
def train_all_cmd(
    ctx: typer.Context,
    data: Path = typer.Option(Path("data"), "--data", help=_DATA_HELP),
    model: str = typer.Option(
        embed_module.DEFAULT_BACKBONE, "--model", help=f"Image model: {' or '.join(embed_module.BACKBONES)}."
    ),
    no_database: bool = typer.Option(False, "--no-database", help="Leave out annotations made in Ground Station."),
    attributes: Path = typer.Option(Path("attributes.json"), "--attributes"),
    out_dir: Path = typer.Option(Path("training"), "--out"),
    test_share: float = typer.Option(0.2, "--test-share"),
):
    """All of training in one go -- build the training set from the data folder,
    embed it, train and score the heads. Meant for a bigger machine: copy the
    project and its data/ folder over, run this, copy training/heads_<model>.pt back."""
    from scraper.ml import dataset as dataset_module
    from scraper.ml import heads as heads_module
    from scraper.stages import annotate as annotate_module

    if model not in embed_module.BACKBONES:
        raise typer.BadParameter(f"Unknown model '{model}'. Pick one of: {', '.join(embed_module.BACKBONES)}")
    schema = annotate_module.load_schema(attributes)
    try:
        csvs, image_dirs = _training_sources(data, [], [])
        if not csvs and no_database:
            raise ValueError(f"No annotation CSVs in {data}/ and --no-database given: nothing to train on.")
        _console_and_file_log("1/3 Building the training set...")
        report = dataset_module.build_training_set(
            None if no_database else ctx.obj, schema, csvs, image_dirs, out_dir, test_share
        )
        typer.echo(dataset_module.format_report(report))

        _console_and_file_log(f"2/3 Embedding with {model}...")
        jobs.run_embed_job(ctx.obj, log=_console_and_file_log, model=model, training_set=out_dir / "dataset.jsonl")

        _console_and_file_log("3/3 Training and scoring the attribute heads...")
        result = heads_module.run_training(ctx.obj, schema, out_dir / "dataset.jsonl", model, out_dir)
    except ValueError as e:
        raise typer.BadParameter(str(e))
    typer.echo(heads_module.format_report(result))
    _console_and_file_log(f"Done. The trained model is {result['saved_to']} -- copy it back to use it elsewhere.")


@app.command()
def status(
    ctx: typer.Context,
    catalog: Path = typer.Option(Path("satellites.csv"), "--catalog"),
):
    from scraper.sources import registry as registry_module

    conn = ctx.obj
    target_families = families_module.load_families(catalog)
    rows = status_module.compute_status(conn, target_families, sources=tuple(registry_module.list_sources()))

    for row in rows:
        typer.echo(
            f"{row['family']:<30} {row['state']:<8} images={row['image_count']:<4} untried={row['untried_sources']}"
        )

    zero_image_families = [row for row in rows if row["image_count"] == 0]
    if zero_image_families:
        typer.echo(f"\n{len(zero_image_families)} family(ies) with zero images:")
        for row in zero_image_families:
            typer.echo(f"  - {row['family']}")


@app.command()
def doctor():
    results = doctor_module.run_checks()
    for result in results:
        typer.echo(f"[{result.status.upper():4}] {result.name}: {result.detail}")
    if any(result.status == "fail" for result in results):
        raise typer.Exit(code=1)


@app.command("refresh-catalog")
def refresh_catalog_cmd(
    csv_path: Path = typer.Option(Path("satellites.csv"), "--catalog", help="Output satellites.csv path."),
    json_path: Path = typer.Option(Path("satellites.json"), "--json-path", help="Raw Space-Track dump path."),
    report_only: bool = typer.Option(False, "--report-only", help="Print the breakdown without writing any files."),
):
    """Re-fetch the satellite catalog from Space-Track and rebuild satellites.csv
    (FAMILY_NAME/VARIANT_NAME derived fresh). Backs up the existing CSV first.

    Known consequence: discovery_queue rows are keyed by family name and are
    NOT reconciled against catalog changes by this command -- family-name
    shifts from a live Space-Track pull can orphan existing discovery_queue
    rows or leave new families unqueued until the next discovery run.
    """
    import requests

    session = requests.Session()
    try:
        summary = catalog_module.refresh_catalog(session, csv_path, json_path, report_only=report_only)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    typer.echo(summary["report_text"])
    if report_only:
        typer.echo(f"\n[report-only] {summary['row_count']} rows would be written to {csv_path} (not written).")
    else:
        if summary.get("backup_path"):
            typer.echo(f"Backed up previous catalog to {summary['backup_path']}")
        typer.echo(
            f"Wrote {summary['row_count']} rows to {csv_path} ({summary['family_count']} families, "
            f"{summary['variant_count']} variant labels)."
        )


@app.command("retry-failed")
def retry_failed_cmd(
    ctx: typer.Context,
    family: Optional[str] = typer.Option(None, "--family", help="Comma-separated family names."),
    source: Optional[str] = typer.Option(None, "--source"),
    image_dir: Path = typer.Option(Path("satellite_images"), "--image-dir"),
):
    conn = ctx.obj
    target_families = [f.strip() for f in family.split(",") if f.strip()] if family else None
    source_module = source.strip() if source else None

    if target_families:
        cleared = sum(clear_negative_cache(conn, family=f, source_module=source_module) for f in target_families)
        typer.echo(f"Cleared {cleared} negative-cache entr(ies) for {target_families}.")
        sources = (source_module,) if source_module else ("wikimedia", "nasa")
        _run_scrape_with_progress(conn, target_families, sources, None, image_dir, dev=False)
    else:
        cleared = clear_negative_cache(conn, source_module=source_module)
        typer.echo(
            f"Cleared {cleared} negative-cache entr(ies) catalog-wide. "
            "Re-run `scrape` with an explicit scope to retry them."
        )


@app.command()
def discover(
    ctx: typer.Context,
    prompt: str = typer.Option(..., "--prompt", help="What kind of image source to look for."),
    max_uses: int = typer.Option(5, "--max-uses", help="Max web searches for this request."),
):
    """Phase 2: find candidate image sources (Claude's web search, or local models with DISCOVERY_PROVIDER=local)."""
    try:
        ids = jobs.run_discovery_job(ctx.obj, prompt, log=_console_and_file_log, max_uses=max_uses)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"\nReview with: python -m scraper.cli sources --pending   (ids: {ids})")


@app.command()
def codegen(
    ctx: typer.Context,
    candidate_id: int = typer.Argument(..., help="candidate_sources.id, from `discover`/`sources --pending`."),
):
    """Phase 2: draft an integration module for an approved candidate. Writes a
    file for you to read -- never imports or enables it."""
    try:
        jobs.run_codegen_job(ctx.obj, candidate_id, log=_console_and_file_log)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)


@app.command()
def approve(ctx: typer.Context, candidate_id: int = typer.Argument(...)):
    from scraper.discovery import review_candidate

    review_candidate(ctx.obj, candidate_id, approved=True)
    typer.echo(f"Candidate {candidate_id} approved. Run `codegen {candidate_id}` to draft its integration module.")


@app.command()
def reject(ctx: typer.Context, candidate_id: int = typer.Argument(...)):
    from scraper.discovery import review_candidate

    review_candidate(ctx.obj, candidate_id, approved=False)
    typer.echo(f"Candidate {candidate_id} rejected.")


@app.command()
def promote(ctx: typer.Context, candidate_id: int = typer.Argument(...)):
    """Phase 2: promote a reviewed candidate's generated module into
    scraper/sources/. Read the file at its generated_path yourself before
    running this -- promotion only checks structural shape, not correctness."""
    from scraper import promotion as promotion_module

    try:
        dest = promotion_module.promote(ctx.obj, candidate_id)
    except ValueError as e:
        typer.echo(f"Could not promote: {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Promoted to {dest}. Restart `scrape`/`serve` for it to be available as a source.")


@app.command()
def sources(
    ctx: typer.Context,
    pending: bool = typer.Option(False, "--pending", help="Only show candidates awaiting review."),
):
    """List registered sources and discovered candidates."""
    from scraper.sources import registry as registry_module

    typer.echo("Registered sources: " + ", ".join(sorted(registry_module.list_sources())))

    query = "SELECT * FROM candidate_sources"
    if pending:
        query += " WHERE status = 'pending'"
    query += " ORDER BY id"
    rows = ctx.obj.execute(query).fetchall()

    typer.echo(f"\nCandidates ({'pending only' if pending else 'all'}):")
    for row in rows:
        typer.echo(
            f"  [{row['id']}] {row['name']!r} status={row['status']} "
            f"verifiable={bool(row['license_verifiable'])} url={row['url']}"
        )


def _browser_target_url(host: str, port: int) -> str:
    """The URL a browser can actually open. 0.0.0.0 means "listen on every
    interface" -- it isn't a destination a browser can connect to, so the
    one running on this same machine should still land on localhost."""
    browsable_host = "127.0.0.1" if host == "0.0.0.0" else host
    return f"http://{browsable_host}:{port}/"


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Use 0.0.0.0 to reach it from another device on the LAN."),
    port: int = typer.Option(8000, "--port"),
    db_path: Path = typer.Option(Path("scraper.db"), "--db-path"),
    open_browser: bool = typer.Option(
        True, "--browser/--no-browser", help="Open the title screen in your browser once the server is up."
    ),
):
    """Launch the web GUI -- opens your browser to it automatically, no URL to type."""
    import uvicorn

    import scraper.web.app as web_app

    url = _browser_target_url(host, port)
    if desktop.is_running(url):
        # A second click on the launcher: show the one that's already up
        # rather than failing on "address already in use".
        typer.echo(f"Ground Station is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return

    web_app.DB_PATH = db_path
    typer.echo(f"Serving on http://{host}:{port} (Ctrl+C to stop)")

    if open_browser:
        # The server isn't listening yet the instant this line runs --
        # give uvicorn a moment to bind before the browser tries to connect.
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()

    # A Server object rather than uvicorn.run(), so the Quit button has
    # something to stop. Pages holding a live log stream open would
    # otherwise keep a shutdown waiting on them forever.
    server = uvicorn.Server(uvicorn.Config(web_app.app, host=host, port=port, timeout_graceful_shutdown=3))
    web_app.SERVER = server
    server.run()


@app.command()
def launch(port: int = typer.Option(desktop.DEFAULT_PORT, "--port")):
    """What the app icon runs: opens Ground Station in your browser, starting
    it in the background first if it isn't already running."""
    if not desktop.launch(port):
        raise typer.Exit(1)


@app.command("install-app")
def install_app(
    remove: bool = typer.Option(False, "--remove", help="Take the icon away again (the project is not touched)."),
    port: int = typer.Option(desktop.DEFAULT_PORT, "--port"),
):
    """Put a Ground Station icon where this machine keeps its apps: the
    Applications folder (macOS), the Start menu and Desktop (Windows), or the
    applications menu (Linux). Click it to open Ground Station -- no terminal."""
    if remove:
        removed = desktop.remove()
        for path in removed:
            typer.echo(f"Removed {path}")
        if not removed:
            typer.echo("Nothing to remove -- the app isn't installed.")
        return
    for path in desktop.install(port):
        typer.echo(f"Installed {path}")
    typer.echo("It opens this project folder. If you move the folder, run install-app again.")


if __name__ == "__main__":
    app()
