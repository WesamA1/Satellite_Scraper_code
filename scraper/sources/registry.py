"""Phase 2: dynamic source registry.

Built-ins (wikimedia, nasa) are always available. Promoted sources
(scraper/promotion.py) are loaded from enabled_sources.yaml. A module missing a
required ImageSource attribute (see scraper/sources/base.py) is skipped with a
logged warning, never partially registered -- download.py should never have to
guard against a half-implemented source.

Python's own import cache means a module already imported this process won't
be re-read from disk even if its file changes -- editing enabled_sources.yaml
or a promoted module's file only takes effect on the next process start, which
is the deliberate "no hot-loading" behavior from the promotion design.
"""

import importlib
import logging
from pathlib import Path
from types import ModuleType

import yaml

from scraper.sources import gunters_space, nasa_images, wikimedia

logger = logging.getLogger("scraper.sources.registry")

ENABLED_SOURCES_PATH = Path(__file__).parent / "enabled_sources.yaml"

REQUIRED_ATTRS = (
    "SOURCE_MODULE",
    "make_session",
    "search_images",
    "relevant_results",
    "extract_record",
    "download_image",
)

_BUILTINS: dict[str, ModuleType] = {
    "wikimedia": wikimedia,
    "nasa": nasa_images,
    "gunters_space": gunters_space,
}


def validate_shape(module: ModuleType) -> list[str]:
    """Returns the list of missing required attributes (empty = looks complete)."""
    return [name for name in REQUIRED_ATTRS if not hasattr(module, name)]


def _is_enabled(module: ModuleType) -> bool:
    """A source can opt out of being listed via ENABLED = False.

    Absent entirely, a source is enabled by default.
    """
    return getattr(module, "ENABLED", True)


def _load_promoted() -> dict[str, ModuleType]:
    loaded: dict[str, ModuleType] = {}
    if not ENABLED_SOURCES_PATH.exists():
        return loaded

    names = yaml.safe_load(ENABLED_SOURCES_PATH.read_text()) or []
    for name in names:
        try:
            module = importlib.import_module(f"scraper.sources.{name}")
        except Exception as e:
            logger.warning(f"Could not import promoted source '{name}': {e.__class__.__name__}: {e}")
            continue

        missing = validate_shape(module)
        if missing:
            logger.warning(f"Promoted source '{name}' missing required attrs {missing}; skipping.")
            continue

        loaded[getattr(module, "SOURCE_MODULE", name)] = module

    return loaded


def list_sources() -> dict[str, ModuleType]:
    sources = dict(_BUILTINS)
    sources.update(_load_promoted())
    return {name: module for name, module in sources.items() if _is_enabled(module)}


def get_source(name: str) -> ModuleType:
    enabled = list_sources()
    if name in enabled:
        return enabled[name]

    all_sources = dict(_BUILTINS)
    all_sources.update(_load_promoted())
    if name in all_sources:
        raise ValueError(f"Source module {name!r} exists but is disabled (module.ENABLED is False).")
    raise ValueError(f"Unknown source module: {name!r}. Known: {sorted(enabled)}")
