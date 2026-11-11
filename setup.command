#!/bin/bash
# Double-click setup for macOS (Terminal.app runs .command files directly).
# The steps themselves are in setup.sh, next to this file.
bash "$(dirname "$0")/setup.sh"
echo
read -rp "Press Enter to close..."
