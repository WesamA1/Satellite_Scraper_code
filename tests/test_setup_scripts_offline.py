"""The one-step setup scripts (setup.bat, setup.command, setup.sh). They can't
be run here -- they install things -- so these check what would quietly break
them on someone else's machine."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = ("setup.sh", "setup.command")


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_setup_scripts_are_valid_and_runnable(name):
    text = _text(name)
    assert text.startswith("#!/bin/bash\n")
    assert "\r" not in text  # Windows line endings stop bash at the first line
    if os.name != "nt":
        assert os.access(ROOT / name, os.X_OK), f"{name} must be executable to be double-clicked"
    bash = shutil.which("bash")
    if bash and os.name != "nt":
        checked = subprocess.run([bash, "-n", str(ROOT / name)], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr


def test_mac_double_click_runs_the_shared_script():
    assert "setup.sh" in _text("setup.command")


@pytest.mark.parametrize("name", ("setup.sh", "setup.bat"))
def test_setup_installs_the_packages_then_the_icon(name):
    text = _text(name)
    assert "astral.sh/uv/install" in text  # a machine without uv gets it
    sync, icon = text.index("uv sync --all-groups"), text.index("scraper.cli install-app")
    assert sync < icon
    # A new .venv is made with a named Python, not whichever is newest.
    assert re.search(r"uv sync --all-groups --python \S*NEW_VENV_PYTHON", text)


def test_windows_setup_has_no_labels():
    """cmd loses its place at a label when a .bat arrives with Unix line
    endings (a GitHub download can do that), so setup.bat uses none."""
    for line in _text("setup.bat").splitlines():
        stripped = line.strip().lower()
        assert not stripped.startswith(":"), line
        assert not re.match(r"(goto|call\s+:)", stripped), line


def test_windows_setup_keeps_brackets_out_of_messages():
    """Inside an if (...) block a bracket in an echoed message ends the block
    early, and what follows runs when it shouldn't. Simplest to allow none."""
    for line in _text("setup.bat").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("echo "):
            assert "(" not in stripped and ")" not in stripped, line
