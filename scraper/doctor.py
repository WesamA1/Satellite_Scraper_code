"""Phase 1 Task 6: the doctor command's checks.

Each check is independent and never raises -- a missing optional dependency or
an unreachable API is a reported result, not a crash.
"""

import importlib.metadata
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

from scraper import secrets
from scraper.sources import registry
from scraper.stages import filter as filter_stage

load_dotenv()

REQUIRED_CREDENTIAL_KEYS = (
    "SPACETRACK_USERNAME",
    "SPACETRACK_PASSWORD",
    "WIKIMEDIA_BOT_USERNAME",
    "WIKIMEDIA_BOT_PASSWORD",
)

# Only needed for Phase 2's discovery/codegen features, not core scraping.
OPTIONAL_CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY",)

REQUIRED_PACKAGES = ("requests", "pillow", "python-dotenv")
OPTIONAL_PACKAGES = ("torch", "transformers")  # only needed for the filter stage

REACHABILITY_TARGETS = {
    "commons.wikimedia.org": "https://commons.wikimedia.org/w/api.php?action=query&meta=siteinfo&format=json",
    "images-api.nasa.gov": "https://images-api.nasa.gov/search?q=test&page_size=1",
}

MIN_FREE_DISK_GB = 1.0


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "warn"
    detail: str


def _default_fetch(url: str) -> bool:
    try:
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def check_gpu() -> CheckResult:
    try:
        _device, label = filter_stage.pick_device()
        return CheckResult("gpu", "pass", label)
    except ModuleNotFoundError:
        return CheckResult("gpu", "warn", "torch not installed (only needed for the filter stage)")
    except Exception as e:
        return CheckResult("gpu", "fail", f"{e.__class__.__name__}: {e}")


def check_credentials() -> list[CheckResult]:
    results = []
    for key in REQUIRED_CREDENTIAL_KEYS:
        value = os.environ.get(key, "").strip()
        status = "pass" if value else "fail"
        detail = "present" if value else "missing from .env"
        results.append(CheckResult(f"credential:{key}", status, detail))
    for key in OPTIONAL_CREDENTIAL_KEYS:
        value = os.environ.get(key, "").strip()
        status = "pass" if value else "warn"
        detail = "present" if value else "missing from .env (only needed for discovery/codegen)"
        results.append(CheckResult(f"credential:{key}", status, detail))
    return results


def check_source_api_keys() -> list[CheckResult]:
    """Per-source keys (scraper/secrets.py's SOURCE_<NAME>_API_KEY convention),
    for any registered source that declares REQUIRES_API_KEY = True."""
    results = []
    for name, module in registry.list_sources().items():
        if not getattr(module, "REQUIRES_API_KEY", False):
            continue
        key = secrets.get_source_api_key(name)
        status = "pass" if key else "warn"
        detail = "present" if key else f"missing SOURCE_{name.upper()}_API_KEY from .env"
        results.append(CheckResult(f"credential:source:{name}", status, detail))
    return results


def check_api_reachability(fetch_fn=None) -> list[CheckResult]:
    fetch_fn = fetch_fn or _default_fetch
    results = []
    for host, url in REACHABILITY_TARGETS.items():
        reachable = fetch_fn(url)
        results.append(CheckResult(f"reachable:{host}", "pass" if reachable else "fail", url))
    return results


def check_disk_space(path: Path = Path.cwd()) -> CheckResult:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    status = "pass" if free_gb >= MIN_FREE_DISK_GB else "fail"
    return CheckResult("disk_space", status, f"{free_gb:.1f} GB free")


def check_package_versions() -> list[CheckResult]:
    results = []
    for package in REQUIRED_PACKAGES:
        try:
            version = importlib.metadata.version(package)
            results.append(CheckResult(f"package:{package}", "pass", version))
        except importlib.metadata.PackageNotFoundError:
            results.append(CheckResult(f"package:{package}", "fail", "not installed"))
    for package in OPTIONAL_PACKAGES:
        try:
            version = importlib.metadata.version(package)
            results.append(CheckResult(f"package:{package}", "pass", version))
        except importlib.metadata.PackageNotFoundError:
            results.append(CheckResult(f"package:{package}", "warn", "not installed (only needed for filter)"))
    return results


def run_checks(fetch_fn=None) -> list[CheckResult]:
    results = [check_gpu()]
    results += check_credentials()
    results += check_source_api_keys()
    results += check_api_reachability(fetch_fn)
    results.append(check_disk_space())
    results += check_package_versions()
    return results


if __name__ == "__main__":
    for result in run_checks():
        print(f"[{result.status.upper():4}] {result.name}: {result.detail}")
