"""The Status page's at-a-glance overview: what needs attention, where the
images are in the pipeline, recent scrape runs, how each source is doing,
and how many satellite families have images yet. Read-only."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper import secrets
from scraper.families import load_families
from scraper.sources import registry
from scraper.stages import scrape_runs

# Sources the project must never use (their sites prohibit AI/ML training).
OFF_LIMITS_SOURCES = {"gunters_space": "Gunter's Space Page"}
# A source with at least this many searches and no images is worth a look.
NEVER_FOUND_AFTER = 10
STALE_RUN_HOURS = 12


def _count(conn, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def pipeline(conn: sqlite3.Connection, training_set: Path) -> dict:
    """Active images by where they are now (each counted once), plus what's
    been set aside and why."""
    annotated = """EXISTS (SELECT 1 FROM annotations a WHERE a.image_id = images.id AND a.attribute != 'variant'
                           AND a.annotator != 'human_review')"""
    stages = {
        "waiting_for_filter": _count(
            conn,
            """SELECT COUNT(*) FROM images WHERE status = 'active' AND origin = 'scraped'
               AND filtered_at IS NULL AND reviewed_at IS NULL""",
        ),
        "waiting_for_review": _count(
            conn,
            """SELECT COUNT(*) FROM images WHERE status = 'active' AND reviewed_at IS NULL
               AND NOT (origin = 'scraped' AND filtered_at IS NULL)""",
        ),
        "kept_not_annotated": _count(
            conn,
            f"SELECT COUNT(*) FROM images WHERE status = 'active' AND reviewed_at IS NOT NULL AND NOT {annotated}",
        ),
        "annotated": _count(conn, f"SELECT COUNT(*) FROM images WHERE status = 'active' AND {annotated}"),
    }
    examples = 0
    if training_set.is_file():
        with open(training_set, encoding="utf-8") as f:
            examples = sum(1 for line in f if line.strip())
    set_aside = {
        (row[0] or "unrecorded"): row[1]
        for row in conn.execute("SELECT reject_reason, COUNT(*) FROM images WHERE status != 'active' GROUP BY 1")
    }
    return {**stages, "training_examples": examples, "set_aside": set_aside}


def sources(conn: sqlite3.Connection) -> list[dict]:
    enabled = registry.list_sources()
    stats = {
        row["source"]: dict(row)
        for row in conn.execute(
            """SELECT s.source_module AS source, COUNT(*) AS images, SUM(i.status = 'active') AS active,
                      SUM(i.reject_reason = 'not_an_image') AS not_an_image, MAX(s.retrieved_at) AS last_download,
                      COUNT(DISTINCT i.family) AS families
               FROM sources s JOIN images i ON i.id = s.image_id GROUP BY s.source_module"""
        )
    }
    empty = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source_module, COUNT(*) FROM rejections WHERE reason = 'no_images_found' GROUP BY 1"
        )
    }
    rows = []
    for name in sorted(set(enabled) | set(stats) | set(empty)):
        s = stats.get(name, {})
        module = enabled.get(name)
        needs_key = bool(module and getattr(module, "REQUIRES_API_KEY", False) and not secrets.get_source_api_key(name))
        searches = (s.get("families") or 0) + empty.get(name, 0)
        rows.append(
            {
                "source": name,
                "enabled": name in enabled,
                "off_limits": name in OFF_LIMITS_SOURCES,
                "needs_key": needs_key,
                "images": s.get("images") or 0,
                "active": s.get("active") or 0,
                "not_an_image": s.get("not_an_image") or 0,
                "last_download": s.get("last_download"),
                "empty_searches": empty.get(name, 0),
                "empty_share": empty.get(name, 0) / searches if searches else None,
            }
        )
    return rows


def coverage(conn: sqlite3.Connection, catalog: Path) -> dict:
    families = set(load_families(catalog)) if catalog.is_file() else set()
    with_images = {r[0] for r in conn.execute("SELECT DISTINCT family FROM images WHERE status = 'active'")}
    searched = {r[0] for r in conn.execute("SELECT DISTINCT family FROM rejections WHERE reason = 'no_images_found'")}
    searched |= {r[0] for r in conn.execute("SELECT DISTINCT family FROM images")}
    return {
        "families": len(families),
        "with_images": len(families & with_images),
        "searched_nothing_kept": len((families & searched) - with_images),
        "never_searched": len(families - searched),
    }


def _family_choices(conn: sqlite3.Connection) -> int:
    return _count(
        conn,
        """SELECT COUNT(*) FROM images a WHERE a.status = 'active' AND a.reviewed_at IS NULL
           AND a.content_sha256 IS NOT NULL AND EXISTS (
             SELECT 1 FROM images b WHERE b.content_sha256 = a.content_sha256 AND b.id != a.id
             AND b.status = 'active' AND b.family != a.family)""",
    )


def _runs(conn: sqlite3.Connection) -> list[dict]:
    runs = scrape_runs.recent(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALE_RUN_HOURS)).isoformat()
    for run in runs:
        # A run still "running" long after it started had its process stop
        # under it (app closed, machine slept) before it could record an end.
        if run["status"] == "running" and run["started_at"] < cutoff:
            run["status"] = "didnt_finish"
    return runs


def alerts(conn, stages: dict, source_rows: list[dict], runs: list[dict], model_file: Path | None) -> list[dict]:
    out = []
    for row in source_rows:
        if row["off_limits"] and row["enabled"]:
            out.append(
                {
                    "level": "bad",
                    "text": f"{OFF_LIMITS_SOURCES[row['source']]} is switched on. It's off-limits: the site prohibits "
                    "AI/ML training use. Set ENABLED = False in its source file.",
                    "link": None,
                }
            )
    if runs and runs[0]["status"] in ("failed", "didnt_finish"):
        what = "failed" if runs[0]["status"] == "failed" else "didn't finish"
        out.append({"level": "warn", "text": f"The last scrape {what}.", "link": "/scrape"})
    if stages["waiting_for_filter"]:
        out.append(
            {
                "level": "warn",
                "text": f"{stages['waiting_for_filter']} scraped image(s) haven't been through the filter yet.",
                "link": "/filter",
            }
        )
    for row in source_rows:
        if (
            row["enabled"]
            and not row["off_limits"]
            and not row["images"]
            and row["empty_searches"] >= NEVER_FOUND_AFTER
        ):
            out.append(
                {
                    "level": "warn",
                    "text": f"{row['source']} has never found an image ({row['empty_searches']} searches).",
                    "link": None,
                }
            )
        if row["enabled"] and row["needs_key"]:
            out.append({"level": "warn", "text": f"{row['source']} needs an API key to run.", "link": "/api-keys"})
    choices = _family_choices(conn)
    if choices:
        out.append(
            {"level": "info", "text": f"{choices} image(s) in Review need their family picked.", "link": "/review"}
        )
    if stages["waiting_for_review"]:
        out.append(
            {"level": "info", "text": f"{stages['waiting_for_review']} image(s) waiting for review.", "link": "/review"}
        )
    if model_file is not None:
        trained = datetime.fromtimestamp(model_file.stat().st_mtime, timezone.utc).isoformat()
        newer = _count(
            conn,
            """SELECT COUNT(DISTINCT image_id) FROM annotations WHERE created_at > ? AND attribute != 'variant'
               AND annotator NOT IN ('human_review', 'legacy_miat')""",
            (trained,),
        )
        if newer:
            out.append(
                {"level": "info", "text": f"{newer} image(s) annotated since the model was trained.", "link": "/train"}
            )
    return out


def build(
    conn: sqlite3.Connection,
    catalog: Path = Path("satellites.csv"),
    training_dir: Path = Path("training"),
) -> dict:
    from scraper.ml import predict

    stages = pipeline(conn, training_dir / "dataset.jsonl")
    source_rows = sources(conn)
    runs = _runs(conn)
    return {
        "alerts": alerts(conn, stages, source_rows, runs, predict.model_path(training_dir)),
        "pipeline": stages,
        "runs": runs,
        "sources": source_rows,
        "coverage": coverage(conn, catalog),
    }


if __name__ == "__main__":
    from scraper.db.connection import get_connection

    print(json.dumps(build(get_connection(Path("scraper.db"))), indent=2, default=str))
