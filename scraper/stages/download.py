"""Phase 1 Task 1 + Phase 2: the download stage.

Searches each source for a query (originally always a satellite family name;
Phase 2 generalizes this to any prompt via domain_hint), runs the relevance
filter, then the license gate (Task 3), and downloads only accepted,
not-already-fetched items. Resume and negative-cache checks go through the DB
(scraper.license_gate), replacing the old filename-substring/flat-text-file
approach.

Phase 2: dispatch is registry-driven (scraper.sources.registry) instead of a
hardcoded if/elif, so a promoted source works here with no changes to this
file -- it only has to satisfy the ImageSource contract in
scraper/sources/base.py.

Each source's own download_image only backs off on an explicit 429 (rate_limiter's
recorded cooldown); it has no reason to pace requests otherwise, and no reason to
notice a run of failures from a *different* cause (403s, timeouts, DNS errors).
_process_results adds that layer here, once, for every source: a short pace after
every download attempt, and a longer pause after a run of consecutive failures.
"""

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scraper import license_gate, master
from scraper.ml.dataset import portable_path
from scraper.sources import registry
from scraper.stages import review

SOURCES = ("wikimedia", "nasa")

# Politeness pacing between downloads, independent of each source's own
# per-request 429 handling -- a host returning steady 403/500/connection
# errors (never a 429) would otherwise get hit once per image with zero
# backoff. Same shape as the reference implementation this was ported from:
# a short pace after every download, a long pause after a run of failures.
DOWNLOAD_PACE_SECONDS = 2
CONSECUTIVE_FAILURE_LIMIT = 3
CONSECUTIVE_FAILURE_PAUSE_SECONDS = 120


QUARANTINE_DIR = Path("_quarantine")


def _is_image(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_dest(conn: sqlite3.Connection, dest: Path) -> Path:
    """`dest`, or the next free name after it (..._03_2.jpg, ..._03_3.jpg).

    Names come from an item's position in the search results, and a site can
    return its results in a different order next time -- so a new image can
    get the same name as one already saved. Downloading straight to that name
    used to overwrite the saved file (leaving its database record, license
    included, pointing at a different picture) before the database refused
    the duplicate path and stopped the run."""
    candidate, n = dest, 2
    while (
        candidate.exists()
        or conn.execute(
            "SELECT 1 FROM images WHERE path IN (?, ?)", (portable_path(candidate), str(candidate))
        ).fetchone()
    ):
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
    return candidate


def _download_from_source(
    conn: sqlite3.Connection,
    session,
    source_module: str,
    family_name: str,
    image_dir: Path,
    limit: int | None,
    sleep_fn,
    domain_hint: str | None,
    cancel: Optional[threading.Event] = None,
    on_item_progress=None,
) -> dict:
    module = registry.get_source(source_module)

    raw = module.search_images(session, conn, family_name, sleep_fn=sleep_fn, domain_hint=domain_hint)
    domain_keywords = None if domain_hint else []
    relevant = module.relevant_results(raw, family_name, domain_keywords=domain_keywords)

    return _process_results(
        conn,
        family_name,
        source_module,
        relevant,
        extract=lambda raw_item, retrieved_at: module.extract_record(raw_item, family_name, retrieved_at),
        download=lambda record, dest: module.download_image(
            session, conn, record.download_url, dest, sleep_fn=sleep_fn
        ),
        image_dir=image_dir,
        limit=limit,
        sleep_fn=sleep_fn,
        cancel=cancel,
        on_item_progress=on_item_progress,
    )


def _process_results(
    conn,
    family_name,
    source_module,
    relevant_raw_items,
    extract,
    download,
    image_dir,
    limit=None,
    sleep_fn=time.sleep,
    cancel: Optional[threading.Event] = None,
    on_item_progress=None,
) -> dict:
    counts = {
        "downloaded": 0,
        "rejected_no_license": 0,
        "skipped_already_fetched": 0,
        "download_failed": 0,
        "not_an_image": 0,
        "rejected_before": 0,
        "already_in_master_set": 0,
    }

    if not relevant_raw_items:
        conn.execute(
            """INSERT INTO rejections (family, source_module, external_id, reason, detail, rejected_at)
               VALUES (?, ?, NULL, 'no_images_found', NULL, ?)""",
            (family_name, source_module, _now()),
        )
        conn.commit()
        return counts

    if limit is not None:
        relevant_raw_items = relevant_raw_items[:limit]

    consecutive_failures = 0
    for idx, raw_item in enumerate(relevant_raw_items, start=1):
        if cancel is not None and cancel.is_set():
            break

        if on_item_progress:
            on_item_progress(idx, len(relevant_raw_items))

        record = extract(raw_item, _now())
        if record is None:
            continue

        if license_gate.is_already_fetched(conn, source_module, record.external_id):
            counts["skipped_already_fetched"] += 1
            continue

        result = license_gate.evaluate(record)
        if not result.accepted:
            license_gate.persist(conn, record, result, path="")
            counts["rejected_no_license"] += 1
            continue

        ext = Path(record.download_url.split("?")[0]).suffix or ".jpg"
        dest = _free_dest(conn, image_dir / family_name / f"{family_name}_{source_module}_{idx:02d}{ext}")
        if download(record, dest):
            image_id = license_gate.persist(conn, record, result, path=portable_path(dest))
            if _is_image(dest):
                # Checked against the master ledger straight away: a picture
                # rejected before (from any source) is set aside again, and one
                # already in the master set is recorded as another source for
                # it instead of being stored twice.
                verdict = master.check_arrival(conn, image_id, QUARANTINE_DIR)
                if verdict == "rejected_before":
                    counts["rejected_before"] += 1
                elif verdict in ("already_kept", "already_waiting"):
                    counts["already_in_master_set"] += 1
                else:
                    counts["downloaded"] += 1
            else:
                # Some archives hand back a PDF or a web page instead of the
                # picture. Keep the record (so it isn't fetched again) but set
                # the file aside, out of Review and everything after it.
                review.quarantine_image(conn, image_id, "rejected_filter", QUARANTINE_DIR, "not_an_image")
                counts["not_an_image"] += 1
            consecutive_failures = 0
        else:
            # No sources row was created, so this item is retried on the next run.
            counts["download_failed"] += 1
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                sleep_fn(CONSECUTIVE_FAILURE_PAUSE_SECONDS)
                consecutive_failures = 0
                continue

        sleep_fn(DOWNLOAD_PACE_SECONDS)

    return counts


def download_family(
    conn: sqlite3.Connection,
    session,
    family_name: str,
    image_dir: Path = Path("satellite_images"),
    sources: tuple[str, ...] = SOURCES,
    limit: int | None = None,
    sleep_fn=time.sleep,
    domain_hint: str | None = "satellite",
    cancel: Optional[threading.Event] = None,
    on_source_start=None,
    on_item_progress=None,
    on_source_done=None,
) -> dict:
    """Run the download stage for one query across the given sources.

    `session` is a single requests.Session (or test double) shared across
    sources/calls -- callers are responsible for logging in before calling this
    for wikimedia. `limit` caps how many relevance-filtered items are processed
    per family per source (used by the CLI's --limit/profile image_limit).
    `sleep_fn` is forwarded to the rate limiter (tests inject a fake one).
    `domain_hint` defaults to "satellite" (today's behavior, unchanged) --
    pass None/"" for a domain-agnostic prompt search (Phase 2): no suffix
    appended to the search query, and relevance filtering falls back to pure
    term-matching instead of each source's domain-specific keyword list.

    `cancel` is checked between sources (and, inside _process_results,
    between items) -- run_scrape_job's own cancel check only runs between
    *families*, which isn't fine-grained enough once a single family can
    span many sources each with their own pacing/circuit-breaker pauses;
    without this, Stop could take many minutes to actually take effect.

    `on_source_start(source_module, index, total)` fires right before each
    source is attempted (1-indexed) -- lets a caller show "currently on
    <family>: <source> (3/8)" instead of just a family-level counter that
    can look frozen for minutes while one slow/stuck source is in progress.

    `on_item_progress(source_module, item_index, item_total)` fires for each
    relevant item within the *current* source -- a source with many items
    looked identical whether it was slow-but-working or truly stuck, since
    on_source_start only fires once per source, not once per item.

    `on_source_done(source_module, downloaded)` fires right after each source
    finishes (however many items it actually downloaded, 0 included) -- a
    caller that only totals images found once per *family* (run_scrape_job's
    own on_progress) would otherwise show a stale total for as long as the
    current family's remaining sources take, even while real images keep
    landing on disk from the sources already done.
    """
    results = {}
    for index, source_module in enumerate(sources, start=1):
        if cancel is not None and cancel.is_set():
            break

        if on_source_start:
            on_source_start(source_module, index, len(sources))

        if license_gate.has_negative_cache(conn, family_name, source_module):
            results[source_module] = {"skipped": "negative_cache"}
            if on_source_done:
                on_source_done(source_module, 0)
            continue

        def _item_progress(item_index, item_total):
            if on_item_progress:
                on_item_progress(source_module, item_index, item_total)

        results[source_module] = _download_from_source(
            conn,
            session,
            source_module,
            family_name,
            image_dir,
            limit,
            sleep_fn,
            domain_hint,
            cancel=cancel,
            on_item_progress=_item_progress,
        )
        if on_source_done:
            on_source_done(source_module, results[source_module].get("downloaded", 0))

    return results
