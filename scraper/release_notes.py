"""Parses CHANGELOG.md for the in-app Release Notes page.

Deliberately narrow, not a general markdown engine -- CHANGELOG.md's own
format is simple and fully under this project's control (## headings,
### category headers, - bullet items, plus an optional summary paragraph
right under a heading), so a tiny line-based parser is enough and avoids a
new dependency for one page.
"""

import html
import re
from pathlib import Path


def _render_inline(text: str) -> str:
    """Escapes the line, then re-enables just the two inline markdown forms
    CHANGELOG.md actually uses: **bold** and `code`. Not a markdown engine --
    deliberately only these two, since that's the full extent of what the
    file's own prose needs."""
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped


def parse_changelog(path: Path) -> list[dict]:
    """Returns entries newest-first (as written), each:
    {"heading": "Ground Station 3.0 -- November 1, 2026", "summary": "...",
     "sections": [{"name": "A new name", "bullets": [...]}]}

    `heading` need not be a literal date -- a major entry's heading can name
    a release the way this project's own REV tag does. `summary` is whatever
    prose sits directly under the heading, before the first "### " category;
    it's optional and empty when there isn't any. A category's own body can
    be either traditional "- " bullets or a plain paragraph (a major entry
    reads better as prose per category than as a bulleted list) -- either
    way it ends up as one or more strings in `bullets`. Ignores everything
    before the first "## " header (the file's own title).
    """
    entries: list[dict] = []
    current_entry = None
    current_section = None

    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()
        text = line.strip()
        if line.startswith("## "):
            current_entry = {"heading": text[3:].strip(), "summary": "", "sections": []}
            current_section = None
            entries.append(current_entry)
        elif line.startswith("### ") and current_entry is not None:
            current_section = {"name": text[4:].strip(), "bullets": []}
            current_entry["sections"].append(current_section)
        elif text and current_section is not None and not line.startswith("#"):
            if text.startswith("- "):
                current_section["bullets"].append(text[2:].strip())
            elif current_section["bullets"]:
                # A continuation line (wrapped "- " bullet, or the next
                # sentence of a plain paragraph) -- folds onto whichever
                # bullet/paragraph is already being built.
                current_section["bullets"][-1] += " " + text
            else:
                # The first line of a plain paragraph, with no "- " at all.
                current_section["bullets"].append(text)
        elif text and current_entry is not None and current_section is None and not line.startswith("#"):
            # Prose between the "## " heading and the first "### " category
            # -- the entry's own summary, not tied to any one category.
            current_entry["summary"] = (current_entry["summary"] + " " + text).strip()

    for entry in entries:
        entry["summary"] = _render_inline(entry["summary"])
        for section in entry["sections"]:
            section["bullets"] = [_render_inline(bullet) for bullet in section["bullets"]]

    return entries
