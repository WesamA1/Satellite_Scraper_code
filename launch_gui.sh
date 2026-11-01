#!/bin/bash
# Linux/Ubuntu launcher. Most file managers need "Allow executing file as
# program" set once (right-click -> Properties -> Permissions) before a
# double-click will run this instead of opening it in a text editor --
# running `./launch_gui.sh` from a terminal always works regardless.
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
    echo ".venv not found -- run \"uv sync --all-groups\" in this folder first."
    exit 1
fi

echo "Starting Scraper 3.0 -- your browser will open automatically..."
./.venv/bin/python -m scraper.cli serve "$@"
