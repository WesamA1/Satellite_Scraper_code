"""Fixing an old-tool annotation CSV whose rows sit under the wrong header.

The old tool's CSV writer kept the first row's header for every later row
but wrote each row's values in whatever order the attribute list was in that
day. Once the list was reordered, later rows landed in the wrong columns:
solar panel answers under Manufacturer, and so on. The importer and the
training set detect such rows and leave them out (annotate.misplaced_values);
this puts them back where they belong.

A fix is a known order: the order a row's values were really written in.
KNOWN_ROW_ORDERS lists the ones found so far. Every row that looks shifted is
tried against each of them, and a fix is used only when it leaves nothing in
the row in the wrong column -- so every answer that was out of place is now
under an attribute that offers it -- and it is the only known fix that does.

A row no known fix explains, or that more than one does, is left exactly as
it was and listed in the report for a person to look at. Nothing is guessed.

Rows that don't look shifted are never touched, and neither is the original
file: the result is a new CSV with the same header and the same rows in the
same order. Cells are moved whole, never edited -- renamed options and
misspellings are still the importer's job.
"""

import csv
from pathlib import Path

from scraper.stages import annotate

# name -> the order the values were written in, as today's attribute names.
# The first cell after "name" holds the answer for the first attribute here,
# whatever the header above it says. To add one: tally, for the rows left
# over, which attribute's own options turn up in each column.
KNOWN_ROW_ORDERS: dict[str, dict] = {
    "windows-and-manufacturer-early": {
        "about": (
            "Windows or Optical Apertures and Manufacturer written 2nd and 3rd, Docking Systems and "
            "Solar Panel Configuration 7th and 8th, Insulation Type ahead of Deployment Mechanisms"
        ),
        "order": [
            "Shape",
            "Windows or Optical Apertures",
            "Manufacturer",
            "Antennas",
            "Passive External Sensors",
            "Active External Sensors",
            "Docking Systems",
            "Solar Panel Configuration",
            "Surface Finish",
            "Insulation Type",
            "Deployment Mechanisms",
            "Mass",
            "Propulsion Mechanism",
            "Color",
        ],
    },
}

REPORT_COLUMNS = ["row", "name", "result", "fix", "detail"]


def default_fixed_path(csv_path: Path) -> Path:
    """Next to the original: annotations.csv -> annotations_fixed.csv."""
    return csv_path.with_name(f"{csv_path.stem}_fixed{csv_path.suffix}")


def _read(csv_path: Path) -> tuple[list[str], list[list[str]], str]:
    """(header, rows, the encoding to write the copy in). The old tool wrote
    in the OS default encoding, cp1252 on the Windows machine."""
    data = csv_path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
        encoding = "utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8"
    except UnicodeDecodeError:
        text, encoding = data.decode("cp1252"), "cp1252"
    rows = list(csv.reader(text.splitlines(keepends=True)))
    return (rows[0] if rows else []), rows[1:], encoding


def _wrong_columns(cells: dict[str, str], attributes: dict, schema: dict) -> dict[str, list[str]]:
    """attribute -> the answers in its cell that belong to another attribute."""
    wrong = {}
    for attribute, cell in cells.items():
        value = annotate.convert_legacy_value(attributes[attribute], annotate.parse_legacy_cell(cell))
        if bad := annotate.misplaced_values(attribute, value, schema):
            wrong[attribute] = bad
    return wrong


def _describe(wrong: dict[str, list[str]]) -> str:
    return "; ".join(f"{attribute}: {', '.join(bad)}" for attribute, bad in wrong.items())


def fix_legacy_csv(
    csv_path: Path,
    schema: dict,
    out_path: Path | None = None,
    report_path: Path | None = None,
    dry_run: bool = False,
    known_orders: dict[str, dict] | None = None,
) -> dict:
    """Writes a copy of csv_path with its shifted rows put back under the
    right headers (to out_path, by default <name>_fixed.csv beside it), and
    a report of every row that looked shifted: fixed and how, or left and
    why. Nothing is written on a dry run; the counts still come back."""
    known_orders = KNOWN_ROW_ORDERS if known_orders is None else known_orders
    if not schema:
        raise ValueError("There are no attributes yet -- set up the attribute list before fixing a CSV.")
    if not csv_path.is_file():
        raise ValueError(f"No CSV at {csv_path}")
    out_path = out_path or default_fixed_path(csv_path)
    if out_path.resolve() == csv_path.resolve():
        raise ValueError("The fixed copy can't be written over the original. Give it a different name.")

    header, rows, encoding = _read(csv_path)
    if "name" not in header:
        raise ValueError(f"{csv_path} has no 'name' column -- is it the old tool's annotation CSV?")
    name_at = header.index("name")
    mapping, unmapped = annotate.map_legacy_columns(header, schema)
    attributes = annotate.flat_attributes(schema)

    # The header's attribute columns, left to right, and what each means today.
    positions = [i for i, column in enumerate(header) if column in mapping]
    in_header = [mapping[header[i]] for i in positions]
    # A known order only says something about a CSV that has the same attributes.
    usable = {name: fix["order"] for name, fix in known_orders.items() if sorted(fix["order"]) == sorted(in_header)}

    def cell(row: list[str], position: int) -> str:
        return row[position] if position < len(row) else ""

    fixed_rows: list[list[str]] = []
    report: list[dict] = []
    by_fix: dict[str, int] = {}
    counts = {"unchanged": 0, "fixed": 0, "left": 0}

    for number, row in enumerate(rows, start=1):
        as_headed = {in_header[k]: cell(row, position) for k, position in enumerate(positions)}
        wrong = _wrong_columns(as_headed, attributes, schema)
        if not wrong:
            counts["unchanged"] += 1
            fixed_rows.append(row)
            continue

        explained: list[tuple[str, dict[str, str]]] = []
        for name, order in usable.items():
            as_written = {order[k]: cell(row, position) for k, position in enumerate(positions)}
            if not _wrong_columns(as_written, attributes, schema):
                explained.append((name, as_written))

        entry = {"row": number, "name": cell(row, name_at), "fix": "", "detail": _describe(wrong)}
        if len(explained) == 1 and len(row) == len(header):
            name, as_written = explained[0]
            repaired = list(row)
            for k, position in enumerate(positions):
                repaired[position] = as_written[in_header[k]]
            fixed_rows.append(repaired)
            counts["fixed"] += 1
            by_fix[name] = by_fix.get(name, 0) + 1
            report.append({**entry, "result": "fixed", "fix": name})
            continue

        if len(row) != len(header):
            why = f"the row has {len(row)} cells and the header has {len(header)}"
        elif explained:
            why = f"more than one known fix fits ({', '.join(name for name, _ in explained)})"
        else:
            why = "no known fix puts every answer in a column it fits"
        fixed_rows.append(row)
        counts["left"] += 1
        report.append({**entry, "result": "left as it was", "detail": f"{why} -- {entry['detail']}"})

    wrote_report = report_path is not None and bool(report) and not dry_run
    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", newline="", encoding=encoding) as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(fixed_rows)
    if wrote_report:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
            writer.writeheader()
            writer.writerows(report)

    return {
        "rows": len(rows),
        **counts,
        "by_fix": by_fix,
        "left_rows": [entry for entry in report if entry["result"] != "fixed"],
        "fixes_tried": list(usable),
        "fixes_not_for_this_csv": [name for name in known_orders if name not in usable],
        "unmapped_columns": unmapped,
        "dry_run": dry_run,
        "out_path": None if dry_run else str(out_path),
        "report_path": str(report_path) if wrote_report else None,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scraper.stages.fix_csv <old-tool CSV>   (a dry run: nothing is written)")
    summary = fix_legacy_csv(Path(sys.argv[1]), annotate.load_schema(), dry_run=True)
    print(f"{summary['rows']} rows: {summary['fixed']} would be fixed, {summary['left']} look shifted but are left")
    for row in summary["left_rows"]:
        print(f"  row {row['row']} {row['name']}: {row['detail']}")
