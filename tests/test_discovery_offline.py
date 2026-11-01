"""Offline tests for Phase 2 discovery. No real Anthropic API calls -- a fake
client stands in, same dependency-injection pattern as the rest of the suite."""

import types

import pytest

from scraper import discovery
from scraper.db.connection import get_connection
from scraper.db.migrate import apply_migrations


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic() -- .messages.create() returns a
    canned response shaped like the real SDK's Message object (content blocks
    with a .type and .text)."""

    def __init__(self, response_text):
        self.response_text = response_text
        self.calls = []

        class _Messages:
            def create(_self, **kwargs):
                self.calls.append(kwargs)
                block = types.SimpleNamespace(type="text", text=self.response_text)
                return types.SimpleNamespace(content=[block])

        self.messages = _Messages()


CANDIDATES_JSON = """[
    {"name": "Openverse", "url": "https://openverse.org", "api_url": "https://api.openverse.org",
     "license_model": "CC-licensed aggregator", "license_verifiable": true,
     "notes": "Per-item license field in API response."},
    {"name": "Random Blog", "url": "https://example.com", "api_url": null,
     "license_model": "unclear", "license_verifiable": false,
     "notes": "No structured license data found."}
]"""


def test_find_candidate_sites_parses_json_from_response():
    client = FakeAnthropicClient(f"Here's what I found:\n{CANDIDATES_JSON}\nHope that helps!")

    candidates = discovery.find_candidate_sites("satellite imagery", client=client)

    assert len(candidates) == 2
    assert candidates[0]["name"] == "Openverse"
    assert candidates[0]["license_verifiable"] is True
    assert candidates[1]["license_verifiable"] is False


def test_find_candidate_sites_uses_web_search_tool_with_max_uses():
    client = FakeAnthropicClient(CANDIDATES_JSON)

    discovery.find_candidate_sites("birds", client=client, max_uses=3)

    call = client.calls[0]
    assert call["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    assert "birds" in call["messages"][0]["content"]


def test_find_candidate_sites_fails_clearly_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        discovery.find_candidate_sites("anything")  # no client injected, no key -- never reaches the network


def test_find_candidate_sites_raises_clearly_on_unparseable_response():
    client = FakeAnthropicClient("I couldn't find any JSON here, sorry.")

    with pytest.raises(ValueError, match="No JSON array found"):
        discovery.find_candidate_sites("anything", client=client)


def test_persist_candidates_writes_pending_rows(conn):
    import json

    candidates = json.loads(CANDIDATES_JSON)
    ids = discovery.persist_candidates(conn, "satellite imagery", candidates)

    assert len(ids) == 2
    rows = conn.execute("SELECT * FROM candidate_sources ORDER BY id").fetchall()
    assert rows[0]["name"] == "Openverse"
    assert rows[0]["status"] == "pending"
    assert rows[0]["license_verifiable"] == 1
    assert rows[1]["license_verifiable"] == 0
    assert rows[0]["prompt"] == "satellite imagery"


def test_load_seed_candidates_inserts_pending_rows(conn, tmp_path):
    seed_path = tmp_path / "seed_candidates.yaml"
    seed_path.write_text(
        "- name: Example Archive\n"
        "  url: https://example.org\n"
        "  api_url: null\n"
        "  license_model: CC0\n"
        "  license_verifiable: true\n"
        "  notes: test entry\n"
    )

    ids = discovery.load_seed_candidates(conn, path=seed_path)

    assert len(ids) == 1
    row = conn.execute("SELECT * FROM candidate_sources WHERE id = ?", (ids[0],)).fetchone()
    assert row["name"] == "Example Archive"
    assert row["status"] == "pending"
    assert row["prompt"] == "seed list"
    assert row["license_verifiable"] == 1


def test_load_seed_candidates_skips_urls_already_present(conn, tmp_path):
    seed_path = tmp_path / "seed_candidates.yaml"
    seed_path.write_text(
        "- name: Example Archive\n"
        "  url: https://example.org\n"
        "  api_url: null\n"
        "  license_model: CC0\n"
        "  license_verifiable: true\n"
        "  notes: test entry\n"
    )

    first = discovery.load_seed_candidates(conn, path=seed_path)
    second = discovery.load_seed_candidates(conn, path=seed_path)

    assert len(first) == 1
    assert second == []  # already present -- not duplicated


def test_real_seed_candidates_file_loads_without_error(conn):
    ids = discovery.load_seed_candidates(conn)
    assert len(ids) > 0

    rows = conn.execute("SELECT * FROM candidate_sources").fetchall()
    names = {row["name"] for row in rows}
    assert "Flickr (official agency photostreams)" in names
    assert all(row["status"] == "pending" for row in rows)


def test_add_manual_candidate_defaults_name_from_domain(conn):
    candidate_id = discovery.add_manual_candidate(conn, "https://sub.example.org/path?x=1")

    row = conn.execute("SELECT * FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
    assert row["name"] == "sub.example.org"
    assert row["status"] == "pending"
    assert row["prompt"] == "manual entry"
    assert row["license_verifiable"] == 0


def test_add_manual_candidate_uses_given_name_and_license_fields(conn):
    candidate_id = discovery.add_manual_candidate(
        conn,
        "https://example.org",
        name="Example Archive",
        license_model="CC0",
        license_verifiable=True,
        notes="Documented per-image API field.",
    )

    row = conn.execute("SELECT * FROM candidate_sources WHERE id = ?", (candidate_id,)).fetchone()
    assert row["name"] == "Example Archive"
    assert row["license_model"] == "CC0"
    assert row["license_verifiable"] == 1
    assert row["notes"] == "Documented per-image API field."


def test_seed_queue_is_idempotent(conn):
    discovery.seed_queue(conn, ["Family A", "Family B"])
    discovery.seed_queue(conn, ["Family A", "Family B", "Family C"])  # re-seed shouldn't reset existing rows

    discovery.mark_searched(conn, "Family A", candidate_count=2)
    discovery.seed_queue(conn, ["Family A", "Family B", "Family C"])

    row = conn.execute(
        "SELECT searched_at, candidate_count FROM discovery_queue WHERE family = ?", ("Family A",)
    ).fetchone()
    assert row["searched_at"] is not None
    assert row["candidate_count"] == 2

    rows = conn.execute("SELECT COUNT(*) AS n FROM discovery_queue").fetchone()
    assert rows["n"] == 3


def test_next_batch_returns_only_unsearched_families_in_order(conn):
    discovery.seed_queue(conn, ["Zeta", "Alpha", "Beta"])
    discovery.mark_searched(conn, "Alpha", candidate_count=0)

    batch = discovery.next_batch(conn, batch_size=5)

    assert batch == ["Beta", "Zeta"]


def test_next_batch_respects_batch_size(conn):
    discovery.seed_queue(conn, ["A", "B", "C"])

    batch = discovery.next_batch(conn, batch_size=2)

    assert len(batch) == 2


def test_queue_status_counts_searched_and_remaining(conn):
    discovery.seed_queue(conn, ["A", "B", "C"])
    discovery.mark_searched(conn, "A", candidate_count=1)

    status = discovery.queue_status(conn)

    assert status == {"total": 3, "searched": 1, "remaining": 2}


def test_review_candidate_approve_and_reject(conn):
    import json

    candidates = json.loads(CANDIDATES_JSON)
    ids = discovery.persist_candidates(conn, "satellite imagery", candidates)

    discovery.review_candidate(conn, ids[0], approved=True)
    discovery.review_candidate(conn, ids[1], approved=False)

    row0 = conn.execute("SELECT status, reviewed_at FROM candidate_sources WHERE id = ?", (ids[0],)).fetchone()
    row1 = conn.execute("SELECT status FROM candidate_sources WHERE id = ?", (ids[1],)).fetchone()
    assert row0["status"] == "approved"
    assert row0["reviewed_at"] is not None
    assert row1["status"] == "rejected"
