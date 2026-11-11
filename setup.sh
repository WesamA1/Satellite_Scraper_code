#!/bin/bash
# One-step setup for macOS and Linux: everything Ground Station needs on a new
# machine, then its icon. It needs an internet connection this once; what it
# installs runs without one afterwards.
#
#   macOS: double-click setup.command (it runs this).
#   Linux: ./setup.sh in a terminal, or double-click it once "Allow executing
#          file as program" is set (right-click -> Properties -> Permissions).
#
# The machine needs neither git nor Python: uv fetches a Python of its own,
# and the folder can come from a GitHub download or a USB stick as well as a
# clone. Safe to run again -- after an update, or after moving the folder --
# since it only fetches what is missing.
cd "$(dirname "$0")" || exit 1

# The Python a new .venv is made with: the one Ground Station is developed on.
# Left to itself uv takes the newest there is, which some packages may not
# support yet. A .venv that already works keeps the Python it has; one copied
# over from another machine doesn't work here and is made again.
NEW_VENV_PYTHON=3.14

step() { printf '\n== %s\n' "$1"; }
stop() {
    printf '\nSetup stopped: %s\n' "$1" >&2
    exit 1
}

# Where uv's installer puts it. A terminal opened before uv was installed
# doesn't have this on its PATH yet.
export PATH="$HOME/.local/bin:$PATH"

step "1/4  uv, the tool that installs everything else"
if ! command -v uv >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        stop "neither curl nor wget is installed, so uv can't be downloaded. Install one (Ubuntu: sudo apt install curl) and run this again."
    fi
fi
command -v uv >/dev/null 2>&1 || stop "uv could not be installed. Check the internet connection and run this again."
uv --version

step "2/4  Python and the packages Ground Station uses (the first time is a large download)"
if [ -x .venv/bin/python ] && ./.venv/bin/python -c "" 2>/dev/null; then
    uv sync --all-groups
else
    uv sync --all-groups --python "$NEW_VENV_PYTHON"
fi || stop "the packages could not be installed -- the lines above say why. Check the internet connection and run this again."

step "3/4  The Ground Station icon"
if [ "$(uname)" = "Darwin" ]; then
    where_it_is="the Applications folder in your home folder (or Launchpad, or Spotlight)"
elif [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    where_it_is="the applications menu"
    # Linux has no web engine built in for the app's own window. Without one
    # the app still works: it opens in the browser instead.
    uv pip install "pywebview[qt]" ||
        echo "Could not install the window's web engine. Ground Station will open in your browser instead."
else
    where_it_is=""
    echo "This machine has no desktop, so there is no icon to add."
    echo "Start Ground Station with ./launch_gui.sh, or use the commands in the README."
fi
if [ -n "$where_it_is" ]; then
    ./.venv/bin/python -m scraper.cli install-app ||
        stop "the icon could not be added -- the lines above say why. Everything else is installed: ./launch_gui.sh starts Ground Station without it."
fi

step "4/4  Check"
./.venv/bin/python -c "
from scraper.doctor import check_gpu
result = check_gpu()
print('The image models will run on:', result.detail)
" || echo "Could not check which processor the image models will use. The Doctor page in the app has the details."

printf '\nDone.\n'
[ -n "$where_it_is" ] && echo "Open Ground Station from $where_it_is."
echo "Scraping needs credentials, which go in on the app's API Keys page. Annotating needs none."
