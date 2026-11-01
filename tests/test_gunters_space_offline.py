"""Offline tests for the Gunter's Space Page source. No network -- sitemap
text is passed in directly rather than fetched."""

import pytest

from scraper import license_gate
from scraper.sources import base
from scraper.sources import gunters_space as gs


def test_search_keywords_uses_alias_when_present():
    assert gs._search_keywords("GLONASS") == ["uragan"]


def test_search_keywords_derives_variants_from_a_multiword_name():
    keywords = gs._search_keywords("GPS IIR")
    assert "gps-iir" in keywords
    assert "gps_iir" in keywords
    assert "gpsiir" in keywords


def test_search_keywords_strips_parenthetical_and_slash_suffixes():
    keywords = gs._search_keywords("COSMOS 2403 (GLONASS)")
    assert "cosmos-2403" in keywords
    assert "cosmos" in keywords


def test_slug_matches_respects_word_boundaries():
    assert gs._slug_matches("sentinel", "sentinel-2a")
    assert not gs._slug_matches("sentinel", "biosentinel")


def test_slug_matches_prefix_alias():
    assert gs._slug_matches("fy-", "fy-3a-01")
    assert not gs._slug_matches("fy-", "unrelated")


def test_relevant_results_filters_by_keyword_and_noise_words():
    raw = {
        "family_name": "STARLINK",
        "urls": [
            "https://space.skyrocket.de/img_sat/starlink-1007.jpg",
            "https://space.skyrocket.de/img_sat/starlink-logo.jpg",
            "https://space.skyrocket.de/img_sat/iridium-33.jpg",
        ],
    }
    results = gs.relevant_results(raw, "STARLINK")
    urls = [r["url"] for r in results]
    assert "https://space.skyrocket.de/img_sat/starlink-1007.jpg" in urls
    assert "https://space.skyrocket.de/img_sat/starlink-logo.jpg" not in urls  # noise word
    assert "https://space.skyrocket.de/img_sat/iridium-33.jpg" not in urls  # wrong family


def test_extract_record_returns_none_while_disabled():
    assert gs.ENABLED is False
    raw_item = {"url": "https://space.skyrocket.de/img_sat/starlink-1007.jpg", "title": "starlink-1007.jpg"}
    assert gs.extract_record(raw_item, "STARLINK", "2026-01-01T00:00:00Z") is None


def test_extract_record_needs_only_enabled_and_marks_license_unverified(monkeypatch):
    monkeypatch.setattr(gs, "ENABLED", True)
    raw_item = {"url": "https://space.skyrocket.de/img_sat/starlink-1007.jpg", "title": "starlink-1007.jpg"}

    record = gs.extract_record(raw_item, "STARLINK", "2026-01-01T00:00:00Z")

    assert record is not None
    assert record.license_short_name == base.UNVERIFIED_LICENSE
    assert record.license_url is None
    result = license_gate.evaluate(record)
    assert result.accepted is True
    assert record.source_module == "gunters_space"


def test_make_session_refuses_while_disabled():
    assert gs.ENABLED is False
    with pytest.raises(RuntimeError, match="disabled"):
        gs.make_session()


def test_search_images_refuses_while_disabled():
    with pytest.raises(RuntimeError, match="disabled"):
        gs.search_images(session=None, conn=None, family_name="STARLINK")
