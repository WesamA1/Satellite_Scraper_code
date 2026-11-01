"""Phase 2: drafting a new source module for an approved candidate, via Claude.

generate_source_module() never writes a file or imports anything -- it returns
text. write_candidate_module() is the only function that touches disk, and it
writes to scraper/sources/candidates/ (gitignored, never imported by the
registry), not scraper/sources/ directly. Promotion into the real, running
scraper happens only through scraper/promotion.py, after a human reads the
code.

No real API call happens from importing or testing this module, same pattern
as scraper/discovery.py -- `client` is lazily constructed only if not injected.
"""

import os
import re
import sqlite3
from pathlib import Path

CODEGEN_MODEL = os.environ.get("ANTHROPIC_CODEGEN_MODEL", "claude-sonnet-5")

BASE_PY = Path(__file__).parent / "sources" / "base.py"
EXAMPLE_MODULE = Path(__file__).parent / "sources" / "nasa_images.py"

SYSTEM_PROMPT_TEMPLATE = """You write Python modules implementing a specific scraper \
plugin interface. Study the contract and the worked example below, then write a NEW \
module for the given candidate site following the exact same conventions.

Required top-level names (see the contract for exact signatures):
SOURCE_MODULE, make_session, search_images, relevant_results, extract_record, \
download_image.

Use scraper.rate_limiter the same way the example does (wait_for_cooldown before each \
request, record_cooldown on a 429, honoring Retry-After when present). Use \
scraper.sources.base.ImageRecord for extract_record's return type.

extract_record's license_short_name must be populated only from real, structured \
per-item license data this site's own API/page actually returns. If you cannot find \
a machine-readable per-item license field for this site, leave license_short_name as \
an empty string rather than guessing or assuming a license -- a human reviews this \
code before it's promoted, and that review depends on this field being honest, not a \
plausible-looking guess.

Output ONLY the Python source code for the new module. No prose, no markdown fences.

--- contract (scraper/sources/base.py) ---
{base_py}

--- worked example (scraper/sources/nasa_images.py) ---
{example_module}
"""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "candidate"


def generate_source_module(candidate: dict, client=None) -> str:
    """Returns the drafted module's source as a string. Writes nothing, imports
    nothing, executes nothing -- purely a text-generation call."""
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set in .env -- codegen needs a Console API key "
                "(see .env.example). This feature is optional; core scraping doesn't need it."
            )
        import anthropic

        client = anthropic.Anthropic()

    system = SYSTEM_PROMPT_TEMPLATE.format(
        base_py=BASE_PY.read_text(),
        example_module=EXAMPLE_MODULE.read_text(),
    )

    response = client.messages.create(
        model=CODEGEN_MODEL,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": f"Write a module for this candidate site:\n{candidate}"}],
    )

    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def _candidate_module_path(conn: sqlite3.Connection, candidates_dir: Path, slug: str, candidate_id: int) -> Path:
    """Picks a draft path for this candidate, appending a numeric suffix if the
    plain slug is already claimed by a *different* candidate -- two candidates
    with names that slugify the same (e.g. "ESA Image Gallery" vs
    "esa-image-gallery") would otherwise silently overwrite each other's
    draft, with both DB rows left pointing at the survivor's file. Reusing
    this candidate's own existing path (if it has one) keeps "Regenerate"
    overwriting only its own draft, not hopping to a new filename."""
    suffix = 0
    while True:
        candidate_slug = slug if suffix == 0 else f"{slug}_{suffix}"
        path = candidates_dir / f"{candidate_slug}.py"
        owner = conn.execute("SELECT id FROM candidate_sources WHERE generated_path = ?", (str(path),)).fetchone()
        if owner is None or owner["id"] == candidate_id:
            return path
        suffix += 1


def write_candidate_module(conn: sqlite3.Connection, candidate_id: int, source_text: str, candidate_name: str) -> Path:
    """Writes the drafted module to scraper/sources/candidates/<slug>.py
    (gitignored, never imported by the registry) and records the path against
    the candidate row. This is the only disk-writing step in the codegen
    pipeline -- the file still has to be read and promoted before it's real."""
    candidates_dir = Path(__file__).parent / "sources" / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(candidate_name)
    path = _candidate_module_path(conn, candidates_dir, slug, candidate_id)
    path.write_text(source_text)

    conn.execute(
        "UPDATE candidate_sources SET generated_path = ?, status = 'code_generated' WHERE id = ?",
        (str(path), candidate_id),
    )
    conn.commit()
    return path
