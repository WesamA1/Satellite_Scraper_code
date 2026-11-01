"""Offline tests for Phase 2's source registry. No network -- just import/shape
checks against fixture modules written to a temp dir."""

import sys
import types

import pytest

from scraper.sources import registry


def test_builtins_are_always_registered():
    sources = registry.list_sources()
    assert "wikimedia" in sources
    assert "nasa" in sources


def test_get_source_returns_the_right_module():
    assert registry.get_source("wikimedia").SOURCE_MODULE == "wikimedia"


def test_get_source_raises_for_unknown_name():
    with pytest.raises(ValueError, match="Unknown source module"):
        registry.get_source("does_not_exist")


def test_disabled_source_is_excluded_from_list_sources():
    from scraper.sources import gunters_space

    assert gunters_space.ENABLED is False
    assert "gunters_space" not in registry.list_sources()


def test_get_source_raises_a_clear_error_for_a_disabled_source():
    with pytest.raises(ValueError, match="disabled"):
        registry.get_source("gunters_space")


def test_enabling_a_source_makes_it_listed(monkeypatch):
    from scraper.sources import gunters_space

    monkeypatch.setattr(gunters_space, "ENABLED", True)
    assert "gunters_space" in registry.list_sources()
    assert registry.get_source("gunters_space").SOURCE_MODULE == "gunters_space"


def test_validate_shape_flags_missing_attrs():
    incomplete = types.SimpleNamespace(SOURCE_MODULE="x", make_session=lambda: None)
    missing = registry.validate_shape(incomplete)
    assert "search_images" in missing
    assert "relevant_results" in missing
    assert "extract_record" in missing
    assert "download_image" in missing
    assert "SOURCE_MODULE" not in missing
    assert "make_session" not in missing


def test_validate_shape_passes_a_complete_module():
    complete = types.SimpleNamespace(
        SOURCE_MODULE="x",
        make_session=lambda: None,
        search_images=lambda: None,
        relevant_results=lambda: None,
        extract_record=lambda: None,
        download_image=lambda: None,
    )
    assert registry.validate_shape(complete) == []


def test_promoted_source_is_loaded_when_well_formed(tmp_path, monkeypatch):
    module_code = (
        "SOURCE_MODULE = 'fixture_source'\n"
        "def make_session(dev=False): pass\n"
        "def search_images(session, conn, query, sleep_fn=None, domain_hint=None): pass\n"
        "def relevant_results(raw, query, domain_keywords=None): return []\n"
        "def extract_record(raw_item, query, retrieved_at): return None\n"
        "def download_image(session, conn, url, dest_path, sleep_fn=None): return False\n"
    )
    module_path = __import__("pathlib").Path(__import__("scraper.sources").sources.__path__[0]) / "fixture_source.py"
    module_path.write_text(module_code)
    monkeypatch.setattr(registry, "ENABLED_SOURCES_PATH", _write_enabled(tmp_path, ["fixture_source"]))

    try:
        sys.modules.pop("scraper.sources.fixture_source", None)
        sources = registry.list_sources()
        assert "fixture_source" in sources
    finally:
        module_path.unlink(missing_ok=True)
        sys.modules.pop("scraper.sources.fixture_source", None)


def test_malformed_promoted_source_is_skipped_not_crashed(tmp_path, monkeypatch, caplog):
    module_code = "SOURCE_MODULE = 'broken_source'\ndef make_session(dev=False): pass\n"
    module_path = __import__("pathlib").Path(__import__("scraper.sources").sources.__path__[0]) / "broken_source.py"
    module_path.write_text(module_code)
    monkeypatch.setattr(registry, "ENABLED_SOURCES_PATH", _write_enabled(tmp_path, ["broken_source"]))

    try:
        sys.modules.pop("scraper.sources.broken_source", None)
        sources = registry.list_sources()
        assert "broken_source" not in sources
        assert "wikimedia" in sources  # built-ins still fine
    finally:
        module_path.unlink(missing_ok=True)
        sys.modules.pop("scraper.sources.broken_source", None)


def _write_enabled(tmp_path, names):
    import yaml

    path = tmp_path / "enabled_sources.yaml"
    path.write_text(yaml.safe_dump(names))
    return path
