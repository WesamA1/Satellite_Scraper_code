"""Phase 1 Task 8 + the web GUI: FastAPI app.

Single-user local tool -- background jobs run on plain threads with a
queue.Queue per job for log/progress events, streamed to the browser over SSE.
No job-queue system needed at this scale.

The GUI is real multi-page navigation (one FastAPI route per section,
server-rendered via Jinja2) rather than a client-side single-page shell --
each page is its own URL, and the sidebar/activity-strip/nav-badge state is
computed server-side on every render plus kept fresh between page loads via
a short client-side poll of /api/jobs/active (see static/js/common.js).
"""

import json
import logging
import os
import queue
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from scraper import discovery, jobs, logging_setup
from scraper import doctor as doctor_module
from scraper import families as families_module
from scraper import profiles as profiles_module
from scraper import secrets as secrets_module
from scraper import wipe as wipe_module
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.ml import check_set as check_set_module
from scraper.ml import predict as predict_module
from scraper.ml import workspace as workspace_module
from scraper.stages import annotate as annotate_module
from scraper.stages import annotate_folder as annotate_folder_module
from scraper.stages import embed as embed_module
from scraper.stages import filter as filter_module
from scraper.stages import review as review_module
from scraper.stages import status as status_module

DB_PATH = Path("scraper.db")
ATTRIBUTES_PATH = annotate_module.DEFAULT_SCHEMA_PATH
ANNOTATION_SOURCE_PATH = annotate_folder_module.SOURCE_CONFIG_PATH
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Sidebar structure, shared by base.html (the full nav) and the pipeline
# breadcrumb partial (just the "Pipeline" group) -- one place defines the
# page list instead of duplicating it in markup.
NAV_GROUPS = [
    {
        "label": "Pipeline",
        "links": [
            {"target": "scrape", "href": "scrape", "icon_name": "scrape", "text": "Scrape"},
            {"target": "filter", "href": "filter", "icon_name": "filter", "text": "Filter"},
            {"target": "export", "href": "export", "icon_name": "export", "text": "Export"},
        ],
    },
    {
        "label": "Review",
        "links": [
            {"target": "review", "href": "review", "icon_name": "review", "text": "Review"},
        ],
    },
    {
        "label": "Annotate",
        "links": [
            {"target": "annotate", "href": "annotate", "icon_name": "annotate", "text": "Annotate"},
            {"target": "attributes", "href": "attributes", "icon_name": "attributes", "text": "Attributes"},
            {"target": "data", "href": "data", "icon_name": "data", "text": "Data"},
            {"target": "train", "href": "train", "icon_name": "train", "text": "Train model"},
        ],
    },
    {
        "label": "Monitor",
        "links": [
            {"target": "status", "href": "status", "icon_name": "status", "text": "Status"},
            {"target": "doctor", "href": "doctor", "icon_name": "doctor", "text": "Doctor"},
        ],
    },
    {
        "label": "Sources",
        "links": [
            {"target": "discovery", "href": "discovery", "icon_name": "discovery", "text": "Discovery"},
            {"target": "api_keys", "href": "api-keys", "icon_name": "api_keys", "text": "API Keys"},
        ],
    },
    {
        "label": "About",
        "links": [
            {"target": "release_notes", "href": "release-notes", "icon_name": "release_notes", "text": "Release Notes"},
        ],
    },
]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging_setup.configure_logging()
    yield


app = FastAPI(title="Ground Station", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _static_url(path: str) -> str:
    """/static/<path>, cache-busted with the file's own mtime. Browsers
    otherwise have no reason to ever refetch style.css/splash.css/*.js once
    cached -- the HTML referencing them can change without the CSS/JS URL
    itself changing, so a stale copy can persist indefinitely (including
    across a full browser restart, since that's disk cache, not memory
    state). Appending a version derived from the file's actual last-modified
    time means any real edit changes the URL, which forces a refetch."""
    full_path = STATIC_DIR / path
    try:
        version = int(full_path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


templates.env.globals["static_url"] = _static_url

_jobs: dict[str, dict] = {}
# How long a finished job's entry stays around after completing -- long enough
# that its SSE stream (opened right after the job starts, per common.js) has
# certainly already read the final event, and that the nav badges/active-jobs
# poll can still show "just finished" for a bit. Swept opportunistically on
# every new job start rather than on a background timer -- simpler, and this
# dict otherwise only ever grows for the life of the server process.
FINISHED_JOB_TTL_SECONDS = 300


class ScrapeRequest(BaseModel):
    family: Optional[str] = None
    source: str = "wikimedia,nasa"
    limit: Optional[int] = None
    profile: Optional[str] = None
    dev: bool = False
    domain_hint: Optional[str] = "satellite"
    image_dir: str = "satellite_images"
    catalog: str = "satellites.csv"
    from_scratch: bool = False


class RetryFailedRequest(BaseModel):
    family: Optional[str] = None
    catalog: str = "satellites.csv"


class DiscoveryRequest(BaseModel):
    prompt: str
    max_uses: int = 5


class AutoDiscoveryRequest(BaseModel):
    batch_size: int = 5
    max_uses: int = 5
    catalog: str = "satellites.csv"
    domain_hint: str = "satellite"


class CodegenRequest(BaseModel):
    candidate_id: int


class ManualCandidateRequest(BaseModel):
    url: str
    name: str = ""
    license_model: str = ""
    license_verifiable: bool = False
    notes: str = ""


class FilterRequest(BaseModel):
    family: Optional[str] = None
    min_score: Optional[float] = None
    dry_run: bool = False
    quarantine_dir: str = "_quarantine"


class EmbedRequest(BaseModel):
    model: str = embed_module.DEFAULT_BACKBONE


class TrainingSetRequest(BaseModel):
    csvs: list[dict] = []
    image_dirs: list[str] = []
    include_database: bool = True


class ImportDataRequest(BaseModel):
    data_dir: str = "data"
    dry_run: bool = False


class CheckSetCreateRequest(BaseModel):
    size: int = check_set_module.DEFAULT_SIZE


class CheckSetLabelRequest(BaseModel):
    sha256: str
    satellites: str
    recognizable: str = ""
    labeled_by: str = ""


class TrainHeadsRequest(BaseModel):
    model: str = embed_module.DEFAULT_BACKBONE


class PrefillRequest(BaseModel):
    model: Optional[str] = None
    include_annotated: bool = False
    redo: bool = False


class ExportRequest(BaseModel):
    out_path: str = "export/manifest.csv"


class ApiKeyWriteRequest(BaseModel):
    key: str
    value: str


class DedupeRequest(BaseModel):
    family: Optional[str] = None


class ReviewDecisionRequest(BaseModel):
    decision: str
    reason: Optional[str] = None  # required for a reject: a review.REJECT_REASONS key
    quarantine_dir: str = "_quarantine"


class ReviewFamilyRequest(BaseModel):
    family: str
    quarantine_dir: str = "_quarantine"
    catalog: str = "satellites.csv"


class ReviewVariantRequest(BaseModel):
    variant: str
    remember: bool = False  # also add it to the family's known variants
    catalog: str = "satellites.csv"


class AnnotationSubmitRequest(BaseModel):
    annotator: str
    values: dict = {}
    unusable: bool = False
    # The schema version the page built its form from -- a mismatch means
    # someone edited the attribute list mid-image, so the page has to reload
    # it rather than submit against attributes it never showed.
    schema_version: Optional[str] = None


class AnnotationExportRequest(BaseModel):
    out_path: str = str(annotate_module.DEFAULT_EXPORT_PATH)
    annotator: Optional[str] = None


class AnnotationImportRequest(BaseModel):
    csv_path: str
    annotator: str = annotate_module.LEGACY_ANNOTATOR
    dry_run: bool = True
    # Blank = don't write a review file; the skip counts still come back.
    review_path: Optional[str] = None


class AnnotationSourceRequest(BaseModel):
    mode: str = "database"
    imageSource: str = ""
    imageDestination: str = ""
    CSVDest: str = ""
    attributesPath: str = ""


class OldToolSettingsRequest(BaseModel):
    location: str


class FolderSuggestRequest(BaseModel):
    name: str


class FolderSubmitRequest(BaseModel):
    name: str
    values: dict = {}
    unusable: bool = False
    schema_version: Optional[str] = None


WIPE_CONFIRM_PHRASE = "WIPE EVERYTHING"


class WipeRequest(BaseModel):
    confirm: str


def _prune_finished_jobs() -> None:
    stale_ids = [
        job_id
        for job_id, entry in _jobs.items()
        if entry["status"] != "running"
        and entry.get("finished_at") is not None
        and time.time() - entry["finished_at"] > FINISHED_JOB_TTL_SECONDS
    ]
    for job_id in stale_ids:
        del _jobs[job_id]


def _new_job(label: str, target: str) -> tuple[str, dict]:
    _prune_finished_jobs()
    job_id = uuid.uuid4().hex
    entry = {
        "queue": queue.Queue(),
        "cancel": threading.Event(),
        "status": "running",
        "label": label,
        "target": target,
        "finished_at": None,
    }
    _jobs[job_id] = entry
    return job_id, entry


def _push(entry: dict, event: dict) -> None:
    if event["type"] in ("progress", "lanes"):
        entry.setdefault("last_events", {})[event["type"]] = event
    elif event["type"] == "status":
        # A status event only ever carries source_index/source_total OR
        # item_index/item_total, never both (see on_status below) -- the
        # client-side handler relies on that, updating only the sub-field a
        # given event carries and leaving the rest of the DOM alone. The
        # snapshot kept here for reattaching connections has to merge the
        # same way, or the far-more-frequent item-only ticks would each wipe
        # out the source_index/source_total from the last time a source
        # actually started. The one exception: a fresh source start has to
        # drop any leftover item_index/item_total from the *previous*
        # source, since those item counts don't carry over to a new one.
        last = entry.setdefault("last_events", {})
        previous = last.get("status", {})
        if "source_index" in event:
            previous = {k: v for k, v in previous.items() if k not in ("item_index", "item_total")}
        last["status"] = {**previous, **event}
    entry["queue"].put(event)


def _start_thread(entry: dict, target, *args) -> None:
    """Runs target(conn, *args) on a new thread, with its own DB connection --
    sqlite3 connections can't be shared across threads, so this opens one
    inside the thread that actually uses it rather than in the request
    handler that kicked the job off."""

    def run():
        conn = get_connection(DB_PATH)
        apply_migrations(conn)
        try:
            result = target(conn, *args)
            entry["status"] = "done"
            _push(entry, {"type": "done", "result": result})
        except Exception as e:
            entry["status"] = "error"
            _push(entry, {"type": "error", "message": f"{e.__class__.__name__}: {e}"})
        finally:
            entry["finished_at"] = time.time()
            conn.close()

    threading.Thread(target=run, daemon=True).start()


_job_logger = logging.getLogger("scraper.web.jobs")


def _log_fn(entry: dict):
    """A job's log lines go to its page and to the log file in logs/."""

    def log(message: str) -> None:
        _push(entry, {"type": "log", "message": message})
        _job_logger.info("[%s] %s", entry["label"], message)

    return log


def _reject_if_already_running(target: str) -> None:
    """Scrape and filter both move files to deterministic paths
    (image_dir/family/family_source_NN.ext) -- two jobs for the same stage
    running at once can compute the same destination path and collide,
    crashing one of them on images.path's UNIQUE constraint. Blocking a
    second concurrent job for the same stage is cheaper and safer than
    making every path collision survivable."""
    already_running = any(entry["target"] == target and entry["status"] == "running" for entry in _jobs.values())
    if already_running:
        raise HTTPException(409, f"A {target} job is already running -- wait for it to finish first.")


def _nav_badges(conn) -> dict:
    """Running-job + pending-candidate counts for the sidebar badges. Computed
    fresh on every page render and re-fetched by the client poll in
    common.js, since real page navigations don't keep any in-memory JS state
    between pages."""
    pending = conn.execute("SELECT COUNT(*) AS n FROM candidate_sources WHERE status = 'pending'").fetchone()["n"]
    running_jobs = [
        {"job_id": job_id, "label": entry["label"], "target": entry["target"]}
        for job_id, entry in _jobs.items()
        if entry["status"] == "running"
    ]
    return {"jobs": running_jobs, "pending_candidates": pending}


def _render(request: Request, template_name: str, active: str, **extra_context) -> HTMLResponse:
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        context = {
            "active": active,
            "nav_groups": NAV_GROUPS,
            "nav_badges": _nav_badges(conn),
            **extra_context,
        }
    finally:
        conn.close()
    return templates.TemplateResponse(request, template_name, context)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def splash(request: Request):
    return templates.TemplateResponse(request, "splash.html", {})


@app.get("/scrape", response_class=HTMLResponse)
def page_scrape(request: Request):
    from scraper.sources import registry as registry_module

    sources = registry_module.list_sources()
    available_sources = [
        {"name": name, "ready": secrets_module.is_source_ready(name, module)}
        for name, module in sorted(sources.items())
    ]
    return _render(request, "scrape.html", "scrape", available_sources=available_sources)


@app.get("/filter", response_class=HTMLResponse)
def page_filter(request: Request):
    return _render(request, "filter.html", "filter")


@app.get("/embed")
def page_embed():
    """Embedding is step 2 of the Train model page now; old links land there."""
    return RedirectResponse("/train", status_code=307)


@app.get("/check-set", response_class=HTMLResponse)
def page_check_set(request: Request):
    return _render(request, "check_set.html", "train")


@app.get("/api/check-set")
def get_check_set():
    return check_set_module.status()


@app.post("/api/check-set/create")
def create_check_set(req: CheckSetCreateRequest):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return check_set_module.create(conn, size=req.size)
    except check_set_module.CheckSetError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/check-set/label")
def label_check_set(req: CheckSetLabelRequest):
    try:
        return check_set_module.label(req.sha256, req.satellites, req.recognizable, req.labeled_by)
    except check_set_module.CheckSetError as e:
        raise HTTPException(400, str(e))


@app.get("/api/check-set/image")
def get_check_set_image(sha256: str):
    try:
        return FileResponse(check_set_module.image_path(sha256))
    except check_set_module.CheckSetError as e:
        raise HTTPException(404, str(e))


@app.get("/data", response_class=HTMLResponse)
def page_data(request: Request):
    return _render(request, "data.html", "data")


@app.get("/export", response_class=HTMLResponse)
def page_export(request: Request):
    return _render(request, "export.html", "export")


@app.get("/status", response_class=HTMLResponse)
def page_status(request: Request):
    return _render(request, "status.html", "status")


@app.get("/doctor", response_class=HTMLResponse)
def page_doctor(request: Request):
    return _render(request, "doctor.html", "doctor", wipe_confirm_phrase=WIPE_CONFIRM_PHRASE)


@app.get("/discovery", response_class=HTMLResponse)
def page_discovery(request: Request):
    return _render(request, "discovery.html", "discovery")


@app.get("/api-keys", response_class=HTMLResponse)
def page_api_keys(request: Request):
    return _render(request, "api_keys.html", "api_keys")


@app.get("/train", response_class=HTMLResponse)
def page_train(request: Request):
    return _render(request, "train.html", "train", wide=True)


@app.get("/review", response_class=HTMLResponse)
def page_review(request: Request):
    return _render(request, "review.html", "review", reject_reasons=review_module.REJECT_REASONS)


@app.get("/annotate", response_class=HTMLResponse)
def page_annotate(request: Request):
    return _render(
        request,
        "annotate.html",
        "annotate",
        wide=True,
        annotation_source=annotate_folder_module.load_source_config(ANNOTATION_SOURCE_PATH),
    )


@app.get("/attributes", response_class=HTMLResponse)
def page_attributes(request: Request):
    return _render(request, "attributes.html", "attributes", wide=True, attribute_types=annotate_module.TYPES)


@app.get("/release-notes", response_class=HTMLResponse)
def page_release_notes(request: Request):
    from scraper.release_notes import parse_changelog

    entries = parse_changelog(Path("CHANGELOG.md")) if Path("CHANGELOG.md").exists() else []
    return _render(request, "release_notes.html", "release_notes", entries=entries)


@app.post("/api/jobs/scrape")
def start_scrape(req: ScrapeRequest):
    _reject_if_already_running("scrape")
    sources = tuple(s.strip() for s in req.source.split(",") if s.strip())
    profile_cfg = profiles_module.load_profile(req.profile) if req.profile else {}
    limit = req.limit if req.limit is not None else profile_cfg.get("image_limit")

    if req.family:
        target_families = [f.strip() for f in req.family.split(",") if f.strip()]
    else:
        all_families = families_module.load_families(Path(req.catalog))
        family_limit = profile_cfg.get("family_limit")
        target_families = all_families[:family_limit] if family_limit else all_families

    job_id, entry = _new_job("Scrape", "scrape")

    def on_progress(completed, total, images_found):
        _push(entry, {"type": "progress", "completed": completed, "total": total, "images_found": images_found})

    def on_status(family, source, source_index=None, source_total=None, item_index=None, item_total=None):
        event = {"type": "status", "family": family, "source": source}
        if source_index is not None:
            event["source_index"] = source_index
            event["source_total"] = source_total
        if item_index is not None:
            event["item_index"] = item_index
            event["item_total"] = item_total
        _push(entry, event)

    def on_lanes(lanes):
        _push(entry, {"type": "lanes", "lanes": lanes})

    _start_thread(
        entry,
        jobs.run_scrape_job,
        target_families,
        sources,
        limit,
        Path(req.image_dir),
        _log_fn(entry),
        on_progress,
        entry["cancel"],
        req.dev,
        req.domain_hint or None,
        req.from_scratch,
        on_status,
        on_lanes,
    )
    return {"job_id": job_id, "families": target_families}


@app.post("/api/retry-failed-searches")
def retry_failed_searches(req: RetryFailedRequest):
    """Clears the negative cache (families/sources previously marked "no
    images found") so the next scrape searches them again -- never touches
    an already-downloaded image. Synchronous, not a background job: it's a
    single DB write, done before the user would even click Run scrape."""
    from scraper.stages.retry_failed import clear_negative_cache

    if req.family:
        target_families = [f.strip() for f in req.family.split(",") if f.strip()]
    else:
        target_families = families_module.load_families(Path(req.catalog))

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        cleared = sum(clear_negative_cache(conn, family=f) for f in target_families)
    finally:
        conn.close()
    return {"cleared": cleared, "families": len(target_families)}


@app.post("/api/jobs/filter")
def start_filter(req: FilterRequest):
    _reject_if_already_running("filter")
    target_families = [f.strip() for f in req.family.split(",") if f.strip()] if req.family else None
    min_score = req.min_score if req.min_score is not None else filter_module.MIN_SCORE

    job_id, entry = _new_job("Filter", "filter")

    def on_progress(completed, total):
        _push(entry, {"type": "progress", "completed": completed, "total": total})

    _start_thread(
        entry,
        jobs.run_filter_job,
        target_families,
        Path(req.quarantine_dir),
        min_score,
        req.dry_run,
        _log_fn(entry),
        None,
        entry["cancel"],
        on_progress,
    )
    return {"job_id": job_id}


@app.post("/api/jobs/embed")
def start_embed(req: Optional[EmbedRequest] = None):
    req = req or EmbedRequest()
    if req.model not in embed_module.BACKBONES:
        raise HTTPException(400, f"Unknown model '{req.model}'.")
    _reject_if_already_running("embed")
    job_id, entry = _new_job("Embed", "embed")

    def on_progress(completed, total):
        _push(entry, {"type": "progress", "completed": completed, "total": total})

    _start_thread(entry, jobs.run_embed_job, _log_fn(entry), req.model, None, entry["cancel"], on_progress)
    return {"job_id": job_id}


@app.get("/api/train/status")
def train_status():
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return workspace_module.status(conn)
    finally:
        conn.close()


@app.post("/api/jobs/training-set")
def start_training_set(req: TrainingSetRequest):
    csvs = workspace_module.csv_args(req.csvs)
    image_dirs = [d.strip() for d in req.image_dirs if d.strip()]
    if not csvs and not req.include_database:
        raise HTTPException(400, "Add an old-tool CSV, or include Ground Station's annotations.")
    _reject_if_already_running("train")
    workspace_module.save_settings(
        {
            "csvs": req.csvs or [{"annotator": "", "path": ""}],
            "image_dirs": image_dirs,
            "include_database": req.include_database,
        }
    )
    job_id, entry = _new_job("Training set", "train")
    _start_thread(
        entry,
        jobs.run_build_training_set_job,
        csvs,
        image_dirs,
        req.include_database,
        _log_fn(entry),
        _attributes_path(),
    )
    return {"job_id": job_id}


@app.post("/api/jobs/import-data")
def start_import_data(req: ImportDataRequest):
    # Same target as the dedupe scan: both merge identical copies, so never at once.
    _reject_if_already_running("review")
    job_id, entry = _new_job("Import data", "review")
    _start_thread(entry, jobs.run_import_data_job, Path(req.data_dir), req.dry_run, _log_fn(entry), _attributes_path())
    return {"job_id": job_id}


@app.post("/api/jobs/train-heads")
def start_train_heads(req: TrainHeadsRequest):
    if req.model not in embed_module.BACKBONES:
        raise HTTPException(400, f"Unknown model '{req.model}'.")
    _reject_if_already_running("train")
    workspace_module.save_settings({"model": req.model})
    job_id, entry = _new_job("Train model", "train")
    _start_thread(entry, jobs.run_train_heads_job, req.model, _log_fn(entry), _attributes_path())
    return {"job_id": job_id}


@app.post("/api/jobs/prefill")
def start_prefill(req: Optional[PrefillRequest] = None):
    from scraper.ml import predict as predict_module

    req = req or PrefillRequest()
    if req.model is not None and req.model not in embed_module.BACKBONES:
        raise HTTPException(400, f"Unknown model '{req.model}'.")
    model = req.model or _preferred_model()  # the model the Annotate page's Auto-fill uses
    if predict_module.model_path(preferred=model) is None:
        raise HTTPException(400, "No trained model yet -- train one first.")
    _reject_if_already_running("train")  # not while the model it uses is being retrained
    job_id, entry = _new_job("Pre-fill", "train")

    def on_progress(completed, total):
        _push(entry, {"type": "progress", "completed": completed, "total": total})

    _start_thread(
        entry,
        jobs.run_prefill_job,
        _log_fn(entry),
        model,
        req.include_annotated,
        req.redo,
        entry["cancel"],
        on_progress,
        _attributes_path(),
    )
    return {"job_id": job_id}


@app.post("/api/jobs/export")
def start_export(req: ExportRequest):
    job_id, entry = _new_job("Export", "export")
    _start_thread(entry, jobs.run_export_job, Path(req.out_path), _log_fn(entry))
    return {"job_id": job_id}


@app.post("/api/jobs/discover")
def start_discover(req: DiscoveryRequest):
    job_id, entry = _new_job("Discovery", "discovery")
    _start_thread(entry, jobs.run_discovery_job, req.prompt, _log_fn(entry), None, req.max_uses)
    return {"job_id": job_id}


@app.post("/api/jobs/discover/auto")
def start_auto_discover(req: AutoDiscoveryRequest):
    job_id, entry = _new_job("Auto-discovery", "discovery")

    def on_progress(completed, total):
        _push(entry, {"type": "progress", "completed": completed, "total": total})

    _start_thread(
        entry,
        jobs.run_auto_discovery_job,
        Path(req.catalog),
        req.batch_size,
        _log_fn(entry),
        None,
        req.max_uses,
        req.domain_hint,
        entry["cancel"],
        on_progress,
    )
    return {"job_id": job_id}


@app.post("/api/jobs/codegen")
def start_codegen(req: CodegenRequest):
    job_id, entry = _new_job("Codegen", "discovery")
    _start_thread(entry, jobs.run_codegen_job, req.candidate_id, _log_fn(entry))
    return {"job_id": job_id}


@app.get("/api/discovery/queue-status")
def discovery_queue_status(catalog: str = "satellites.csv"):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    families = families_module.load_families(Path(catalog))
    discovery.seed_queue(conn, families)
    result = discovery.queue_status(conn)
    conn.close()
    return result


@app.post("/api/candidates/manual")
def add_manual_candidate(req: ManualCandidateRequest):
    if not req.url.strip():
        raise HTTPException(400, "url is required")
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    candidate_id = discovery.add_manual_candidate(
        conn,
        req.url.strip(),
        req.name,
        req.license_model,
        req.license_verifiable,
        req.notes,
    )
    conn.close()
    return {"candidate_id": candidate_id}


@app.post("/api/candidates/seed")
def load_seed_candidates():
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    ids = discovery.load_seed_candidates(conn)
    conn.close()
    return {"added": len(ids)}


@app.get("/api/candidates")
def list_candidates(pending: bool = False):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    query = "SELECT * FROM candidate_sources"
    if pending:
        query += " WHERE status = 'pending'"
    query += " ORDER BY id DESC"
    rows = conn.execute(query).fetchall()
    conn.close()
    return {"candidates": [dict(row) for row in rows]}


@app.get("/api/candidates/{candidate_id}/code")
def get_candidate_code(candidate_id: int):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    row = conn.execute("SELECT generated_path FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "unknown candidate_id")
    if not row["generated_path"]:
        raise HTTPException(404, "no generated code yet for this candidate")
    code_path = Path(row["generated_path"])
    if not code_path.exists():
        raise HTTPException(404, f"generated file is missing from disk: {code_path}")
    return {"code": code_path.read_text()}


@app.post("/api/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int):
    from scraper.discovery import review_candidate

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    review_candidate(conn, candidate_id, approved=True)
    conn.close()
    return {"status": "approved"}


@app.post("/api/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int):
    from scraper.discovery import review_candidate

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    review_candidate(conn, candidate_id, approved=False)
    conn.close()
    return {"status": "rejected"}


@app.post("/api/candidates/{candidate_id}/promote")
def promote_candidate(candidate_id: int):
    from scraper import promotion as promotion_module

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        dest = promotion_module.promote(conn, candidate_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"promoted_to": str(dest)}


@app.get("/api/sources")
def list_registered_sources():
    from scraper.sources import registry as registry_module

    return {"sources": sorted(registry_module.list_sources())}


@app.get("/api/jobs/active")
def list_active_jobs():
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return _nav_badges(conn)
    finally:
        conn.close()


@app.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(404, "unknown job_id")

    def event_stream():
        # An empty queue on connect means the job has gone quiet for a while
        # (e.g. mid-backoff pause) rather than mid-burst, which is exactly
        # when a newly-opened connection (reattaching via the activity
        # strip's View link, or just navigating back to the page) would
        # otherwise sit blank until the job's next tick. Show the latest
        # known progress/status right away in that case. If the queue isn't
        # empty there's a real backlog to deliver in order, so leave it
        # alone rather than risk showing a stale snapshot ahead of it.
        if entry["queue"].empty():
            for snapshot_type in ("progress", "status", "lanes"):
                snapshot = entry.get("last_events", {}).get(snapshot_type)
                if snapshot is not None:
                    yield f"data: {json.dumps(snapshot)}\n\n"
        while True:
            try:
                event = entry["queue"].get(timeout=30)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield f"data: {json.dumps(event)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    entry = _jobs.get(job_id)
    if entry is None:
        raise HTTPException(404, "unknown job_id")
    entry["cancel"].set()
    return {"cancelled": True}


@app.get("/api/overview")
def get_overview(catalog: str = "satellites.csv"):
    from scraper.stages import overview as overview_module

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return overview_module.build(conn, Path(catalog))
    finally:
        conn.close()


@app.get("/api/status")
def get_status(catalog: str = "satellites.csv"):
    from scraper.sources import registry as registry_module

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    target_families = families_module.load_families(Path(catalog))
    rows = status_module.compute_status(conn, target_families, sources=tuple(registry_module.list_sources()))
    return {"rows": rows}


@app.get("/api/doctor")
def get_doctor():
    results = doctor_module.run_checks()
    return {"results": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results]}


@app.post("/api/keys")
def write_api_key(req: ApiKeyWriteRequest, request: Request):
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "API key writes are only accepted from localhost.")
    try:
        secrets_module.write_env_value(req.key, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    print(f"[api-keys] updated {req.key} via the web GUI")
    return {"status": "saved"}


# Gives the in-flight HTTP response to /api/wipe time to actually flush to
# the client before the process image underneath it gets replaced. A real
# module-level constant (not a literal inline) so tests can shrink it instead
# of actually sleeping a full second.
RESTART_DELAY_SECONDS = 1.0


def _restart_process() -> None:
    """Replaces this process with a fresh one running `python -m scraper.cli`
    plus this process's own CLI args, so code changes made since the server
    started take effect too -- not just a wipe's own code path.

    Deliberately reconstructed as -m rather than reused from sys.argv:
    launching via `-m` puts the current working directory on sys.path, which
    is what lets cli.py's own `from scraper import ...` resolve. sys.argv[0]
    for a `-m` launch is already resolved to cli.py's absolute file path, so
    naively re-execing [sys.executable] + sys.argv runs it as a plain script
    instead -- same file, different sys.path, so `scraper` is no longer
    importable (confirmed live: this exact mistake took the server down
    after a real wipe). cwd carries across exec unmodified, so -m resolves
    the same package regardless of where this process happened to be
    launched from.

    Sockets opened by the `socket` module default to close-on-exec in
    CPython, so the port this server is bound to is freed as part of the
    exec syscall itself -- the new process can bind it again immediately, no
    race against the old one still holding it open."""
    os.execv(sys.executable, [sys.executable, "-m", "scraper.cli"] + sys.argv[1:])


@app.post("/api/wipe")
def wipe_data(req: WipeRequest, request: Request):
    """Clears every image and the data derived from it (sources, licenses,
    predictions, annotations, rejections, rate limits) plus the actual
    image/quarantine directories, then restarts the process so the next
    scrape starts from a genuinely clean slate. Leaves candidate_sources and
    discovery_queue alone -- see scraper/wipe.py's docstring for why.

    Destructive and irreversible, so: localhost-only (same restriction as
    API key writes), and requires typing WIPE_CONFIRM_PHRASE exactly rather
    than just clicking a button."""
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Wiping is only accepted from localhost.")
    if req.confirm != WIPE_CONFIRM_PHRASE:
        raise HTTPException(400, f'Type "{WIPE_CONFIRM_PHRASE}" exactly to confirm.')

    # Wiping out from under a job that's still writing would crash it on a
    # missing file or a now-invisible row, and could leave the DB/disk in a
    # half-wiped, half-written state -- cancel first and give it a moment to
    # actually stop, same as the Stop button, before touching anything.
    running = [entry for entry in _jobs.values() if entry["status"] == "running"]
    for entry in running:
        entry["cancel"].set()
    deadline = time.time() + 10
    while any(entry["status"] == "running" for entry in running) and time.time() < deadline:
        time.sleep(0.1)

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        result = wipe_module.wipe_images_and_data(conn, Path("satellite_images"), Path("_quarantine"))
    finally:
        conn.close()

    print(f"[wipe] cleared via the web GUI, restarting: {result}")

    def restart_after_response_flushes():
        time.sleep(RESTART_DELAY_SECONDS)
        _restart_process()

    threading.Thread(target=restart_after_response_flushes, daemon=True).start()
    return {"status": "wiped", **result}


@app.post("/api/jobs/dedupe")
def start_dedupe(req: DedupeRequest):
    families = [req.family] if req.family else None
    job_id, entry = _new_job("Dedupe", "review")
    _start_thread(entry, jobs.run_dedupe_job, families, _log_fn(entry))
    return {"job_id": job_id}


@app.get("/api/review/queue")
def get_review_queue(family: Optional[str] = None, limit: int = 50):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    queue = review_module.review_queue(conn, family=family, limit=limit)
    conn.close()
    return {"queue": queue}


@app.get("/api/review/variants")
def get_review_variants(family: str, catalog: str = "satellites.csv"):
    variants = review_module.available_variants(family, catalog=Path(catalog))
    return {"variants": variants}


@app.post("/api/review/{image_id}/decide")
def review_decide(image_id: int, req: ReviewDecisionRequest):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        if req.decision == "reject" and req.reason not in review_module.REJECT_REASONS:
            raise ValueError(f"Say why it's rejected: one of {', '.join(review_module.REJECT_REASONS)}.")
        review_module.decide(conn, image_id, req.decision, Path(req.quarantine_dir), req.reason or "other")
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"status": "ok"}


@app.post("/api/review/{image_id}/family")
def review_choose_family(image_id: int, req: ReviewFamilyRequest):
    """Which family a file downloaded under several families belongs to."""
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return review_module.choose_family(conn, image_id, req.family, Path(req.quarantine_dir), Path(req.catalog))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.get("/api/families")
def search_families(q: str = "", limit: int = 20, catalog: str = "satellites.csv"):
    """Satellite-catalog families matching q (names starting with it first),
    for picking a family by typing instead of scrolling ~1,600 names."""
    path = Path(catalog)
    if not path.is_file():
        return {"families": [], "total": 0}
    families = families_module.load_families(path)
    needle = q.strip().lower()
    matches = [f for f in families if needle in f.lower()] if needle else families
    matches.sort(key=lambda f: (not f.lower().startswith(needle), f))
    return {"families": matches[: max(1, min(limit, 100))], "total": len(matches)}


@app.post("/api/review/{image_id}/variant")
def review_set_variant(image_id: int, req: ReviewVariantRequest):
    variant = " ".join(req.variant.split())
    if not variant:
        raise HTTPException(400, "Pick or type a variant first.")
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        row = conn.execute("SELECT family FROM images WHERE id = ?", (image_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "unknown image_id")
        added = False
        if req.remember:
            try:
                added = review_module.add_known_variant(row["family"], variant, Path(req.catalog))
            except ValueError as e:
                raise HTTPException(400, str(e))
        review_module.record_variant(conn, image_id, variant)
    finally:
        conn.close()
    return {"status": "ok", "added_to_known": added}


@app.get("/api/images/{image_id}/file")
def get_image_file(image_id: int):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    row = conn.execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(404, "unknown image_id")

    path = Path(row["path"]).resolve()
    if not path.is_file() or not path.is_relative_to(Path.cwd().resolve()):
        raise HTTPException(404, "image file not found")
    return FileResponse(path)


def _attributes_path() -> Path:
    """attributes.json, unless the annotation settings point at another
    attribute file (e.g. the old tool's own) -- then that one, for both
    annotating and the Attributes page."""
    return annotate_folder_module.attributes_path(
        annotate_folder_module.load_source_config(ANNOTATION_SOURCE_PATH), ATTRIBUTES_PATH
    )


def _load_attribute_schema() -> dict:
    path = _attributes_path()
    try:
        return annotate_module.load_schema(path)
    except annotate_module.SchemaError as e:
        raise HTTPException(500, f"{path}: {e}")


@app.get("/api/annotate/schema")
def get_annotation_schema():
    schema = _load_attribute_schema()
    return {
        "schema": schema,
        "version": annotate_module.schema_version(schema),
        "types": list(annotate_module.TYPES),
        "path": str(_attributes_path()),
    }


@app.put("/api/annotate/schema")
def save_annotation_schema(schema: dict):
    try:
        saved = annotate_module.save_schema(schema, _attributes_path())
    except annotate_module.SchemaError as e:
        raise HTTPException(400, str(e))
    return {"schema": saved, "version": annotate_module.schema_version(saved)}


@app.get("/api/annotate/queue")
def get_annotation_queue(
    annotator: str,
    family: Optional[str] = None,
    reviewed_only: bool = True,
    skip_annotated_by_others: bool = True,
    limit: int = 50,
):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return annotate_module.annotation_queue(
            conn,
            annotator.strip(),
            family=family or None,
            reviewed_only=reviewed_only,
            limit=limit,
            skip_annotated_by_others=skip_annotated_by_others,
        )
    finally:
        conn.close()


@app.get("/api/annotate/stats")
def get_annotation_stats():
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return annotate_module.annotation_counts(conn)
    finally:
        conn.close()


@app.post("/api/annotate/export")
def export_image_annotations(req: AnnotationExportRequest):
    """Synchronous, not a background job: one DB read and one CSV write."""
    schema = _load_attribute_schema()
    out_path = Path(req.out_path)
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        rows = annotate_module.export_annotations(conn, schema, out_path, annotator=req.annotator or None)
    finally:
        conn.close()
    return {"rows": rows, "path": str(out_path)}


@app.post("/api/annotate/import")
def import_legacy_annotations(req: AnnotationImportRequest):
    """Synchronous, like export: a few thousand CSV rows and one DB write.
    Defaults to a dry run so the page can show what would happen first."""
    schema = _load_attribute_schema()
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return annotate_module.import_legacy_csv(
            conn,
            Path(req.csv_path.strip()).expanduser(),
            schema,
            annotator=req.annotator,
            dry_run=req.dry_run,
            review_path=Path(req.review_path.strip()) if req.review_path and req.review_path.strip() else None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.get("/api/annotate/source")
def get_annotation_source():
    return annotate_folder_module.load_source_config(ANNOTATION_SOURCE_PATH)


@app.put("/api/annotate/source")
def save_annotation_source(req: AnnotationSourceRequest):
    try:
        return annotate_folder_module.save_source_config(req.model_dump(), ANNOTATION_SOURCE_PATH)
    except annotate_folder_module.FolderError as e:
        raise HTTPException(400, str(e))


@app.post("/api/annotate/source/old-tool")
def read_old_tool_settings(req: OldToolSettingsRequest):
    """Reads the old tool's metadata.json (+ attributes.json) so the page can
    fill in the folder settings. Doesn't save anything by itself."""
    try:
        return annotate_folder_module.read_old_tool_settings(Path(req.location.strip()))
    except annotate_folder_module.FolderError as e:
        raise HTTPException(400, str(e))


def _folder_config() -> dict:
    config = annotate_folder_module.load_source_config(ANNOTATION_SOURCE_PATH)
    if config["mode"] != "folder":
        raise HTTPException(400, "Folder mode isn't switched on -- set it up under Setup first.")
    return config


@app.get("/api/annotate/folder/queue")
def get_folder_queue(limit: int = 50):
    try:
        return annotate_folder_module.folder_queue(_folder_config(), limit=limit)
    except annotate_folder_module.FolderError as e:
        raise HTTPException(400, str(e))


@app.get("/api/annotate/folder/image")
def get_folder_image(name: str):
    try:
        return FileResponse(annotate_folder_module.image_path(_folder_config(), name))
    except annotate_folder_module.FolderError as e:
        raise HTTPException(404, str(e))


@app.post("/api/annotate/folder/submit")
def submit_folder_annotation(req: FolderSubmitRequest):
    config = _folder_config()
    schema = _load_attribute_schema()
    if req.schema_version and req.schema_version != annotate_module.schema_version(schema):
        raise HTTPException(409, "The attribute list changed since this image was opened.")
    try:
        saved = annotate_folder_module.submit_folder_annotation(
            config, req.name, req.values, schema, unusable=req.unusable
        )
    except annotate_module.AnnotationError as e:
        raise HTTPException(422, {"message": "Some attributes are missing or invalid.", "errors": e.errors})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"status": "ok", "values": saved}


# ---- Auto-fill: the trained model's guesses ----


def _preferred_model() -> str:
    return workspace_module.load_settings()["model"]


@app.get("/api/annotate/model")
def annotate_model_info():
    return predict_module.model_info(preferred=_preferred_model())


def _suggest(path: Path, image_id: int | None):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        return predict_module.suggest(
            conn, path, _load_attribute_schema(), image_id=image_id, preferred=_preferred_model()
        )
    except predict_module.NoModelError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.post("/api/annotate/folder/suggest")
def suggest_folder_annotation(req: FolderSuggestRequest):
    try:
        path = annotate_folder_module.image_path(_folder_config(), req.name)
    except annotate_folder_module.FolderError as e:
        raise HTTPException(404, str(e))
    return _suggest(path, None)  # folder images aren't in the database: nothing to save the guesses against


@app.post("/api/annotate/{image_id}/suggest")
def suggest_image_annotation(image_id: int):
    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    row = conn.execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if row is None or not Path(row["path"]).is_file():
        raise HTTPException(404, "No image file for that id.")
    return _suggest(Path(row["path"]), image_id)


@app.post("/api/annotate/{image_id}")
def submit_image_annotation(image_id: int, req: AnnotationSubmitRequest):
    schema = _load_attribute_schema()
    if req.schema_version and req.schema_version != annotate_module.schema_version(schema):
        raise HTTPException(409, "The attribute list changed since this image was opened.")

    conn = get_connection(DB_PATH)
    apply_migrations(conn)
    try:
        saved = annotate_module.submit_annotation(
            conn, image_id, req.annotator, req.values, schema, unusable=req.unusable
        )
    except annotate_module.AnnotationError as e:
        raise HTTPException(422, {"message": "Some attributes are missing or invalid.", "errors": e.errors})
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    return {"status": "ok", "values": saved}
