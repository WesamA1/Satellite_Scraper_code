"""Offline tests for scraper/secrets.py's per-source API key convention."""

import types

from scraper import secrets


def test_source_with_no_key_requirement_is_always_ready():
    module = types.SimpleNamespace(SOURCE_MODULE="wikimedia")
    assert secrets.is_source_ready("wikimedia", module) is True


def test_source_requiring_a_key_is_not_ready_when_unset(monkeypatch):
    monkeypatch.delenv("SOURCE_FLICKR_API_KEY", raising=False)
    module = types.SimpleNamespace(SOURCE_MODULE="flickr", REQUIRES_API_KEY=True)
    assert secrets.is_source_ready("flickr", module) is False


def test_source_requiring_a_key_is_ready_once_configured(monkeypatch):
    monkeypatch.setenv("SOURCE_FLICKR_API_KEY", "fake-key")
    module = types.SimpleNamespace(SOURCE_MODULE="flickr", REQUIRES_API_KEY=True)
    assert secrets.is_source_ready("flickr", module) is True
