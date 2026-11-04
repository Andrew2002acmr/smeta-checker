"""Bounded native resource continuations, with exact reviewed-row admission.

No norm allowlist, inferred units, OCR, model, active release or global path.
Regeneration is read-only; insertion belongs to the caller's staging transaction.
"""

from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re

import pymupdf
from pymupdf.table import find_tables

VERSION = "native-resource-continuation-proposal-v2-per-column"
# Existing offline build mechanism relocated into the agreed checkout.
UPSTREAM = {
    "path": "smeta-checker-local/comprehensive-road-kb/tools/resource_continuation.py",
    "sha256": "f813bb06f8442bcb0ef47fe8e5dbf2a8b06628aeaed8fb220f75a8b6da52bf32",
    "version": "native-resource-continuation-proposal-v1",
}
TOLERANCE = 0.75
NORM = re.compile(r"\d{2}-\d{2}-\d{3}-\d{2,3}")
RESOURCE = re.compile(r"(?:\d{1,2}(?:[.\-]\d+){2,}|[1-4])")


def js(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def sha(value):
    return hashlib.sha256(value).hexdigest()


class Native:
    def __init__(self, page):
        self.lines = []
        for block in page.get_text("rawdict").get("blocks", []):
            for line in block.get("lines", []):
                chars = [
                    char
                    for span in line.get("spans", [])
                    for char in span.get("chars", [])
                ]
                if chars:
                    self.lines.append(chars)

    def read(self, box):
        x0, y0, x1, y1 = box
        found = []
        for chars in self.lines:
            selected = [
                char
                for char in chars
                if x0 <= (char["bbox"][0] + char["bbox"][2]) / 2 < x1
                and y0 <= (char["bbox"][1] + char["bbox"][3]) / 2 < y1
            ]
            if selected:
                found.append(
                    (
                        min(char["bbox"][1] for char in selected),
                        min(char["bbox"][0] for char in selected),
                        "".join(char["c"] for char in selected),
                    )
                )
        return "\n".join(part[2].strip() for part in sorted(found) if part[2].strip())


def grids(page, native):
    found = find_tables(page, strategy="lines_strict", use_layout=False)
    if found is None:
        return []
    return [
        dict(
            table_id=f"t{index + 1}",
            bbox=list(table.bbox),
            headers=[
                dict(column=column, bbox=list(box), text=native.read(box))
                for column, box in enumerate(table.rows[0].cells)
                if box
            ],
        )
        for index, table in enumerate(found.tables)
        if table.rows
    ]


def context_for(table, page_number, height):
    by_column = {header["column"]: header for header in table["headers"]}
    if len(by_column) != len(table["headers"]):
        return None
    columns = {
        column: re.sub(r"\s+", "", header["text"])
        for column, header in by_column.items()
        if NORM.fullmatch(re.sub(r"\s+", "", header["text"]))
    }
    # A continuation must follow a physical grid near the preceding page bottom.
    if not columns or not all(column in by_column for column in (0, 1, 2)):
        return None
    if any(column < 3 for column in columns) or len(set(columns.values())) != len(
        columns
    ):
        return None
    if table["bbox"][3] < height * 0.90:
        return None
    if "Кодресурса" not in re.sub(r"\s+", "", by_column[0]["text"]):
        return None
    if len({code.rsplit("-", 1)[0] for code in columns.values()}) != 1:
        return None
    return dict(page=page_number, table=table, columns=columns, by_column=by_column)


def continuation_reason(previous, table, page, page_number, native):
    if not previous or previous["page"] != page_number - 1:
        return "no_adjacent_bottom_resource_header"
    if table["bbox"][1] > page.rect.height * 0.12:
        return "not_at_page_top"
    current = {header["column"]: header for header in table["headers"]}
    if len(current) != len(table["headers"]):
        return "duplicate_column_identity"
    if any(NORM.fullmatch(re.sub(r"\s+", "", h["text"])) for h in current.values()):
        return "has_own_norm_header"
    if set(current) != set(previous["by_column"]):
        return "column_count_changed"
    # Code/name/unit descriptors belong to every resource column. Their
    # geometry is a table-level gate; numeric columns are admitted separately.
    for column in (0, 1, 2):
        header = previous["by_column"][column]
        if any(
            abs(header["bbox"][axis] - current[column]["bbox"][axis]) > TOLERANCE
            for axis in (0, 2)
        ):
            return "descriptor_geometry_changed"
    ordered = sorted(current)
    if any(current[c]["bbox"][0] >= current[c]["bbox"][2] for c in ordered) or any(
        abs(current[a]["bbox"][2] - current[b]["bbox"][0]) > TOLERANCE
        for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        return "noncontiguous_column_geometry"
    if not admitted_columns(previous, table):
        return "no_stable_norm_columns"
    if len(current) != len(previous["by_column"]):
        return "column_count_changed"
    # PDF grids may vertically merge several resource-code cells into the
    # first row. Every literal line must still be a resource code.
    first_codes = [
        part.strip() for part in current[0]["text"].splitlines() if part.strip()
    ]
    if not first_codes or not all(RESOURCE.fullmatch(part) for part in first_codes):
        return "first_row_not_a_resource"
    table_text = native.read(table["bbox"])
    if re.search(r"Таблица\s+ГЭСН|Измеритель\s*:", table_text, re.I):
        return "new_definition_inside_grid"
    # Above the grid only the normal running document header is tolerated.
    preceding = native.read((table["bbox"][0], 0, table["bbox"][2], table["bbox"][1]))
    if re.search(r"Таблица\s+ГЭСН|Измеритель\s*:|Состав\s+работ", preceding, re.I):
        return "new_definition_before_grid"
    return None


def admitted_columns(previous, table):
    """One-to-one ordinal columns; unchanged edges, no value-based remapping."""
    current = {header["column"]: header for header in table["headers"]}
    return {
        column: code
        for column, code in previous["columns"].items()
        if column in current
        and all(
            abs(
                previous["by_column"][column]["bbox"][axis]
                - current[column]["bbox"][axis]
            )
            <= TOLERANCE
            for axis in (0, 2)
        )
    }


def extract_proposal(source, previous, table, native, page_number, norm_ids, existing):
    headings = {header["column"]: header["bbox"] for header in table["headers"]}
    left, right = headings[0][0], headings[0][2]
    anchors = []
    for chars in native.lines:
        selected = [
            char
            for char in chars
            if left <= (char["bbox"][0] + char["bbox"][2]) / 2 < right
            and table["bbox"][1]
            <= (char["bbox"][1] + char["bbox"][3]) / 2
            < table["bbox"][3]
        ]
        if selected:
            code = clean("".join(char["c"] for char in selected))
            if RESOURCE.fullmatch(code):
                anchors.append(
                    (
                        min(char["bbox"][1] for char in selected),
                        max(char["bbox"][3] for char in selected),
                        code,
                    )
                )
    anchors = sorted(set(anchors))
    anchor_counts = Counter(code for _top, _bottom, code in anchors)
    proposed, rejected = [], []
    eligible = admitted_columns(previous, table)
    rejected.extend(
        dict(
            page=page_number, norm=code, column=column, reason="column_geometry_changed"
        )
        for column, code in previous["columns"].items()
        if column not in eligible
    )
    for index, (top, bottom, resource_code) in enumerate(anchors):
        if anchor_counts[resource_code] != 1:
            rejected.append(
                dict(
                    page=page_number,
                    code=resource_code,
                    reason="ambiguous_duplicate_continuation_resource",
                )
            )
            continue
        stop = (
            anchors[index + 1][0] - 0.15
            if index + 1 < len(anchors)
            else table["bbox"][3]
        )
        row_top = top - 0.15
        unit_box = [headings[2][0], row_top, headings[2][2], min(stop, bottom + 0.15)]
        name_box = [headings[1][0], row_top, headings[1][2], stop]
        unit, name = clean(native.read(unit_box)), clean(native.read(name_box))
        if (
            not unit
            or not name
            or not re.fullmatch(
                r"(?:чел\.-ч|маш\.-ч|м[23]|м|т|кг|шт\.?|га|1000\s*шт\.?|100\s*шт\.?)",
                unit,
            )
        ):
            if unit:
                rejected.append(
                    dict(
                        page=page_number,
                        code=resource_code,
                        reason="unsupported_unit_or_name",
                        unit=unit,
                    )
                )
            continue
        for column, normcode in eligible.items():
            box = [
                headings[column][0],
                row_top,
                headings[column][2],
                min(stop, bottom + 0.15),
            ]
            value = clean(native.read(box))
            if not value:
                continue
            if normcode not in norm_ids:
                rejected.append(
                    dict(
                        page=page_number,
                        code=resource_code,
                        norm=normcode,
                        reason="unknown_norm",
                    )
                )
                continue
            if re.fullmatch(r"\d+(?:[.,]\d+)?", value):
                quantity, kind = str(Decimal(value.replace(",", "."))), "numeric"
            elif value in ("П", "п"):
                quantity, kind = None, "project"
            else:
                rejected.append(
                    dict(
                        page=page_number,
                        code=resource_code,
                        norm=normcode,
                        reason="ambiguous_quantity",
                        raw=value,
                    )
                )
                continue
            key = (norm_ids[normcode], resource_code)
            if key in existing:
                rejected.append(
                    dict(
                        page=page_number,
                        code=resource_code,
                        norm=normcode,
                        reason="resource_already_exists_no_overwrite",
                    )
                )
                continue
            rid = (
                "resource-"
                + sha(
                    js(
                        (
                            source["id"],
                            page_number - 1,
                            table["table_id"],
                            resource_code,
                            column,
                            top,
                        )
                    ).encode()
                )[:32]
            )
            citation = dict(
                source_id=source["id"],
                physical_page=page_number,
                bbox=box,
                coordinate_system="pdf-points",
            )
            proposed.append(
                dict(
                    id=rid,
                    norm_id=norm_ids[normcode],
                    resource_code=resource_code,
                    name=name,
                    unit=unit,
                    raw_quantity=value,
                    quantity=quantity,
                    quantity_kind=kind,
                    status="extracted_native_geometric_row_unreviewed",
                    citation_json=js(citation),
                    proof=dict(
                        version=VERSION,
                        source_sha256=source["sha256"],
                        norm_code=normcode,
                        header_page=previous["page"],
                        header_table_id=previous["table"]["table_id"],
                        header_bbox=previous["by_column"][column]["bbox"],
                        header_text=previous["by_column"][column]["text"],
                        table_id=table["table_id"],
                        table_bbox=table["bbox"],
                        unit_bbox=unit_box,
                        name_bbox=name_box,
                        numeric_bbox=box,
                        inherited_header_geometry_tolerance_pdf_points=TOLERANCE,
                        admitted_column=column,
                        header_inheritance="independent_unchanged_column",
                        no_model_calls=True,
                        no_ocr_calls=True,
                    ),
                )
            )
    multiplicity = Counter((row["norm_id"], row["resource_code"]) for row in proposed)
    safe = []
    for row in proposed:
        if multiplicity[(row["norm_id"], row["resource_code"])] != 1:
            rejected.append(
                dict(id=row["id"], reason="ambiguous_duplicate_continuation_resource")
            )
        else:
            safe.append(row)
    return safe, rejected


RESOURCE_FIELDS = (
    "id",
    "norm_id",
    "resource_code",
    "name",
    "unit",
    "raw_quantity",
    "quantity",
    "quantity_kind",
    "status",
    "citation_json",
)


def discover_native(db, root):
    """Discover strictly bounded continuations for every native GESN source.

    These are literal unreviewed extraction records, never engineering findings.
    No collection/norm allowlist or source-specific geometry is used.
    """
    rows, rejected = [], []
    for source in db.execute(
        "SELECT * FROM sources WHERE kind='GESN' ORDER BY id"
    ).fetchall():
        source = dict(source)
        norm_ids = {
            row["code"]: row["id"]
            for row in db.execute(
                "SELECT id,code FROM norms WHERE source_id=?", (source["id"],)
            )
        }
        if not norm_ids:
            continue
        path = (Path(root) / source["blob_path"]).resolve()
        if (
            not path.is_relative_to(Path(root).resolve())
            or sha(path.read_bytes()) != source["sha256"]
        ):
            raise ValueError("Unverified source blob")
        existing = {
            (row["norm_id"], row["resource_code"])
            for row in db.execute(
                "SELECT r.norm_id,r.resource_code FROM resources r JOIN norms n ON n.id=r.norm_id WHERE n.source_id=?",
                (source["id"],),
            )
        }
        previous = None
        with pymupdf.open(path) as pdf:
            for page_index in range(len(pdf)):
                page_number, page = page_index + 1, pdf[page_index]
                # Avoid geometric analysis of pages that cannot supply or follow
                # a resource header. An empty intervening page drops context.
                if previous is None and not re.search(
                    r"Код\s+ресурса", str(page.get_text())
                ):
                    continue
                native = Native(page)
                tables = sorted(grids(page, native), key=lambda value: value["bbox"][1])
                if previous and tables:
                    reason = continuation_reason(
                        previous, tables[0], page, page_number, native
                    )
                    if reason is None:
                        found, omitted = extract_proposal(
                            source,
                            previous,
                            tables[0],
                            native,
                            page_number,
                            norm_ids,
                            existing,
                        )
                        rows.extend(found)
                        rejected.extend(omitted)
                    else:
                        rejected.append(
                            dict(
                                source_id=source["id"], page=page_number, reason=reason
                            )
                        )
                previous = (
                    context_for(tables[-1], page_number, page.rect.height)
                    if tables
                    else None
                )
    multiplicity = Counter((row["norm_id"], row["resource_code"]) for row in rows)
    safe = []
    for row in rows:
        if multiplicity[(row["norm_id"], row["resource_code"])] != 1:
            rejected.append(
                dict(id=row["id"], reason="ambiguous_duplicate_continuation_resource")
            )
        else:
            safe.append(row)
    return sorted(safe, key=lambda row: row["id"]), rejected


def apply_native_continuations(db, root):
    """Permanent full-build path; all admitted records stay explicitly unreviewed."""
    rows, rejected = discover_native(db, root)
    # discover_native excludes existing identities. Refuse any collision before
    # writing rather than replacing previous resource values.
    for row in rows:
        assert row["status"] == "extracted_native_geometric_row_unreviewed"
        if db.execute(
            "SELECT 1 FROM resources WHERE id=? OR (norm_id=? AND resource_code=?)",
            (row["id"], row["norm_id"], row["resource_code"]),
        ).fetchone():
            raise ValueError("Native continuation conflicts with existing resource")
    db.executemany(
        "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in RESOURCE_FIELDS) for row in rows],
    )
    proof = dict(
        version=VERSION,
        mode="native_geometric_unreviewed",
        parser_sha256=sha(Path(__file__).read_bytes()),
        added=[row["id"] for row in rows],
        rows=rows,
        rejected=rejected,
        model_calls=0,
        ocr_calls=0,
        applicability_not_inferred=True,
        edition_not_reapproved=True,
    )
    db.execute(
        "INSERT OR REPLACE INTO metadata VALUES(?,?)",
        ("resource_continuations", js(proof)),
    )
    return proof


def regenerate_reviewed(db, root, review):
    """Regenerate every admitted row from its hash-pinned, adjacent native pages.

    The reviewed document admits IDs, never replacement values. Its complete row
    (including geometry/proof) must exactly equal a newly parsed source row.
    """
    if review.get("status") != "visually_reviewed_candidate_not_published":
        raise ValueError("Expected an explicitly reviewed continuation manifest")
    rows = review.get("rows", [])
    if not rows or len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Empty or duplicated reviewed rows")
    if len({(row["norm_id"], row["resource_code"]) for row in rows}) != len(rows):
        raise ValueError("Ambiguous duplicate resource identity")
    groups = {}
    for row in rows:
        cite, proof = json.loads(row["citation_json"]), row["proof"]
        key = (cite["source_id"], proof["header_page"], cite["physical_page"])
        if key[2] != key[1] + 1:
            raise ValueError("Reviewed page pair is not adjacent")
        groups.setdefault(key, []).append(row)
    regenerated = []
    for (source_id, header_page, page_number), approved in sorted(groups.items()):
        source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise ValueError("Unknown reviewed source")
        source = dict(source)
        path = (Path(root) / source["blob_path"]).resolve()
        if not path.is_relative_to(Path(root).resolve()):
            raise ValueError("Source blob escapes the knowledge root")
        if sha(path.read_bytes()) != source["sha256"]:
            raise ValueError("Source PDF hash changed")
        if any(row["proof"]["source_sha256"] != source["sha256"] for row in approved):
            raise ValueError("Reviewed source version differs")
        norm_ids = {
            row["code"]: row["id"]
            for row in db.execute(
                "SELECT id,code FROM norms WHERE source_id=?", (source_id,)
            )
        }
        with pymupdf.open(path) as pdf:
            if not 1 <= header_page < page_number <= len(pdf):
                raise ValueError("Reviewed pages outside source PDF")
            preceding_page, page = pdf[header_page - 1], pdf[page_number - 1]
            before_native, native = Native(preceding_page), Native(page)
            before_tables = sorted(
                grids(preceding_page, before_native), key=lambda value: value["bbox"][1]
            )
            tables = sorted(grids(page, native), key=lambda value: value["bbox"][1])
            if not before_tables or not tables:
                raise ValueError("Missing physical resource grid")
            previous = context_for(
                before_tables[-1], header_page, preceding_page.rect.height
            )
            reason = continuation_reason(previous, tables[0], page, page_number, native)
            if reason is not None:
                raise ValueError("Unsafe resource continuation: " + reason)
            candidates, _rejected = extract_proposal(
                source, previous, tables[0], native, page_number, norm_ids, set()
            )
        candidates = {row["id"]: row for row in candidates}
        for row in approved:
            if candidates.get(row["id"]) != row:
                raise ValueError(
                    "Reviewed row does not exactly match native PDF: " + row["id"]
                )
            regenerated.append(candidates[row["id"]])
    return sorted(regenerated, key=lambda row: row["id"])


def apply_reviewed(db, root, review):
    """Insert exact reviewed rows in a staging DB; preserve all existing rows.

    Identical existing rows are an idempotent no-op, never overwritten. A conflicting
    ID or norm/resource pair aborts before any insert; caller controls commit.
    """
    rows = regenerate_reviewed(db, root, review)
    additions, present = [], []
    for row in rows:
        old = db.execute(
            "SELECT * FROM resources WHERE id=? OR (norm_id=? AND resource_code=?)",
            (row["id"], row["norm_id"], row["resource_code"]),
        ).fetchall()
        if old:
            expected = {key: row[key] for key in RESOURCE_FIELDS}
            if len(old) != 1 or dict(old[0]) != expected:
                raise ValueError(
                    "Existing resource conflict; no overwrite: " + row["id"]
                )
            present.append(row["id"])
        else:
            additions.append(row)
    db.executemany(
        "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?)",
        [tuple(row[key] for key in RESOURCE_FIELDS) for row in additions],
    )
    proof = dict(
        version=VERSION,
        parser_sha256=sha(Path(__file__).read_bytes()),
        reviewed_manifest_sha256=sha(js(review).encode()),
        added=[row["id"] for row in additions],
        already_present=present,
        rows=rows,
        model_calls=0,
        ocr_calls=0,
        applicability_not_inferred=True,
        edition_not_reapproved=True,
    )
    db.execute(
        "INSERT OR REPLACE INTO metadata VALUES(?,?)",
        ("resource_continuations", js(proof)),
    )
    return proof
