"""Shared test setup."""

from scraper import logging_setup

# Tests start the CLI and the web app, both of which add a file logger writing
# to ./logs -- which, from the project folder, is the real logs/ folder.
# Marking logging as already set up keeps test runs out of it.
logging_setup._configured = True
