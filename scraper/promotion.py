"""Phase 2: the review/promotion gate -- the one path by which a generated
candidate module can ever become a real, running source.

validate_module_shape parses with `ast`, never `exec`/`import`, so checking an
unreviewed candidate's shape never runs a single line of it. promote() only
copies a file and appends to a YAML list; it never hot-loads anything into the
current process -- scraper.sources.registry's own import caching means a
promoted source only takes effect on the next process start (CLI invocation or
`scraper serve` restart), which is the deliberate "no hot-loading untrusted
code" behavior from the design.
"""

import ast
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scraper.sources.registry import ENABLED_SOURCES_PATH, REQUIRED_ATTRS

SOURCES_DIR = Path(__file__).parent / "sources"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_module_shape(source_text: str) -> list[str]:
    """Returns the list of required ImageSource names missing from source_text
    (empty = structurally complete). This is a shape check only -- it says
    nothing about correctness or whether license_short_name is actually
    trustworthy; a human still has to read the code."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError as e:
        return [f"syntax error: {e}"]

    defined_names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined_names.add(target.id)

    return [name for name in REQUIRED_ATTRS if name not in defined_names]


def promote(conn: sqlite3.Connection, candidate_id: int) -> Path:
    """Copies the candidate's generated file into scraper/sources/ and appends
    it to enabled_sources.yaml. Raises ValueError if the candidate has no
    generated code or fails the shape check -- never promotes a partial
    module."""
    row = conn.execute("SELECT * FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"No candidate with id {candidate_id}")
    if row["status"] != "code_generated":
        raise ValueError(
            f"Candidate {candidate_id} has status {row['status']!r}, not 'code_generated' -- "
            "it either has no reviewed draft yet or has already been promoted."
        )
    if not row["generated_path"]:
        raise ValueError(f"Candidate {candidate_id} has no generated code yet")

    generated_path = Path(row["generated_path"])
    if not generated_path.exists():
        raise ValueError(f"Candidate {candidate_id}'s generated file is missing on disk: {generated_path}")
    source_text = generated_path.read_text()

    missing = validate_module_shape(source_text)
    if missing:
        raise ValueError(f"Candidate {candidate_id}'s generated module is missing: {missing}")

    dest = SOURCES_DIR / generated_path.name
    if dest.exists():
        raise ValueError(
            f"{dest.name} already exists in scraper/sources/ -- rename the candidate "
            "or its generated file to avoid overwriting an existing source module."
        )
    shutil.copy2(generated_path, dest)

    enabled = yaml.safe_load(ENABLED_SOURCES_PATH.read_text()) or []
    module_name = dest.stem
    if module_name not in enabled:
        enabled.append(module_name)
        ENABLED_SOURCES_PATH.write_text(yaml.safe_dump(enabled))

    conn.execute(
        "UPDATE candidate_sources SET status = 'promoted', reviewed_at = ? WHERE id = ?",
        (_now(), candidate_id),
    )
    conn.commit()
    return dest
