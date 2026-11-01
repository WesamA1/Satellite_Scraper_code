"""Offline tests for Phase 2 codegen. No real Anthropic API calls, and
generate_source_module never writes/imports/executes anything by itself."""

import types

import pytest

from scraper import codegen
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations
from scraper.discovery import persist_candidates


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeAnthropicClient:
    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

        class _Messages:
            def create(_self, **kwargs):
                self.calls.append(kwargs)
                block = types.SimpleNamespace(type="text", text=self.response_text)
                return types.SimpleNamespace(content=[block])

        self.messages = _Messages()


FAKE_MODULE_SOURCE = (
    "SOURCE_MODULE = 'openverse'\n"
    "def make_session(dev=False): pass\n"
    "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
    "def relevant_results(raw, query, domain_keywords=None): return []\n"
    "def extract_record(raw_item, query, retrieved_at): return None\n"
    "def download_image(session, conn, url, dest_path, sleep_fn=None): return False\n"
)


def test_generate_source_module_fails_clearly_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        codegen.generate_source_module({"name": "X", "url": "https://x.example"})


def test_generate_source_module_returns_text_without_side_effects(tmp_path, monkeypatch):
    client = FakeAnthropicClient(FAKE_MODULE_SOURCE)

    result = codegen.generate_source_module({"name": "Openverse", "url": "https://openverse.org"}, client=client)

    assert "SOURCE_MODULE = 'openverse'" in result
    call = client.calls[0]
    assert "Openverse" in call["messages"][0]["content"]
    assert "base.py" in call["system"] or "SOURCE_MODULE" in call["system"]


def test_generate_source_module_system_prompt_includes_license_safety_rule():
    client = FakeAnthropicClient(FAKE_MODULE_SOURCE)
    codegen.generate_source_module({"name": "X", "url": "https://x.example"}, client=client)

    system_prompt = client.calls[0]["system"]
    assert "empty string" in system_prompt
    assert "license_short_name" in system_prompt


def test_write_candidate_module_writes_to_candidates_dir_and_updates_row(conn):
    ids = persist_candidates(
        conn,
        "test prompt",
        [{"name": "Openverse", "url": "https://openverse.org", "license_verifiable": True}],
    )
    candidate_id = ids[0]

    # Writes into the real scraper/sources/candidates/ (gitignored, drafts only)
    # -- cleaned up in the finally block below.
    path = codegen.write_candidate_module(conn, candidate_id, FAKE_MODULE_SOURCE, "Openverse")

    try:
        assert path.exists()
        assert path.read_text() == FAKE_MODULE_SOURCE
        assert path.parent.name == "candidates"

        row = conn.execute(
            "SELECT generated_path, status FROM candidate_sources WHERE id = ?", (candidate_id,)
        ).fetchone()
        assert row["generated_path"] == str(path)
        assert row["status"] == "code_generated"
    finally:
        path.unlink(missing_ok=True)


def test_write_candidate_module_does_not_overwrite_a_different_candidates_draft(conn):
    """Two candidates whose names slugify the same must not collide -- the
    second write gets a numbered suffix instead of clobbering the first."""
    ids = persist_candidates(
        conn,
        "test prompt",
        [
            {"name": "ESA Image Gallery", "url": "https://esa.example"},
            {"name": "esa-image-gallery", "url": "https://esa-mirror.example"},
        ],
    )
    first_id, second_id = ids

    first_path = codegen.write_candidate_module(conn, first_id, "FIRST = 1\n", "ESA Image Gallery")
    second_path = codegen.write_candidate_module(conn, second_id, "SECOND = 2\n", "esa-image-gallery")

    try:
        assert first_path != second_path
        assert first_path.read_text() == "FIRST = 1\n"
        assert second_path.read_text() == "SECOND = 2\n"
    finally:
        first_path.unlink(missing_ok=True)
        second_path.unlink(missing_ok=True)


def test_write_candidate_module_regenerate_reuses_its_own_path(conn):
    """Re-running codegen for the SAME candidate (the "Regenerate" button)
    must overwrite its own draft in place, not hop to a new filename."""
    ids = persist_candidates(conn, "test prompt", [{"name": "Openverse", "url": "https://openverse.org"}])
    candidate_id = ids[0]

    first_path = codegen.write_candidate_module(conn, candidate_id, "FIRST = 1\n", "Openverse")
    second_path = codegen.write_candidate_module(conn, candidate_id, "SECOND = 2\n", "Openverse")

    try:
        assert first_path == second_path
        assert second_path.read_text() == "SECOND = 2\n"
    finally:
        second_path.unlink(missing_ok=True)
