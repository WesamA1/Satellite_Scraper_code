"""Phase 2: per-source API key convention.

Same .env/python-dotenv pattern as every other credential in this project
(SPACETRACK_*, WIKIMEDIA_BOT_*, ANTHROPIC_API_KEY) -- no new secret-storage
mechanism. A promoted source that needs its own key reads it via this helper
rather than inventing its own env var name.
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv, set_key

load_dotenv()

# The exact credential keys this project knows about -- same list doctor.py
# checks (REQUIRED/OPTIONAL_CREDENTIAL_KEYS) plus the notification webhooks
# from .env.example. The web GUI's key-write endpoint only ever writes one of
# these, or a SOURCE_<NAME>_API_KEY for a currently-registered source -- never
# an arbitrary key name typed into a form.
BASE_ENV_KEYS = (
    "SPACETRACK_USERNAME",
    "SPACETRACK_PASSWORD",
    "WIKIMEDIA_BOT_USERNAME",
    "WIKIMEDIA_BOT_PASSWORD",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "DISCORD_WEBHOOK_URL",
    "NTFY_URL",
)


def _env_key_for(source_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", source_name.upper()).strip("_")
    return f"SOURCE_{normalized}_API_KEY"


def get_source_api_key(source_name: str) -> str | None:
    """Reads SOURCE_<NAME>_API_KEY for source_name. Returns None if unset --
    callers decide whether that's fatal (most sources need no key at all)."""
    value = os.environ.get(_env_key_for(source_name), "").strip()
    return value or None


def is_source_ready(source_name: str, module) -> bool:
    """A source is ready to use right now if it either needs no API key at
    all, or its key is already configured in .env -- the same check
    doctor.py's check_source_api_keys uses, shared here so the Scrape page's
    source picker can pre-select exactly the sources that will actually find
    something, not just the two original builtins."""
    if not getattr(module, "REQUIRES_API_KEY", False):
        return True
    return get_source_api_key(source_name) is not None


def allowed_env_keys() -> set[str]:
    """BASE_ENV_KEYS plus a per-source key name for every currently registered
    source -- computed lazily (not at import time) since it depends on
    enabled_sources.yaml, which can change between server starts."""
    from scraper.sources import registry

    keys = set(BASE_ENV_KEYS)
    keys.update(_env_key_for(name) for name in registry.list_sources())
    return keys


def write_env_value(key: str, value: str, env_path: Path = Path(".env")) -> None:
    """Writes key=value into env_path via python-dotenv's set_key, which
    updates an existing line in place or appends a new one, preserving
    everything else in the file. Raises ValueError for a key outside
    allowed_env_keys() -- never writes an arbitrary key a caller made up.
    Strips newlines from value so a crafted value can't inject extra .env
    lines. Reloads into this process's os.environ so the change is visible
    immediately, without a server restart."""
    if key not in allowed_env_keys():
        raise ValueError(f"{key!r} is not a recognized credential key")

    clean_value = value.replace("\n", "").replace("\r", "")
    if not env_path.exists():
        env_path.touch()
    set_key(str(env_path), key, clean_value)
    load_dotenv(env_path, override=True)
