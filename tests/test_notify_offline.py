"""Offline tests for Phase 1 Task 8: notify(). No real HTTP -- post_fn is injected."""

from scraper import notify


def test_no_op_when_nothing_configured(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NTFY_URL", raising=False)

    calls = []
    sent = notify.notify("hello", post_fn=lambda *a, **k: calls.append((a, k)))

    assert sent is False
    assert calls == []


def test_posts_to_discord_when_configured(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.delenv("NTFY_URL", raising=False)

    calls = []
    sent = notify.notify("hello", post_fn=lambda url, **k: calls.append((url, k)))

    assert sent is True
    assert len(calls) == 1
    assert calls[0][0] == "https://discord.example/webhook"
    assert calls[0][1]["json"] == {"content": "hello"}


def test_posts_to_both_when_both_configured(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example/topic")

    calls = []
    sent = notify.notify("hello", post_fn=lambda url, **k: calls.append(url))

    assert sent is True
    assert calls == ["https://discord.example/webhook", "https://ntfy.example/topic"]


def test_failed_post_does_not_raise(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.delenv("NTFY_URL", raising=False)

    def boom(*a, **k):
        raise ConnectionError("no network")

    sent = notify.notify("hello", post_fn=boom)
    assert sent is False
