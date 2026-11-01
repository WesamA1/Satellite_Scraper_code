"""Offline tests for Phase 2 promotion -- the review gate. validate_module_shape
uses ast.parse only, never exec/import, so these tests never run a line of the
"generated" fixture code."""

import sys

import pytest
import yaml

from scraper import promotion
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.discovery import persist_candidates

COMPLETE_MODULE = (
    "SOURCE_MODULE = 'fixture_promoted'\n"
    "def make_session(dev=False): pass\n"
    "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
    "def relevant_results(raw, query, domain_keywords=None): return []\n"
    "def extract_record(raw_item, query, retrieved_at): return None\n"
    "def download_image(session, conn, url, dest_path, sleep_fn=None): return False\n"
)

INCOMPLETE_MODULE = (
    "SOURCE_MODULE = 'fixture_incomplete'\n"
    "def make_session(dev=False): pass\n"
    "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
)

BROKEN_SYNTAX_MODULE = "def make_session(dev=False:\n    pass\n"  # missing closing paren


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def test_validate_module_shape_passes_a_complete_module():
    assert promotion.validate_module_shape(COMPLETE_MODULE) == []


def test_validate_module_shape_lists_missing_functions():
    missing = promotion.validate_module_shape(INCOMPLETE_MODULE)
    assert "relevant_results" in missing
    assert "extract_record" in missing
    assert "download_image" in missing
    assert "make_session" not in missing


def test_validate_module_shape_reports_syntax_errors_without_crashing():
    missing = promotion.validate_module_shape(BROKEN_SYNTAX_MODULE)
    assert any("syntax error" in m for m in missing)


def test_promote_requires_generated_code(conn):
    ids = persist_candidates(conn, "prompt", [{"name": "NoCodeYet", "url": "https://x.example"}])
    with pytest.raises(ValueError, match="not 'code_generated'"):
        promotion.promote(conn, ids[0])


def test_promote_rejects_an_incomplete_module(conn, tmp_path):
    ids = persist_candidates(conn, "prompt", [{"name": "Incomplete", "url": "https://x.example"}])
    candidate_id = ids[0]
    draft_path = tmp_path / "incomplete.py"
    draft_path.write_text(INCOMPLETE_MODULE)
    conn.execute(
        "UPDATE candidate_sources SET generated_path = ?, status = 'code_generated' WHERE id = ?",
        (str(draft_path), candidate_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="missing"):
        promotion.promote(conn, candidate_id)

    # Must not have copied anything into the real sources/ directory.
    assert not (promotion.SOURCES_DIR / "incomplete.py").exists()


def test_promote_copies_file_and_registers_in_enabled_sources(conn, tmp_path, monkeypatch):
    enabled_path = tmp_path / "enabled_sources.yaml"
    enabled_path.write_text("[]")
    monkeypatch.setattr(promotion, "ENABLED_SOURCES_PATH", enabled_path)

    ids = persist_candidates(conn, "prompt", [{"name": "Fixture Promoted", "url": "https://x.example"}])
    candidate_id = ids[0]
    draft_path = tmp_path / "fixture_promoted.py"
    draft_path.write_text(COMPLETE_MODULE)
    conn.execute(
        "UPDATE candidate_sources SET generated_path = ?, status = 'code_generated' WHERE id = ?",
        (str(draft_path), candidate_id),
    )
    conn.commit()

    try:
        dest = promotion.promote(conn, candidate_id)

        assert dest.exists()
        assert dest.read_text() == COMPLETE_MODULE
        assert yaml.safe_load(enabled_path.read_text()) == ["fixture_promoted"]

        row = conn.execute("SELECT status, reviewed_at FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
        assert row["status"] == "promoted"
        assert row["reviewed_at"] is not None
    finally:
        (promotion.SOURCES_DIR / "fixture_promoted.py").unlink(missing_ok=True)
        sys.modules.pop("scraper.sources.fixture_promoted", None)


def test_promote_raises_for_unknown_candidate(conn):
    with pytest.raises(ValueError, match="No candidate"):
        promotion.promote(conn, 999)


def test_promote_refuses_to_overwrite_an_existing_source_module(conn, tmp_path):
    """A candidate's generated filename colliding with an existing file in
    scraper/sources/ (a built-in, or a previously-promoted different
    candidate) must not be silently overwritten."""
    ids = persist_candidates(conn, "prompt", [{"name": "Wikimedia", "url": "https://x.example"}])
    candidate_id = ids[0]
    draft_path = tmp_path / "wikimedia.py"
    draft_path.write_text(COMPLETE_MODULE)
    conn.execute(
        "UPDATE candidate_sources SET generated_path = ?, status = 'code_generated' WHERE id = ?",
        (str(draft_path), candidate_id),
    )
    conn.commit()

    with pytest.raises(ValueError, match="already exists"):
        promotion.promote(conn, candidate_id)


def test_promote_refuses_to_run_twice_on_the_same_candidate(conn, tmp_path, monkeypatch):
    """Once a candidate is 'promoted', re-invoking promote() (e.g. a direct
    API call bypassing the UI, which only shows Promote for code_generated
    rows) must not re-copy and overwrite the already-live file."""
    enabled_path = tmp_path / "enabled_sources.yaml"
    enabled_path.write_text("[]")
    monkeypatch.setattr(promotion, "ENABLED_SOURCES_PATH", enabled_path)

    ids = persist_candidates(conn, "prompt", [{"name": "Fixture Promoted Twice", "url": "https://x.example"}])
    candidate_id = ids[0]
    draft_path = tmp_path / "fixture_promoted_twice.py"
    draft_path.write_text(COMPLETE_MODULE.replace("fixture_promoted", "fixture_promoted_twice"))
    conn.execute(
        "UPDATE candidate_sources SET generated_path = ?, status = 'code_generated' WHERE id = ?",
        (str(draft_path), candidate_id),
    )
    conn.commit()

    try:
        promotion.promote(conn, candidate_id)
        with pytest.raises(ValueError, match="not 'code_generated'"):
            promotion.promote(conn, candidate_id)
    finally:
        (promotion.SOURCES_DIR / "fixture_promoted_twice.py").unlink(missing_ok=True)
        sys.modules.pop("scraper.sources.fixture_promoted_twice", None)
