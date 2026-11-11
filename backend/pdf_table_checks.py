"""Conservative physical-table arithmetic diagnostics, never OCR verification."""

import re
from decimal import Decimal, localcontext

from audit_measurements import _decimal
from pdf_cell_ocr import VERSION as CELL_OCR_VERSION, column_unit, literal_unit
from pdf_geometry_contract import usable_box
from pdf_header_geometry import literal

VERSION = "physical-table-checks-v2"
_NUMBER = re.compile(r"[+-]?[0-9]+(?:[.,][0-9]+)?")
_THICKNESS = re.compile(r"(?<!\w)h\s*=\s*[0-9]+(?:[.,][0-9]+)?\s*[мm](?!\w)", re.I)
_UNIT_LEAF = re.compile(r"\s*(?:(?:площадь|объем|объём|s|v)\s*[,:(]?\s*)?[мm][²³23]\s*[).]?\s*", re.I)
_MEASURE_LEAF = re.compile(r"\s*(?:площадь|объем|объём|s|v)\s*[,.:]?\s*", re.I)
_TOTAL_LEAF = re.compile(r"^\s*(?:итого|всего|итог)\b", re.I)
_LIMITATIONS = [
    "Arithmetic consistency does not verify OCR, units, or header recognition.",
    "No numeric correction, expected VOR values, adjacent-column pairing, or piket conversion is used.",
    "Totals are not checked: explicit contiguous source ranges and subtotal exclusion are not established.",
]


def _number(text):
    # Gate before the shared parser: never squeeze merged OCR tokens or repair glyphs.
    token = str(text).strip()
    if not _NUMBER.fullmatch(token):
        return None
    return _decimal(token)


def _fact(cell):
    return {key: cell[key] for key in (
        "cell_id", "row", "column", "rowspan", "colspan", "bbox", "text", "header_ids"
    ) if key in cell}


def _row_kind(table, row, by_id):
    """Interval, subtotal or grand total row: labels only, never values."""
    labels = []
    for cell in table["cells"]:
        if cell.get("row") != row or cell.get("role") not in {"cell", "group"}:
            continue
        text = str(cell.get("text", "")).strip()
        if not text or not _TOTAL_LEAF.match(text):
            continue
        labels.append(cell)
    if not labels:
        return "interval", None
    if any("по пикету" in str(cell.get("text", "")).casefold() for cell in labels):
        return "subtotal", labels
    if any(re.fullmatch(r"\s*итог\s*:?\s*", str(cell.get("text", "")), re.I) for cell in labels):
        return "grand_total", labels
    return "subtotal", labels



def _number(text):
    # Gate before the shared parser: never squeeze merged OCR tokens or repair glyphs.
    token = str(text).strip()
    if not _NUMBER.fullmatch(token):
        return None
    return _decimal(token)


def _fact(cell):
    return {key: cell[key] for key in (
        "cell_id", "row", "column", "rowspan", "colspan", "bbox", "text", "header_ids"
    ) if key in cell}


def _ancestry(table, cell, by_id):
    ids = cell.get("header_ids") or []
    if not ids or len(ids) != len(set(ids)):
        return None, "missing_or_duplicate_header_ancestry"
    headers = []
    for key in ids:
        header = by_id.get(key)
        if not header or header.get("role") not in {"header", "group"}:
            return None, "unresolved_header_ancestry"
        if not usable_box(header):
            return None, "invalid_header_geometry"
        if header.get("role") == "header" and not (
            header["row"] + header.get("rowspan", 1) <= cell["row"]
            and header["column"] <= cell["column"]
            and header["column"] + header.get("colspan", 1)
            >= cell["column"] + cell.get("colspan", 1)
        ):
            return None, "header_span_does_not_cover_cell"
        headers.append(header)
    return headers, None


def _candidate(table, cell, by_id):
    if not usable_box(cell) or not usable_box(table):
        return None, "invalid_source_geometry"
    if cell.get("rowspan", 1) != 1 or cell.get("colspan", 1) != 1:
        return None, "merged_body_cell"
    headers, reason = _ancestry(table, cell, by_id)
    if reason:
        return None, reason
    unit = column_unit(table, cell)
    if unit not in {"м2", "м3", "m2", "m3"}:
        return None, "unknown_or_unproved_literal_unit"
    value = _number(cell.get("text", ""))
    if value is None or value < 0:
        return None, "malformed_or_negative_numeric_literal"
    # column_unit has validated the proof before its exact header ID may be
    # omitted from grouping. Blank unknown headers remain scope blockers.
    proof = (table.get("column_units") or {}).get(str(cell["column"])) or {}
    from pdf_table_verification import VERSION as HEADER_VERSION, verified_column_unit
    unit_header_id = None
    if proof.get("version") == HEADER_VERSION:
        if verified_column_unit(table, cell, proof) == unit:
            unit_header_id = proof.get("header_id")
    elif (proof.get("version") == CELL_OCR_VERSION and proof.get("unit") == unit
          and len(proof.get("variants", [])) == 2
          and all(literal_unit(v.get("words", [])) == unit for v in proof["variants"])):
        unit_header_id = proof.get("header_id")
    scope = tuple(sorted(
        h["cell_id"] for h in headers
        if not (
            h.get("role") == "header" and h.get("colspan", 1) == 1
            and (
                h["cell_id"] == unit_header_id
                or _UNIT_LEAF.fullmatch(h.get("text", ""))
                or _MEASURE_LEAF.fullmatch(h.get("text", ""))
            )
        )
    ))
    if not scope:
        return None, "missing_explicit_side_layer_group"
    thicknesses = []
    for header in headers:
        for match in _THICKNESS.finditer(header.get("text", "")):
            parsed = literal(match[0])
            if parsed:
                thicknesses.append((header, parsed[0]))
    if len(thicknesses) != 1 or thicknesses[0][1] <= 0:
        return None, "missing_or_ambiguous_literal_thickness"
    return dict(cell=cell, headers=headers, unit=unit[-1], value=value,
                scope=scope, thickness_header=thicknesses[0][0], thickness=thicknesses[0][1]), None


def check_table(table: dict) -> dict:
    """Return JSON-safe source-linked diagnostics without mutating the table.

    A product is considered only within one physical row, identical complete
    non-unit ancestry, and a single shared literal h=<number>m ancestor.
    Displayed S and V are rounded readings; h is the literal design parameter.
    The product interval propagates half a displayed last place from S and V.
    """
    # Copy source facts rather than returning aliases into the caller's table.
    from copy import deepcopy

    checks = []
    cells = table.get("cells") or []
    by_id = {c.get("cell_id"): c for c in cells}
    duplicate_ids = len(by_id) != len(cells) or None in by_id
    groups = {}
    kinds = {}
    for cell in cells:
        if cell.get("role") != "cell" or not str(cell.get("text", "")).strip():
            continue
        if cell["row"] not in kinds:
            kinds[cell["row"]], _ = _row_kind(table, cell["row"], by_id)
    for cell in cells:
        if cell.get("role") != "cell" or not str(cell.get("text", "")).strip():
            continue
        kind = kinds.get(cell["row"], "interval")
        if kind != "interval":
            checks.append(dict(kind="volume_area_thickness", state="not_checkable",
                source_cells=[_fact(cell)], ocr_verified=False,
                reason="total_row_product_not_applicable_use_summation",
                row_kind=kind))
            continue
        check = dict(kind="volume_area_thickness", state="not_checkable",
                     source_cells=[_fact(cell)], ocr_verified=False)
        if duplicate_ids:
            candidate, reason = None, "missing_or_duplicate_source_ids"
        else:
            candidate, reason = _candidate(table, cell, by_id)
        if reason:
            check["reason"] = reason
            checks.append(check)
            continue
        key = (cell["row"], candidate["scope"], candidate["thickness_header"]["cell_id"])
        groups.setdefault(key, []).append(candidate)
    for candidates in groups.values():
        areas = [c for c in candidates if c["unit"] == "2"]
        volumes = [c for c in candidates if c["unit"] == "3"]
        check = dict(kind="volume_area_thickness", state="not_checkable", ocr_verified=False,
                     source_cells=[_fact(c["cell"]) for c in candidates],
                     source_headers=[_fact(h) for h in {
                         h["cell_id"]: h for c in candidates for h in c["headers"]
                     }.values()])
        if len(areas) != 1 or len(volumes) != 1:
            check["reason"] = "ambiguous_or_missing_same_scope_area_volume_pair"
        else:
            area, volume = areas[0], volumes[0]
            # Exact Decimal arithmetic at sufficient precision for source literals.
            with localcontext() as context:
                context.prec = max(28, sum(len(c["value"].as_tuple().digits) for c in candidates)
                                   + len(area["thickness"].as_tuple().digits) + 10)
                s, v, h = area["value"], volume["value"], area["thickness"]
                expected = s * h
                s_half = Decimal(1).scaleb(s.as_tuple().exponent) / 2
                v_half = Decimal(1).scaleb(v.as_tuple().exponent) / 2
                tolerance = s_half * h + v_half
                delta = v - expected
                check.update(state="consistent" if abs(delta) <= tolerance else "conflict",
                             reason="displayed_precision_intervals_overlap" if abs(delta) <= tolerance
                             else "volume_outside_displayed_precision_interval",
                             formula="V = S * h", operands={"S": str(s), "V": str(v), "h_m": str(h)},
                             calculated_volume=str(expected), difference=str(delta),
                             roundoff_tolerance=str(tolerance),
                             thickness_header_id=area["thickness_header"]["cell_id"])
        checks.append(check)
    checks.append(dict(kind="totals", state="not_checkable",
                       reason="explicit_contiguous_ranges_and_subtotal_exclusion_not_established",
                       source_cell_ids=[c.get("cell_id") for c in cells
                                        if re.search(r"\b(?:итого|всего|итог)\b", c.get("text", ""), re.I)],
                       ocr_verified=False))
    states = {c["state"] for c in checks if c["kind"] == "volume_area_thickness"}
    state = "conflict" if "conflict" in states else "consistent" if "consistent" in states else "not_checkable"
    return deepcopy(dict(version=VERSION, state=state, checks=checks,
                         limitations=list(_LIMITATIONS), ocr_verified=False))
