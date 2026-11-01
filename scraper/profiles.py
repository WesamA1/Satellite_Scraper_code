"""Loads named scrape profiles (smoke/full) from profiles.yaml."""

from pathlib import Path

import yaml

PROFILES_PATH = Path(__file__).parent / "profiles.yaml"


def load_profile(name: str) -> dict:
    profiles = yaml.safe_load(PROFILES_PATH.read_text())
    if name not in profiles:
        raise ValueError(f"Unknown profile '{name}'. Known profiles: {sorted(profiles)}")
    return profiles[name]
