"""The annotate stage: human visual-attribute labeling.

Ported from the standalone PyQt5 Attribute Annotation Tool (MIAT --
AnnotationScreen.py, Admin_Panel.py, SubmitMenu.py, JsonRW.py). Same
attribute schema file format, same 14 input types, same per-type value rules
(empty multi-select -> ["None"], DropdownOther's free text wins over its
dropdown, "Mark unusable" -> every attribute "UNUSABLE"), same CSV shape.

What changed in the port, and why:
- Images come from the DB (active images, optionally only ones already kept
  in Review) instead of a folder, and stay where they are. The old tool moved
  each finished image into an "annotated" folder to track progress, which
  here would break images.path -- "already annotated" is just "has
  annotation rows from this annotator" instead.
- Values live in the existing annotations table (one row per attribute,
  value JSON-encoded, guide_version = a hash of the schema they were made
  under) -- no schema change. The CSV is regenerated from the DB on export
  rather than appended per submit: the old append-only writer took its
  header from whichever row came first, so any later row with a different
  attribute order landed in the wrong columns.
- Attribute names must be unique across categories, since the old tool's
  flat annoHistory.json already silently assumed that.
- import_legacy_csv brings the old tool's CSV in, so a team can carry on
  from where the old tool left off instead of re-annotating.
- Every attribute accepts "Unknown" (can't tell from the image), so nothing
  visually undeterminable has to be guessed. The old tool only offered it on
  its Present/Unsure/Not present questions.
"""

import ast
import csv
import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, time, timezone
from pathlib import Path

DEFAULT_SCHEMA_PATH = Path("attributes.json")
DEFAULT_EXPORT_PATH = Path("export/annotations.csv")

UNUSABLE = "UNUSABLE"

# "Can't tell from this image" -- a built-in answer for every attribute, so
# an annotator never has to guess (a manufacturer, a mass) just to get past a
# required field. Distinct from "None", which means "looked, and it isn't
# there". Stored in this one spelling ("unknown", "UNKNOWN" are folded in).
UNKNOWN = "Unknown"

# Present/Unsure/Not present -- what the old tool's RadioSelector recorded.
RADIO_VALUES = ("Present", "Unknown", "Not Present")

TYPES = (
    "Text Box",
    "Long Text",
    "Dropdown",
    "DropdownOther",
    "Single-Select",
    "Multi-Select",
    "Check Button",
    "Number",
    "Slider",
    "Rating Scale",
    "Date",
    "Time",
    "Date-Time",
    "Boolean",
)
OPTION_TYPES = ("Dropdown", "DropdownOther", "Multi-Select")
RANGE_TYPES = ("Number", "Slider", "Rating Scale")

# Defaults when a range type's [type, min, max, step] params are left off --
# same fallbacks AnnotationScreen.build_chunks used.
RANGE_DEFAULTS = {
    "Number": (0, 100, 1),
    "Slider": (0, 100, 1),
    "Rating Scale": (1, 5, 1),
}

# Annotator name the Review page's variant labels are written under -- an
# attribute annotator using it too would make their rows indistinguishable.
RESERVED_ANNOTATORS = ("human_review",)
RESERVED_ATTRIBUTES = ("variant",)


class SchemaError(ValueError):
    pass


class AnnotationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{name}: {msg}" for name, msg in errors.items()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Schema ----


def validate_schema(data) -> dict:
    """Returns a cleaned copy of `data` ({category: {attribute: [type, ...]}}),
    or raises SchemaError. Type names are matched case-insensitively and
    canonicalized, since the old Admin Panel wrote new attributes as
    lowercase "text box", which its own annotation screen then didn't
    recognize."""
    if not isinstance(data, dict):
        raise SchemaError("The attribute list must be a JSON object of categories.")

    canonical_types = {t.lower(): t for t in TYPES}
    cleaned: dict = {}
    seen: dict[str, str] = {}

    for category, attributes in data.items():
        category = str(category).strip()
        if not category:
            raise SchemaError("Category names can't be empty.")
        if category in cleaned:
            raise SchemaError(f"Category '{category}' appears twice.")
        if not isinstance(attributes, dict):
            raise SchemaError(f"Category '{category}' must map attribute names to specs.")

        cleaned[category] = {}
        for name, spec in attributes.items():
            name = str(name).strip()
            if not name:
                raise SchemaError(f"An attribute in '{category}' has an empty name.")
            if name in RESERVED_ATTRIBUTES:
                raise SchemaError(f"'{name}' is reserved (the Review page's variant label).")
            if name in seen:
                raise SchemaError(f"Attribute '{name}' is in both '{seen[name]}' and '{category}' -- must be unique.")
            seen[name] = category

            if not isinstance(spec, list) or not spec:
                raise SchemaError(f"'{name}' needs a spec like [\"Dropdown\", [options...]].")
            attr_type = canonical_types.get(str(spec[0]).strip().lower())
            if attr_type is None:
                raise SchemaError(f"'{name}' has unknown type '{spec[0]}'.")

            if attr_type in OPTION_TYPES:
                options = spec[1] if len(spec) > 1 else []
                if not isinstance(options, list):
                    raise SchemaError(f"'{name}' options must be a list.")
                options = [str(o).strip() for o in options if str(o).strip()]
                if not options:
                    raise SchemaError(f"'{name}' ({attr_type}) needs at least one option.")
                if len(set(options)) != len(options):
                    raise SchemaError(f"'{name}' has a duplicate option.")
                cleaned[category][name] = [attr_type, options]
            elif attr_type in RANGE_TYPES:
                params = spec[1:]
                if not all(isinstance(p, (int, float)) and not isinstance(p, bool) for p in params):
                    raise SchemaError(f"'{name}' ({attr_type}) params must be numbers: [type, min, max, step].")
                lo, hi, step = range_params([attr_type, *params])
                if lo >= hi or step <= 0:
                    raise SchemaError(f"'{name}' needs min < max and step > 0.")
                cleaned[category][name] = [attr_type, *[int(p) for p in params[:3]]]
            else:
                cleaned[category][name] = [attr_type]

    return cleaned


def range_params(spec: list) -> tuple[int, int, int]:
    lo, hi, step = RANGE_DEFAULTS[spec[0]]
    params = [int(p) for p in spec[1:4]]
    return (
        params[0] if len(params) > 0 else lo,
        params[1] if len(params) > 1 else hi,
        params[2] if len(params) > 2 else step,
    )


def load_schema(path: Path = DEFAULT_SCHEMA_PATH) -> dict:
    """Empty schema when the file doesn't exist yet -- the Attributes page
    is where one gets built from scratch."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise SchemaError(f"{path} isn't valid JSON: {e}") from e
    return validate_schema(data)


def save_schema(data, path: Path = DEFAULT_SCHEMA_PATH) -> dict:
    """Validates, then writes via a temp file + rename so a crash mid-write
    can't leave a half-written attributes.json behind."""
    cleaned = validate_schema(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=4)
        f.write("\n")
    tmp.replace(path)
    return cleaned


def schema_version(schema: dict) -> str:
    """Short content hash, stored as each annotation's guide_version so rows
    made under different attribute lists can be told apart later."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def flat_attributes(schema: dict) -> dict[str, list]:
    return {name: spec for attributes in schema.values() for name, spec in attributes.items()}


# ---- Values ----


def _is_unknown(raw) -> bool:
    return isinstance(raw, str) and raw.strip().lower() == UNKNOWN.lower()


def normalize_value(spec: list, raw):
    """One attribute's submitted value -> what gets stored. Raises ValueError
    with an annotator-facing message when it's missing or invalid."""
    attr_type = spec[0]

    if attr_type in ("Check Button", "Single-Select"):
        if raw not in RADIO_VALUES:
            raise ValueError("pick Present, Unsure, or Not present")
        return raw

    if attr_type == "Multi-Select" and isinstance(raw, list) and any(_is_unknown(v) for v in raw):
        if len(raw) > 1:
            raise ValueError("Unknown can't be combined with other options")
        return [UNKNOWN]
    if _is_unknown(raw):
        return [UNKNOWN] if attr_type == "Multi-Select" else UNKNOWN

    if attr_type in ("Text Box", "Long Text"):
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            raise ValueError("required")
        return value

    if attr_type == "Boolean":
        if not isinstance(raw, bool):
            raise ValueError("must be true or false")
        return raw

    if attr_type in RANGE_TYPES:
        if isinstance(raw, bool) or raw is None or raw == "":
            raise ValueError("required")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number")
        lo, hi, _ = range_params(spec)
        if not lo <= value <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return value

    if attr_type == "Dropdown":
        if raw not in spec[1]:
            raise ValueError("pick one of the listed options")
        return raw

    if attr_type == "DropdownOther":
        value = raw.strip() if isinstance(raw, str) else ""
        if not value:
            raise ValueError("pick an option or type one in")
        return value

    if attr_type == "Multi-Select":
        if raw is None:
            raw = []
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise ValueError("must be a list of options")
        unknown = [v for v in raw if v not in spec[1] and v != "None"]
        if unknown:
            raise ValueError(f"unknown option(s): {', '.join(unknown)}")
        selected = [o for o in spec[1] if o in raw]
        return selected or ["None"]

    if attr_type in ("Date", "Time", "Date-Time"):
        parsers = {"Date": date.fromisoformat, "Time": time.fromisoformat, "Date-Time": datetime.fromisoformat}
        parser = parsers[attr_type]
        if not isinstance(raw, str) or not raw:
            raise ValueError("required")
        try:
            parser(raw)
        except ValueError:
            raise ValueError("isn't a valid value")
        return raw

    raise ValueError(f"unsupported type '{attr_type}'")


def normalize_values(schema: dict, values: dict, unusable: bool = False) -> dict:
    """Every attribute in the schema -> its stored value. Raises
    AnnotationError carrying every invalid field at once, so the page can
    flag them all instead of one per round trip."""
    attributes = flat_attributes(schema)
    if unusable:
        return {name: UNUSABLE for name in attributes}

    normalized, errors = {}, {}
    for name, spec in attributes.items():
        try:
            normalized[name] = normalize_value(spec, values.get(name))
        except ValueError as e:
            errors[name] = str(e)
    if errors:
        raise AnnotationError(errors)
    return normalized


def validate_annotator(annotator: str) -> str:
    annotator = (annotator or "").strip()
    if not annotator:
        raise ValueError("Enter an annotator name first.")
    if len(annotator) > 64:
        raise ValueError("Annotator name is too long (64 characters max).")
    if annotator in RESERVED_ANNOTATORS:
        raise ValueError(f"'{annotator}' is reserved -- pick a different annotator name.")
    return annotator


# ---- Queue + submit ----


def annotation_queue(
    conn: sqlite3.Connection,
    annotator: str,
    family: str | None = None,
    reviewed_only: bool = True,
    limit: int = 50,
    skip_annotated_by_others: bool = True,
) -> dict:
    """Active images still needing annotation. reviewed_only limits it to
    images already kept on the Review page, so nobody spends time labeling
    something that's about to be rejected. skip_annotated_by_others drops
    images anyone has already annotated (imported old-tool annotations
    included), which is what lets a team pick up where it left off; turn it
    off to have a second annotator label the same images again."""
    if skip_annotated_by_others:
        already_done = """NOT EXISTS (SELECT 1 FROM annotations a
                       WHERE a.image_id = images.id AND a.annotator NOT IN ('human_review')
                         AND a.attribute != 'variant')"""
        params: list = []
    else:
        already_done = """NOT EXISTS (SELECT 1 FROM annotations a
                       WHERE a.image_id = images.id AND a.annotator = ? AND a.attribute != 'variant')"""
        params = [annotator]
    where = ["images.status = 'active'", already_done]
    if reviewed_only:
        where.append("images.reviewed_at IS NOT NULL")
    if family:
        where.append("images.family = ?")
        params.append(family)
    where_sql = " AND ".join(where)

    remaining = conn.execute(f"SELECT COUNT(*) AS n FROM images WHERE {where_sql}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT id, family, path FROM images WHERE {where_sql} ORDER BY id LIMIT ?",
        (*params, limit),
    ).fetchall()
    return {
        "remaining": remaining,
        "queue": [{"id": r["id"], "family": r["family"], "name": Path(r["path"]).name} for r in rows],
    }


def submit_annotation(
    conn: sqlite3.Connection,
    image_id: int,
    annotator: str,
    values: dict,
    schema: dict,
    unusable: bool = False,
) -> dict:
    """Validates and stores one image's full annotation, all-or-nothing.
    Re-submitting overwrites this annotator's earlier values for the image."""
    annotator = validate_annotator(annotator)
    if conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone() is None:
        raise ValueError(f"No image with id {image_id}")
    if not schema:
        raise ValueError("There are no attributes to annotate -- add some on the Attributes page.")

    normalized = normalize_values(schema, values, unusable=unusable)
    version = schema_version(schema)
    now = _now()
    with conn:
        conn.executemany(
            """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(image_id, attribute, annotator)
               DO UPDATE SET value = excluded.value, guide_version = excluded.guide_version,
                             created_at = excluded.created_at""",
            [(image_id, name, json.dumps(value), annotator, version, now) for name, value in normalized.items()],
        )
    return normalized


def get_annotation(conn: sqlite3.Connection, image_id: int, annotator: str) -> dict:
    rows = conn.execute(
        "SELECT attribute, value FROM annotations WHERE image_id = ? AND annotator = ? AND attribute != 'variant'",
        (image_id, annotator),
    ).fetchall()
    return {r["attribute"]: json.loads(r["value"]) for r in rows}


def annotation_counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT COUNT(DISTINCT image_id) AS images, COUNT(DISTINCT annotator) AS annotators
           FROM annotations WHERE attribute != 'variant' AND annotator NOT IN ('human_review')"""
    ).fetchone()
    return {"images": row["images"], "annotators": row["annotators"]}


# ---- Export ----


def _csv_cell(value) -> str:
    """Same rendering the old tool's csv.DictWriter produced (str() of the
    value), so lists read back as "['Patch', 'Omni']" -- what anything
    already parsing its annotatedInfo.csv expects."""
    return str(value)


def export_annotations(
    conn: sqlite3.Connection,
    schema: dict,
    out_path: Path = DEFAULT_EXPORT_PATH,
    annotator: str | None = None,
) -> int:
    """One row per (image, annotator), columns in the current schema's
    order: name, every attribute, then provenance columns. Attributes the
    image was never annotated on (added to the schema later) stay blank."""
    attribute_names = list(flat_attributes(schema))
    query = """SELECT annotations.image_id, annotations.annotator, annotations.attribute, annotations.value,
                      annotations.guide_version, annotations.created_at, images.family, images.path
               FROM annotations JOIN images ON images.id = annotations.image_id
               WHERE annotations.attribute != 'variant' AND annotations.annotator NOT IN ('human_review')"""
    params: tuple = ()
    if annotator:
        query += " AND annotations.annotator = ?"
        params = (annotator,)
    query += " ORDER BY images.family, annotations.image_id, annotations.annotator"

    records: dict[tuple[int, str], dict] = {}
    for row in conn.execute(query, params):
        key = (row["image_id"], row["annotator"])
        record = records.setdefault(
            key,
            {
                "name": Path(row["path"]).name,
                "family": row["family"],
                "image_id": row["image_id"],
                "annotator": row["annotator"],
                "guide_version": row["guide_version"],
                "annotated_at": row["created_at"],
            },
        )
        if row["created_at"] > record["annotated_at"]:
            record["annotated_at"] = row["created_at"]
            record["guide_version"] = row["guide_version"]
        if row["attribute"] in attribute_names:
            record[row["attribute"]] = _csv_cell(json.loads(row["value"]))

    fieldnames = ["name", *attribute_names, "family", "image_id", "annotator", "guide_version", "annotated_at"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        for record in records.values():
            writer.writerow(record)
    return len(records)


# ---- Import from the old tool ----

LEGACY_ANNOTATOR = "legacy_miat"
LEGACY_GUIDE_VERSION = "legacy-miat"
DEFAULT_IMPORT_REVIEW_PATH = Path("export/legacy_import_review.csv")

# Old-tool CSV column names that attributes.json has since renamed.
LEGACY_COLUMN_ALIASES = {
    "Antennae": "Antennas",
    "Docking/Grappling Fixtures": "Docking Systems",
    "Active External Sensor": "Active External Sensors",
    "Deployment Mechanism": "Deployment Mechanisms",
    "Propellant Mechanism": "Propulsion Mechanism",
}

# Option spellings from the old tool's own attribute lists that
# attributes.json has since corrected. Only fixed option names belong here --
# typos annotators typed themselves are kept exactly as written.
LEGACY_VALUE_ALIASES = {
    "Atimeter": "Altimeter",
    "Monoropellant": "Monopropellant",
    # Typed into DropdownOther boxes by annotators.
    "Tripple Wing": "Triple Wing",
    "Doublw Wing": "Double Wing",
    "Quad Wing": "Quadruple Wing",
    "Quad WIng": "Quadruple Wing",
    "Octogonal": "Octagonal",
}

# Answers any attribute can legitimately have -- never evidence that a value
# landed in the wrong column.
SHARED_VALUES = {"", "None", "none", "Unknown", UNUSABLE}

_COMBO_SPLIT = re.compile(r",\s*(?:and\s+)?|\s+and\s+")


def map_legacy_columns(fieldnames: list[str], schema: dict) -> tuple[dict[str, str], list[str]]:
    """Old CSV column -> current attribute name: exact match, then a known
    rename, then a case-insensitive match. Returns (mapping, unmapped)."""
    names = list(flat_attributes(schema))
    by_lower = {n.lower(): n for n in names}
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for column in fieldnames:
        if column == "name":
            continue
        target = column if column in names else None
        if target is None and LEGACY_COLUMN_ALIASES.get(column) in names:
            target = LEGACY_COLUMN_ALIASES[column]
        if target is None:
            target = by_lower.get(column.lower())
        if target is None or target in mapping.values():
            unmapped.append(column)
        else:
            mapping[column] = target
    return mapping, unmapped


def parse_legacy_cell(text: str):
    """The old tool wrote cells with str(): lists as "['Patch', 'Omni']",
    everything else as plain text."""
    text = (text or "").strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(value, list):
            return [str(v) for v in value]
    return text


def _pieces(value) -> list[str]:
    """Individual answers in a cell, including the "A and B" / "A, B, and C"
    combinations annotators typed into DropdownOther boxes."""
    values = value if isinstance(value, list) else [value]
    return [p.strip() for v in values for p in _COMBO_SPLIT.split(str(v)) if p.strip()]


def convert_legacy_value(spec: list, value):
    """Renamed options -> current names, and a plain-text cell under an
    attribute that is now Multi-Select -> a list of its individual answers
    ("Solar Gimbals and Sail Deployer" -> ["Solar Gimbals", "Sail Deployer"])."""
    if value == UNUSABLE:
        return value
    if isinstance(value, list):
        return [LEGACY_VALUE_ALIASES.get(v, v) for v in value]
    if spec[0] == "Multi-Select":
        return [LEGACY_VALUE_ALIASES.get(p, p) for p in _pieces(value)] or ["None"]
    return LEGACY_VALUE_ALIASES.get(value, value)


def misplaced_values(attribute: str, value, schema: dict) -> list[str]:
    """Answers in this cell that are options of a *different* attribute and
    not of this one -- the signature of a row the old CSV writer put under
    the wrong header (e.g. "Double Wing" under Manufacturer)."""
    options = {name: set(spec[1]) for name, spec in flat_attributes(schema).items() if spec[0] in OPTION_TYPES}
    own = options.get(attribute, set())
    others = set().union(*(opts for name, opts in options.items() if name != attribute)) if options else set()
    return [p for p in _pieces(value) if p not in SHARED_VALUES and p not in own and p in others]


def _fits_schema(spec: list, value) -> bool:
    if value == UNUSABLE:
        return True
    try:
        normalize_value(spec, value)
    except ValueError:
        return False
    return True


def import_legacy_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    schema: dict,
    annotator: str = LEGACY_ANNOTATOR,
    dry_run: bool = False,
    review_path: Path | None = DEFAULT_IMPORT_REVIEW_PATH,
) -> dict:
    """Brings the old tool's annotatedInfo.csv into the annotations table so
    those images count as done. Rows are matched to images by file name.

    Values are kept exactly as the old tool wrote them (lists parsed back
    into lists), never corrected -- guide_version 'legacy-miat' marks them,
    and off_schema counts the ones that don't fit today's attribute list.
    Rows are skipped, never guessed at, when they match no image, match more
    than one, look column-shifted, or the image already has annotations from
    this annotator; skipped rows are written to review_path with the reason.
    Nothing is written to the DB on a dry run."""
    annotator = validate_annotator(annotator)
    if not schema:
        raise ValueError("There are no attributes yet -- set up the attribute list before importing.")
    if not csv_path.is_file():
        raise ValueError(f"No CSV at {csv_path}")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "name" not in fieldnames:
        raise ValueError(f"{csv_path} has no 'name' column -- is it the old tool's annotation CSV?")

    mapping, unmapped = map_legacy_columns(fieldnames, schema)
    attributes = flat_attributes(schema)

    images_by_name: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, path FROM images"):
        images_by_name.setdefault(Path(row["path"]).name, []).append(row["id"])
    already_done = {
        row["image_id"]
        for row in conn.execute(
            "SELECT DISTINCT image_id FROM annotations WHERE annotator = ? AND attribute != 'variant'", (annotator,)
        )
    }

    counts = {"unmatched": 0, "ambiguous": 0, "misaligned": 0, "already_annotated": 0, "imported": 0}
    off_schema: dict[str, int] = {}
    skipped: list[dict] = []
    to_insert: list[tuple] = []
    now = _now()

    for row in rows:
        name = (row.get("name") or "").strip()
        matches = images_by_name.get(name, [])
        values = {
            target: convert_legacy_value(attributes[target], parse_legacy_cell(row.get(column, "")))
            for column, target in mapping.items()
        }
        misplaced = {attr: bad for attr, value in values.items() if (bad := misplaced_values(attr, value, schema))}

        reason = None
        if not matches:
            reason, key = "no image in the database with this file name", "unmatched"
        elif len(matches) > 1:
            reason, key = f"{len(matches)} images share this file name", "ambiguous"
        elif misplaced:
            detail = "; ".join(f"{attr}: {', '.join(bad)}" for attr, bad in misplaced.items())
            reason, key = f"values look like they're in the wrong column ({detail})", "misaligned"
        elif matches[0] in already_done:
            reason, key = f"image already has annotations from {annotator}", "already_annotated"

        if reason:
            counts[key] += 1
            skipped.append({"reason": reason, **row})
            continue

        counts["imported"] += 1
        already_done.add(matches[0])
        for attr, value in values.items():
            if not _fits_schema(attributes[attr], value):
                off_schema[attr] = off_schema.get(attr, 0) + 1
            to_insert.append((matches[0], attr, json.dumps(value), annotator, LEGACY_GUIDE_VERSION, now))

    if not dry_run and to_insert:
        with conn:
            conn.executemany(
                """INSERT INTO annotations (image_id, attribute, value, annotator, guide_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(image_id, attribute, annotator) DO NOTHING""",
                to_insert,
            )

    if review_path is not None and skipped:
        review_path.parent.mkdir(parents=True, exist_ok=True)
        with open(review_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["reason", *fieldnames], extrasaction="ignore")
            writer.writeheader()
            writer.writerows(skipped)

    return {
        "rows": len(rows),
        **counts,
        "dry_run": dry_run,
        "annotator": annotator,
        "column_mapping": mapping,
        "unmapped_columns": unmapped,
        "missing_attributes": [name for name in attributes if name not in mapping.values()],
        "off_schema": off_schema,
        "review_path": str(review_path) if review_path is not None and skipped else None,
    }


if __name__ == "__main__":
    import sys

    schema_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SCHEMA_PATH
    loaded = load_schema(schema_path)
    attribute_count = len(flat_attributes(loaded))
    print(f"{schema_path}: {len(loaded)} categories, {attribute_count} attributes, version {schema_version(loaded)}")
    for category, attributes in loaded.items():
        print(f"  {category}")
        for name, spec in attributes.items():
            print(f"    {name}: {spec[0]}{' ' + ', '.join(spec[1]) if spec[0] in OPTION_TYPES else ''}")
