"""Offline tests for the CHANGELOG.md parser behind the in-app Release Notes
page. Deliberately exercises the exact shape CHANGELOG.md itself uses:
wrapped bullet continuation lines, multiple dated entries, prose before the
first header."""

from scraper.release_notes import parse_changelog

SAMPLE = """# Changelog

Some intro prose that should be ignored entirely.

## 2026-11-01

### Added
- A short item.
- A long item that wraps onto a
  second line and even a
  third line here.

### Fixed
- Something that broke, now fixed.

## 2026-10-31

### Changed
- An older entry, listed after the newer one in the file.
"""


def test_parses_multiple_dated_entries_in_file_order(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)

    entries = parse_changelog(path)

    assert [e["heading"] for e in entries] == ["2026-11-01", "2026-10-31"]


def test_parses_sections_and_items_for_an_entry(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)

    entries = parse_changelog(path)
    latest = entries[0]

    assert [s["name"] for s in latest["sections"]] == ["Added", "Fixed"]
    assert latest["sections"][1]["bullets"] == ["Something that broke, now fixed."]


def test_folds_wrapped_continuation_lines_into_one_item(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)

    entries = parse_changelog(path)
    added_items = entries[0]["sections"][0]["bullets"]

    assert added_items == [
        "A short item.",
        "A long item that wraps onto a second line and even a third line here.",
    ]


def test_ignores_everything_before_the_first_dated_header(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)

    entries = parse_changelog(path)

    assert len(entries) == 2  # the intro prose didn't become a phantom entry


def test_empty_file_returns_no_entries(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\nNothing here yet.\n")

    assert parse_changelog(path) == []


def test_renders_bold_and_code_inline_markdown_as_html(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n## 2026-11-01\n\n### Changed\n"
        "- Renamed to **Ground Station**; `license_short_name` stays as-is.\n"
    )

    entries = parse_changelog(path)

    assert entries[0]["sections"][0]["bullets"] == [
        "Renamed to <strong>Ground Station</strong>; <code>license_short_name</code> stays as-is."
    ]


def test_escapes_html_special_characters_in_bullet_text(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## 2026-11-01\n\n### Fixed\n- A <script>alert(1)</script> edge case.\n")

    entries = parse_changelog(path)

    assert "<script>" not in entries[0]["sections"][0]["bullets"][0]
    assert "&lt;script&gt;" in entries[0]["sections"][0]["bullets"][0]


def test_heading_can_name_a_release_instead_of_a_literal_date(tmp_path):
    """A major entry's heading can read like the project's own REV tag
    ("Ground Station 3.0") rather than a bare date -- the parser doesn't
    care, it just captures whatever follows "## "."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n\n## Ground Station 3.0 -- November 1, 2026\n\n### Added\n- Something.\n")

    entries = parse_changelog(path)

    assert entries[0]["heading"] == "Ground Station 3.0 -- November 1, 2026"


def test_captures_a_summary_paragraph_between_heading_and_first_category(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n## Ground Station 3.0\n\n"
        "A short intro paragraph describing the\nrelease as a whole.\n\n"
        "### Added\n- Something.\n"
    )

    entries = parse_changelog(path)

    assert entries[0]["summary"] == "A short intro paragraph describing the release as a whole."


def test_summary_is_empty_when_an_entry_has_none(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)

    entries = parse_changelog(path)

    assert entries[0]["summary"] == ""


def test_a_category_with_no_dashes_becomes_a_single_paragraph(tmp_path):
    """A major entry reads better as prose per category than a bulleted
    list -- CHANGELOG.md's "Ground Station 3.0" entry actually uses this
    form, not "- " bullets, for exactly that reason."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n## Ground Station 3.0\n\n"
        "### A new name\n"
        "Renamed from Scraper 3.0 to Ground Station, across the app, the\n"
        "CLI, and the docs.\n"
    )

    entries = parse_changelog(path)

    assert entries[0]["sections"][0]["bullets"] == [
        "Renamed from Scraper 3.0 to Ground Station, across the app, the CLI, and the docs."
    ]


def test_a_category_can_still_mix_a_lead_in_sentence_with_dashed_bullets(tmp_path):
    """Covers the boundary between the two forms: text before the first "- "
    starts its own paragraph-bullet, then "- " lines each start a new one."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text(
        "# Changelog\n\n## Ground Station 3.0\n\n"
        "### Reliability\n"
        "Several fixes landed together:\n"
        "- First fix.\n"
        "- Second fix.\n"
    )

    entries = parse_changelog(path)

    assert entries[0]["sections"][0]["bullets"] == [
        "Several fixes landed together:",
        "First fix.",
        "Second fix.",
    ]
