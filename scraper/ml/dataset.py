"""Training set for the first-pass attribute model.

Turns what annotators have recorded -- the old tool's annotatedInfo.csv and
the annotations table -- into one example per (image, annotator) with a
training label per attribute, and reports how many examples every answer
has so rare answers don't get a head they can't learn.

What becomes a label, and what doesn't:
- Only attributes a classifier can learn: Dropdown / DropdownOther (pick
  one), Multi-Select (each option present or not) and the Present / Not
  present questions. Free-text types (Manufacturer is a Text Box) are left
  out -- they aren't something a picture shows.
- Unknown, UNUSABLE and blank answers are ignored, not learned: "can't tell"
  says nothing about the satellite. Multi-Select "None" IS a label -- the
  annotator looked and none of the options are there.
- Free text typed into a DropdownOther, or a multi-select answer that isn't
  one of today's options, is ignored for that attribute only.
- Whole rows are dropped when they're unusable, have no image file, or look
  column-shifted by the old tool's CSV writer (same check the importer
  uses) -- shifted rows can be repaired later and fed back in.

Each annotator's answers stay a separate example, tagged with their name --
several CSVs (one per annotator) can go in at once. An image two people
labeled is two examples here; combining them (majority vote as ground
truth) is a later step, and keeping them apart is what makes it possible.

Examples are split into train/test by satellite family, never by image, so
near-identical photos of one satellite can't sit on both sides and make the
test score look better than it is. The split is a hash of the family name:
it doesn't change as data is added. Families that share an identical image
file (the old data filed some photos under two names, e.g. ELEKTRO and
ELEKTRO-L) are tied together and go to the same side.

Portable between machines: image paths inside the project are stored
relative to it, and each example carries its file's SHA-256, so a training
set built here still lines up with the images (and their embeddings) after
the project and its data/ folder are copied to a bigger machine. data/
(images + annotation CSVs, gitignored) is the default source -- see
data_sources().
"""

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from scraper.stages import annotate
from scraper.stages.annotate_folder import IMAGE_EXTENSIONS
from scraper.stages.embed import file_sha256

DEFAULT_OUT_DIR = Path("training")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_TEST_SHARE = 0.2
# Fewer training examples than this and an answer is reported as too rare to
# learn yet (it stays in the data; the heads decide what to do with it).
MIN_EXAMPLES = 10

SINGLE_TYPES = ("Dropdown", "DropdownOther", "Check Button", "Single-Select")
MULTI_TYPES = ("Multi-Select",)
RADIO_LABELS = ("Present", "Not Present")

SOURCE_CSV = "old_csv"
SOURCE_DB = "database"

# "3CAT_01.jpg" -> 3CAT, "A-1_wikimedia_11.jpg" -> A-1, "GPS_IIF_02.png" -> GPS_IIF:
# the old tool's file names were <family>[_<source>]_<number>.
_FAMILY_FROM_NAME = re.compile(r"^(?P<family>.+?)(?:_[a-z]+)?_\d+$")


# Answers that mean "can't tell from the image" without being spelled Unknown.
# "No Frame of Reference" was Mass's preselected first option in the old tool,
# so it mostly records a field nobody changed; "Unknown (Black and White
# Image)" and the like are caught by the "unknown" prefix.
UNKNOWN_EQUIVALENTS = {"no frame of reference"}


class DatasetError(ValueError):
    pass


def _means_unknown(value) -> bool:
    if isinstance(value, list):
        return any(_means_unknown(v) for v in value)
    text = value.strip().lower() if isinstance(value, str) else ""
    return text.startswith(annotate.UNKNOWN.lower()) or text in UNKNOWN_EQUIVALENTS


def trainable_attributes(schema: dict) -> dict[str, list]:
    learnable = SINGLE_TYPES + MULTI_TYPES
    return {name: spec for name, spec in annotate.flat_attributes(schema).items() if spec[0] in learnable}


def label_options(spec: list) -> list[str]:
    """The answers a head for this attribute chooses between."""
    return list(RADIO_LABELS) if spec[0] in ("Check Button", "Single-Select") else list(spec[1])


def training_label(spec: list, value) -> tuple[object, str | None]:
    """A stored answer -> (label, None), or (None, why it was ignored).
    Single-choice labels are one option; multi-select labels are the list
    of options present ([] for "None")."""
    if value is None or value == "" or value == [] or value == annotate.UNUSABLE:
        return None, "blank"
    if _means_unknown(value):
        return None, "unknown"

    options = label_options(spec)
    if spec[0] in MULTI_TYPES:
        values = value if isinstance(value, list) else [value]
        picked = [v for v in values if v != "None"]
        if any(v not in options for v in picked):
            return None, "not_an_option"
        return [o for o in options if o in picked], None

    if isinstance(value, list):
        return None, "not_an_option"
    if value not in options:
        return None, "not_an_option"
    return value, None


def family_from_name(name: str) -> str:
    match = _FAMILY_FROM_NAME.match(Path(name).stem)
    return (match.group("family") if match else Path(name).stem).upper()


def split_for(family: str, test_share: float = DEFAULT_TEST_SHARE) -> str:
    bucket = int.from_bytes(hashlib.sha256(family.strip().upper().encode()).digest()[:4], "big") % 1000
    return "test" if bucket < test_share * 1000 else "train"


def parse_csv_args(values: list[str]) -> list[tuple[str, Path]]:
    """--csv values -> (annotator, path). "alice=C:/old/annotatedInfo.csv"
    names the annotator; a bare path is fine for a single CSV, or for
    several whose file names differ (the name is the file name then)."""
    pairs = []
    for value in values:
        if "=" in value and not Path(value).is_file():
            name, _, path = value.partition("=")
            pairs.append((name.strip(), Path(path.strip())))
        else:
            pairs.append((None, Path(value)))
    if len(pairs) == 1 and pairs[0][0] is None:
        return [(annotate.LEGACY_ANNOTATOR, pairs[0][1])]

    named = []
    for name, path in pairs:
        name = name or path.stem
        try:
            named.append((annotate.validate_annotator(name), path))
        except ValueError as e:
            raise DatasetError(f"{path}: {e}") from e
    names = [name for name, _ in named]
    if len(set(names)) < len(names):
        raise DatasetError(
            "Two CSVs would be filed under the same annotator -- say whose each one is, "
            "e.g. --csv alice=path/to/annotatedInfo.csv --csv bob=other/annotatedInfo.csv"
        )
    return named


def _pick_file(found: list[Path], hashes: dict[Path, str]) -> Path | None:
    """One image file for a name. Several annotators' folders often hold the
    same image; that's fine as long as the copies are identical."""
    if len(found) == 1:
        return found[0]
    for path in found:
        if path not in hashes:
            hashes[path] = file_sha256(path)
    return found[0] if len({hashes[path] for path in found}) == 1 else None


def data_sources(data_dir: Path = DEFAULT_DATA_DIR) -> tuple[list[tuple[str, Path]], list[Path]]:
    """The annotation CSVs and image folders in a data folder:
    data/annotations.csv (one CSV, no annotator name needed) and/or
    data/annotations/<annotator>.csv (one per annotator, named by file),
    with the images in data/images/ (subfolders searched too)."""
    csvs: list[tuple[str, Path]] = []
    if (data_dir / "annotations.csv").is_file():
        csvs.append((annotate.LEGACY_ANNOTATOR, data_dir / "annotations.csv"))
    if (data_dir / "annotations").is_dir():
        for path in sorted((data_dir / "annotations").glob("*.csv")):
            try:
                csvs.append((annotate.validate_annotator(path.stem), path))
            except ValueError as e:
                raise DatasetError(f"{path}: {e}") from e
    images = [data_dir / "images"] if (data_dir / "images").is_dir() else []
    return csvs, images


def _portable(path: str) -> str:
    """A path inside the project, relative to it (forward slashes, so it
    works on Windows, Mac and Linux alike); anything else as-is."""
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _assign_splits(examples: list[dict], test_share: float) -> int:
    """Sets each example's split by family, with families that share an
    identical image tied to the same side. Returns how many families were
    tied to another."""
    parent: dict[str, str] = {}

    def find(family: str) -> str:
        parent.setdefault(family, family)
        while parent[family] != family:
            parent[family] = parent[parent[family]]
            family = parent[family]
        return family

    first_family_with: dict[str, str] = {}
    for example in examples:
        find(example["family"])
        other = first_family_with.setdefault(example["sha256"], example["family"])
        parent[find(example["family"])] = find(other)

    members: dict[str, list[str]] = {}
    for family in parent:
        members.setdefault(find(family), []).append(family)
    for example in examples:
        # The tied group's alphabetically-first family decides, so the side
        # stays put as long as the group doesn't change.
        example["split"] = split_for(min(members[find(example["family"])]), test_share)
    return sum(len(group) for group in members.values() if len(group) > 1)


def _index_images(image_dirs: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for directory in image_dirs:
        directory = directory.expanduser()
        if not directory.is_dir():
            raise DatasetError(f"Image folder not found: {directory}")
        for path in directory.rglob("*"):
            if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in IMAGE_EXTENSIONS:
                index.setdefault(path.name, []).append(path)
    return index


def _labels(values: dict, attributes: dict, ignored: dict[str, Counter]) -> dict:
    labels = {}
    for name, spec in attributes.items():
        label, reason = training_label(spec, values.get(name))
        if reason:
            ignored[name][reason] += 1
        else:
            labels[name] = label
    return labels


def _db_examples(conn: sqlite3.Connection, attributes: dict, ignored: dict[str, Counter]) -> tuple[list[dict], int]:
    query = """SELECT annotations.image_id, annotations.annotator, annotations.attribute, annotations.value,
                      images.path, images.family
               FROM annotations JOIN images ON images.id = annotations.image_id
               WHERE images.status = 'active' AND annotations.attribute != 'variant'
                 AND annotations.annotator NOT IN ('human_review')"""
    grouped: dict[tuple[int, str], dict] = {}
    for row in conn.execute(query):
        record = grouped.setdefault(
            (row["image_id"], row["annotator"]),
            {"path": row["path"], "family": row["family"], "annotator": row["annotator"], "values": {}},
        )
        record["values"][row["attribute"]] = json.loads(row["value"])

    examples, missing = [], 0
    for record in grouped.values():
        if record["values"] and all(v == annotate.UNUSABLE for v in record["values"].values()):
            continue
        if not Path(record["path"]).is_file():
            missing += 1
            continue
        examples.append(
            {
                "image": str(record["path"]),
                "name": Path(record["path"]).name,
                "family": (record["family"] or family_from_name(Path(record["path"]).name)).upper(),
                "source": SOURCE_DB,
                "annotator": record["annotator"],
                "labels": _labels(record["values"], attributes, ignored),
            }
        )
    return examples, missing


def _csv_examples(
    annotator: str,
    csv_path: Path,
    schema: dict,
    attributes: dict,
    image_index: dict[str, list[Path]],
    db_images: dict[str, list[tuple[str, str]]],
    ignored: dict[str, Counter],
    hashes: dict[Path, str],
) -> tuple[list[dict], list[dict], Counter]:
    if not csv_path.is_file():
        raise DatasetError(f"No CSV at {csv_path}")
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "name" not in fieldnames:
        raise DatasetError(f"{csv_path} has no 'name' column -- is it the old tool's annotation CSV?")

    mapping, _ = annotate.map_legacy_columns(fieldnames, schema)
    every_attribute = annotate.flat_attributes(schema)
    examples, skipped, reasons = [], [], Counter()

    for row in rows:
        name = (row.get("name") or "").strip()
        values = {
            target: annotate.convert_legacy_value(
                every_attribute[target], annotate.parse_legacy_cell(row.get(column, ""))
            )
            for column, target in mapping.items()
        }

        reason = None
        found = image_index.get(name, [])
        chosen = _pick_file(found, hashes) if found else None
        in_db = db_images.get(name, [])
        if values and all(v == annotate.UNUSABLE for v in values.values()):
            reason = "marked unusable"
        elif any(annotate.misplaced_values(attr, value, schema) for attr, value in values.items()):
            reason = "values look column-shifted"
        elif (found and chosen is None) or (not found and len(in_db) > 1):
            reason = "several different image files share this name"
        elif not found and not in_db:
            reason = "no image file with this name"

        if reason:
            reasons[reason] += 1
            skipped.append({"annotator": annotator, "reason": reason, "name": name})
            continue

        path, family = (str(chosen), None) if chosen else in_db[0]
        examples.append(
            {
                "image": path,
                "name": name,
                "family": (family or (in_db[0][1] if in_db else None) or family_from_name(name)).upper(),
                "source": SOURCE_CSV,
                "annotator": annotator,
                "labels": _labels({k: v for k, v in values.items() if k in attributes}, attributes, ignored),
            }
        )
    return examples, skipped, reasons


def _attribute_report(name: str, spec: list, examples: list[dict], ignored: Counter) -> dict:
    options = label_options(spec)
    counts = {option: {"train": 0, "test": 0} for option in options}
    labeled = {"train": 0, "test": 0}
    for example in examples:
        if name not in example["labels"]:
            continue
        label = example["labels"][name]
        labeled[example["split"]] += 1
        for option in label if isinstance(label, list) else [label]:
            counts[option][example["split"]] += 1

    rare = [o for o, c in counts.items() if c["train"] < MIN_EXAMPLES]
    if spec[0] in MULTI_TYPES:
        # Each option is its own yes/no question: learnable with enough of both.
        learnable = [o for o, c in counts.items() if MIN_EXAMPLES <= c["train"] <= labeled["train"] - MIN_EXAMPLES]
    else:
        learnable = [o for o in options if o not in rare]
    return {
        "type": spec[0],
        "labeled": labeled,
        "ignored": dict(ignored),
        "options": counts,
        "rare_options": rare,
        "trainable": len(learnable) >= (1 if spec[0] in MULTI_TYPES else 2),
    }


def build_training_set(
    conn: sqlite3.Connection | None,
    schema: dict,
    csvs: Path | list[tuple[str, Path]] | None = None,
    image_dirs: list[Path] | None = None,
    out_dir: Path | None = DEFAULT_OUT_DIR,
    test_share: float = DEFAULT_TEST_SHARE,
) -> dict:
    """Builds the examples and writes dataset.jsonl, report.json and (for old
    CSVs) skipped.csv into out_dir. Returns the report. `csvs` is a list of
    (annotator, CSV path), or one path for a single unnamed CSV. With
    out_dir=None nothing is written."""
    if isinstance(csvs, Path):
        csvs = [(annotate.LEGACY_ANNOTATOR, csvs)]
    attributes = trainable_attributes(schema)
    if not attributes:
        raise DatasetError("The attribute list has nothing a classifier can learn (no dropdowns or multi-selects).")
    if not 0 < test_share < 1:
        raise DatasetError("test_share has to be between 0 and 1.")
    if conn is None and not csvs:
        raise DatasetError("Nothing to build from: give an old-tool CSV, the database, or both.")

    ignored = {name: Counter() for name in attributes}
    examples, db_missing = _db_examples(conn, attributes, ignored) if conn is not None else ([], 0)

    skipped, skip_reasons, skipped_by_annotator = [], Counter(), {}
    if csvs:
        db_images: dict[str, list[tuple[str, str]]] = {}
        if conn is not None:
            for row in conn.execute("SELECT path, family FROM images WHERE status = 'active'"):
                if Path(row["path"]).is_file():
                    db_images.setdefault(Path(row["path"]).name, []).append((row["path"], row["family"]))
        image_index, hashes = _index_images(image_dirs or []), {}
        # The importer may already have copied some of these rows into the DB.
        already = {(e["name"], e["annotator"]) for e in examples}
        for annotator, csv_path in csvs:
            csv_examples, csv_skipped, reasons = _csv_examples(
                annotator, csv_path, schema, attributes, image_index, db_images, ignored, hashes
            )
            examples += [e for e in csv_examples if (e["name"], e["annotator"]) not in already]
            skipped += csv_skipped
            skip_reasons += reasons
            if reasons:
                skipped_by_annotator[annotator] = dict(reasons)

    examples = [e for e in examples if e["labels"]]
    hashes: dict[Path, str] = {}
    for example in examples:
        path = Path(example["image"])
        if path not in hashes:
            hashes[path] = file_sha256(path)
        example["sha256"] = hashes[path]
        example["image"] = _portable(example["image"])
    tied_families = _assign_splits(examples, test_share)
    examples.sort(key=lambda e: (e["split"], e["family"], e["name"], e["annotator"]))

    splits = Counter(e["split"] for e in examples)
    annotators_per_image = Counter(e["name"] for e in examples)
    report = {
        "schema_version": annotate.schema_version(schema),
        "test_share": test_share,
        "min_examples": MIN_EXAMPLES,
        "examples": {"train": splits["train"], "test": splits["test"]},
        "families": {s: len({e["family"] for e in examples if e["split"] == s}) for s in ("train", "test")},
        "sources": dict(Counter(e["source"] for e in examples)),
        "annotators": dict(Counter(e["annotator"] for e in examples)),
        "images_with_several_annotators": sum(n > 1 for n in annotators_per_image.values()),
        "families_tied_by_shared_images": tied_families,
        "skipped_rows": dict(skip_reasons),
        "skipped_rows_by_annotator": skipped_by_annotator,
        "database_images_missing": db_missing,
        "attributes": {
            name: _attribute_report(name, spec, examples, ignored[name]) for name, spec in attributes.items()
        },
        "left_out_attributes": [n for n in annotate.flat_attributes(schema) if n not in attributes],
    }

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "dataset.jsonl", "w", encoding="utf-8") as f:
            for example in examples:
                f.write(json.dumps(example) + "\n")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if skipped:
            with open(out_dir / "skipped.csv", "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["annotator", "reason", "name"])
                writer.writeheader()
                writer.writerows(skipped)
        report["out_dir"] = str(out_dir)
    return report


def load_examples(path: Path = DEFAULT_OUT_DIR / "dataset.jsonl") -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def format_report(report: dict) -> str:
    """The report as plain text, for the CLI."""
    lines = [
        f"Examples: {report['examples']['train']} train / {report['examples']['test']} test "
        f"({report['families']['train']} / {report['families']['test']} families, split by family)",
    ]
    if report["annotators"]:
        lines.append("Examples per annotator: " + ", ".join(f"{a} {n}" for a, n in report["annotators"].items()))
    if report.get("families_tied_by_shared_images"):
        lines.append(
            f"Families kept on one side of the split because they share an identical image: "
            f"{report['families_tied_by_shared_images']}"
        )
    if report["images_with_several_annotators"]:
        lines.append(f"Images labeled by more than one annotator: {report['images_with_several_annotators']}")
    for annotator, reasons in report["skipped_rows_by_annotator"].items():
        lines.append(f"Old-CSV rows left out ({annotator}): " + ", ".join(f"{n} {r}" for r, n in reasons.items()))
    if report["left_out_attributes"]:
        lines.append("Not learnable from an image (free text): " + ", ".join(report["left_out_attributes"]))
    for name, info in report["attributes"].items():
        ignored = ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in info["ignored"].items()) or "none"
        status = "" if info["trainable"] else "  [not enough data to train yet]"
        labeled = info["labeled"]
        lines.append(f"\n{name} ({info['type']}): {labeled['train']} train / {labeled['test']} test{status}")
        lines.append(f"  ignored: {ignored}")
        for option, c in sorted(info["options"].items(), key=lambda kv: -kv[1]["train"]):
            flag = "  (rare)" if option in info["rare_options"] else ""
            lines.append(f"    {option:32} {c['train']:5} train {c['test']:5} test{flag}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from scraper.db.connection import get_connection

    csv_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    dirs = [Path(p) for p in sys.argv[2:]]
    db = get_connection(Path("scraper.db")) if Path("scraper.db").exists() else None
    schema = annotate.load_schema(annotate.DEFAULT_SCHEMA_PATH)
    print(format_report(build_training_set(db, schema, csv_arg, dirs, out_dir=None)))
