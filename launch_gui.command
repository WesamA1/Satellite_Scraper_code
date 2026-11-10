#!/bin/bash
# Double-click launcher for macOS (Terminal.app runs .command files directly).
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
    echo ".venv not found -- run \"uv sync --all-groups\" in this folder first."
    read -rp "Press Enter to close..."
    exit 1
fi

echo "Starting Ground Station -- your browser will open automatically..."
./.venv/bin/python -m scraper.cli serve "$@"
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "Ground Station exited with an error -- see above."
    read -rp "Press Enter to close..."
fi
