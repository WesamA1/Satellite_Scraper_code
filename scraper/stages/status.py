"""Phase 1 Task 7: the status command.

Per family: which sources have been tried (an image, or a no_images_found
rejection) vs untried, and an overall state. DB-driven port of
reference/coverage_report.py's three-state-per-source accounting (has images /
tried-no-result / untried), which previously read satellites.csv + flat
no_images_found*.txt files.
"""

import sqlite3

DEFAULT_SOURCES = ("wikimedia", "nasa")


def compute_status(
    conn: sqlite3.Connection, families: list[str], sources: tuple[str, ...] = DEFAULT_SOURCES
) -> list[dict]:
    image_counts = {}  # (family, source_module) -> count, any image status
    for row in conn.execute(
        """SELECT images.family AS family, sources.source_module AS source_module, COUNT(*) AS n
           FROM images JOIN sources ON sources.image_id = images.id
           GROUP BY images.family, sources.source_module"""
    ):
        image_counts[(row["family"], row["source_module"])] = row["n"]

    tried_no_result = set()  # (family, source_module) with a no_images_found rejection
    for row in conn.execute("SELECT family, source_module FROM rejections WHERE reason = 'no_images_found'"):
        tried_no_result.add((row["family"], row["source_module"]))

    results = []
    for family in families:
        per_source_counts = {s: image_counts.get((family, s), 0) for s in sources}
        total_images = sum(per_source_counts.values())

        tried = {s for s in sources if per_source_counts[s] > 0 or (family, s) in tried_no_result}
        untried = [s for s in sources if s not in tried]

        if untried:
            state = "pending"
        elif total_images > 0:
            state = "done"
        else:
            state = "failed"  # fully tried, zero images -- a yield failure, not an error

        results.append(
            {
                "family": family,
                "state": state,
                "image_count": total_images,
                "per_source": per_source_counts,
                "untried_sources": untried,
            }
        )

    return results
