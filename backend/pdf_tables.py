"""Canonical physical cells shared by native grids and bounded raster OCR.

No model libraries are imported by LLM workers. Raster inference runs in a
short-lived CPU subprocess; model weights are downloaded during image build.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

VERSION = "physical-cells-v12-rapidocr-primary"
RULED_OCR_VERSION = "rapidocr-ruled-v1"
RULED_REC_MODEL = "PP-OCRv5/cyrillic/mobile"


def is_primary_ruled_table(table):
    """Identify this producer even when its observation is damaged/incomplete."""
    return (isinstance(table, dict) and table.get("origin") == "ocr"
        and (table.get("recognition") or {}).get("version") == RULED_OCR_VERSION)


def _observation_box(box, enclosing=None):
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        return False
    return enclosing is None or (box[0] >= enclosing[0] - .01
        and box[1] >= enclosing[1] - .01 and box[2] <= enclosing[2] + .01
        and box[3] <= enclosing[3] + .01)


def _observation_confidence(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _reading_text(readings):
    lines = [r["text"] for r in readings if r.get("method") == "batch_line"]
    first = "\n".join(lines)
    if first.strip():
        return first
    return next((r["text"] for r in readings if r["text"].strip()), "")


def primary_observation_complete(table):
    """Validate physical observations, never confer independent verification."""
    from pdf_ruled_recovery import geometry_from_boxes

    if not is_primary_ruled_table(table):
        return False
    marker = table["recognition"]
    cells = table.get("cells")
    bounds = table.get("bbox")
    if (marker.get("state") != "complete" or marker.get("engine") != "rapidocr"
            or marker.get("rec_model") != RULED_REC_MODEL
            or not isinstance(cells, list) or not cells or not _observation_box(bounds)
            or marker.get("physical_cells", len(cells)) != len(cells)):
        return False
    logical = []
    ids = set()
    for cell in cells:
        box = cell.get("bbox")
        rec = cell.get("recognition") or {}
        if (not _observation_box(box, bounds) or cell.get("numeric_verified")
                or (cell.get("geometry_validation") or {}).get("requires_source_review")
                or rec.get("version") != RULED_OCR_VERSION):
            return False
        identity = cell.get("cell_id")
        if not identity or identity in ids:
            return False
        ids.add(identity)
        if any(type(cell.get(k)) is not int for k in ("row", "column", "rowspan", "colspan")):
            return False
        r, c, rs, cs = (cell[k] for k in ("row", "column", "rowspan", "colspan"))
        if min(r, c) < 0 or min(rs, cs) < 1:
            return False
        logical.append([r, r + rs - 1, c, c + cs - 1])
        state, text = rec.get("state"), cell.get("text")
        readings = rec.get("readings")
        if not isinstance(text, str) or not isinstance(readings, list):
            return False
        if state in {'literal_empty', 'literal_dash'}:
            proof = cell.get('literal_marker') or {}
            expected = ('', 'physical-empty-paper-v1', 'empty_paper_not_numeric_zero'
                ) if state == 'literal_empty' else (
                '–', 'physical-printed-dash-v1', 'printed_dash_not_numeric_zero')
            proof_box = proof.get('bbox')
            if (text != expected[0] or proof.get('version') != expected[1]
                    or proof.get('meaning') != expected[2] or readings
                    or rec.get('confidence') is not None
                    or not _observation_box(proof_box, box)
                    or any(abs(proof_box[i] - box[i]) > .01 for i in range(4))):
                return False
            continue
        if state not in {"recognized", "uncertain"} or not text.strip() or not readings:
            return False
        for reading in readings:
            if (not isinstance(reading, dict) or not isinstance(reading.get("text"), str)
                    or not _observation_box(reading.get("crop_bbox"), box)
                    or (reading.get("confidence") is not None
                        and not _observation_confidence(reading["confidence"]))
                    or (reading["text"].strip()
                        and not _observation_confidence(reading.get("confidence")))):
                return False
        raw = _reading_text(readings)
        if (raw != text and _normalize_unit_text(raw) != text) or not _observation_confidence(rec.get("confidence")):
            return False
        if state == "recognized":
            nonempty = [reading for reading in readings if reading["text"].strip()]
            primary = "\n".join(r["text"] for r in readings if r.get("method") == "batch_line")
            if (rec["confidence"] < .95 or any(r["confidence"] < .95 for r in nonempty)
                    or any(not r["text"].strip() for r in readings)
                    or any(r["text"] != (primary or text) for r in nonempty
                        if r.get("method") != "batch_line")):
                return False
    if (max(c["row"] + c["rowspan"] for c in cells) != table.get("row_count")
            or max(c["column"] + c["colspan"] for c in cells) != table.get("col_count")):
        return False
    # Grid closure (no overlaps/holes) is coordinate-free; logical (row,
    # column) assignment is validated separately by bbox-anchored acceptance.
    # On a rotated page, pixel sort order differs from logical order, so the
    # helper's own logical sequence cannot be compared element-wise.
    occupied = set()
    for cell in cells:
        slots = {(row, column) for row in range(cell["row"], cell["row"] + cell["rowspan"])
            for column in range(cell["column"], cell["column"] + cell["colspan"])}
        if not slots or slots & occupied:
            return False
        occupied.update(slots)
    height, width = table["row_count"], table["col_count"]
    holes = {(row, column) for row in range(height) for column in range(width)} - occupied
    exterior = {slot for slot in holes
        if slot[0] in {0, height - 1} or slot[1] in {0, width - 1}}
    pending = list(exterior)
    while pending:
        row, column = pending.pop()
        for neighbour in ((row - 1, column), (row + 1, column), (row, column - 1), (row, column + 1)):
            if neighbour in holes and neighbour not in exterior:
                exterior.add(neighbour)
                pending.append(neighbour)
    if holes != exterior:
        return False
    return geometry_from_boxes([c["bbox"] for c in cells], tolerance=.01) is not None


def recover_unit_headers(tables, words):
    """Recover literal unit labels from independent OCR inside physical headers."""
    import unicodedata
    from pdf_geometry_contract import usable_box
    for table in tables:
        for cell in table['cells']:
            if cell.get('role') != 'header' or cell.get('colspan',1) != 1 or not usable_box(cell):
                continue
            x0,y0,x1,y1=cell['bbox']
            observations=[w for w in words if w.get('conf',0)>=75 and usable_box(w)
                and x0 <= (w['bbox'][0]+w['bbox'][2])/2 <= x1
                and y0 <= (w['bbox'][1]+w['bbox'][3])/2 <= y1]
            inside=[w['text'] for w in observations]
            label=unicodedata.normalize('NFKC',''.join(inside)).casefold().replace(' ','')
            match=re.fullmatch(r'[мm]([23])',label)
            if match:
                cell['unit_observation'] = dict(version='page-unit-observation-v1',
                    engine='tesseract', unit_candidate='м'+match[1],
                    bbox=list(cell['bbox']), words=observations, verified=False)


def strip_legacy_unit_notes(tables):
    """Separate historical generated annotations from literal source text."""
    changes = 0
    for table in tables:
        for cell in table['cells']:
            if cell.get('role') != 'header':
                continue
            match = re.fullmatch(r'(.*?)\nЕдиница по Tesseract: (м[23])', cell['text'], re.S)
            if not match:
                continue
            cell.setdefault('legacy_unit_annotation', dict(text=cell['text'],
                unit_candidate=match[2], verified=False))
            cell['text'] = match[1]
            changes += 1
    return changes


def verify_numeric_cells(tables, words):
    """Confirm numerical cells only when two OCR engines agree geometrically."""
    from decimal import Decimal, InvalidOperation
    def number(text):
        text = str(text).strip().replace(' ', '').replace(',', '.')
        if len(text) > 40 or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    for table in tables:
        for cell in table['cells']:
            cell['numeric_verified'] = False
            value = number(cell['text'])
            if value is None or cell.get('quality') != 'usable' or not cell.get('header_ids'):
                continue
            rapid = cell.get('words') or []
            if not rapid or any(w.get('confidence', 0) < .9 for w in rapid):
                continue
            x0, y0, x1, y1 = cell['bbox']
            matches = []
            for word in words:
                box = word['bbox']
                if x0 <= (box[0]+box[2])/2 <= x1 and y0 <= (box[1]+box[3])/2 <= y1:
                    candidate = number(word['text'])
                    if candidate is not None and word.get('conf', 0) >= 90:
                        matches.append(candidate)
            cell['numeric_verified'] = bool(matches) and set(matches) == {value}


def finish_table(cells, *, table_id, page, bbox, origin, header_rows, reasons=()):
    """Bind full header ancestry by occupied columns, without filling data holes."""
    reasons = list(reasons)
    for number, cell in enumerate(cells):
        cell.update(
            cell_id=f"{table_id}:cell{number + 1}",
            origin=origin,
            role="header" if cell["row"] < header_rows else "cell",
        )
        cell.setdefault("quality", "usable")
    width = max((cell["column"] + cell["colspan"] for cell in cells), default=0)
    groups = []
    for cell in cells:
        only_description = cell["column"] == 0 and not any(
            other["text"]
            for other in cells
            if other["row"] == cell["row"] and other is not cell
        )
        if (
            cell["row"] >= header_rows
            and (cell["colspan"] == width or only_description)
            and cell["text"]
            and not re.fullmatch(r"[\d\s.,+-]+", cell["text"])
        ):
            cell["role"] = "group"
            groups.append(cell)
    headers = [cell for cell in cells if cell["role"] == "header" and cell["text"]]
    if not headers:
        reasons.append("header_unresolved")
    for cell in cells:
        left, right = cell["column"], cell["column"] + cell["colspan"]
        cell["header_ids"] = [
            header["cell_id"]
            for header in headers
            if header["row"] < cell["row"]
            and header["column"] <= left
            and header["column"] + header["colspan"] >= right
        ]
        preceding = [group for group in groups if group["row"] < cell["row"]]
        if preceding and cell["role"] == "cell":
            cell["header_ids"].append(
                max(preceding, key=lambda group: group["row"])["cell_id"]
            )
        if cell["role"] != "header" and not cell["header_ids"]:
            cell["quality"] = "uncertain"
    occupied = set()
    for cell in cells:
        for row in range(cell["row"], cell["row"] + cell["rowspan"]):
            for col in range(cell["column"], cell["column"] + cell["colspan"]):
                if (row, col) in occupied:
                    reasons.append("overlapping_logical_cells")
                occupied.add((row, col))
    return {
        "table_id": table_id,
        "page": page,
        "bbox": list(bbox),
        "origin": origin,
        "row_count": max((cell["row"] + cell["rowspan"] for cell in cells), default=0),
        "col_count": max(
            (cell["column"] + cell["colspan"] for cell in cells), default=0
        ),
        "header_rows": header_rows,
        "cells": cells,
        "reasons": sorted(set(reasons)),
        "quality": "uncertain"
        if reasons or any(cell["quality"] != "usable" for cell in cells)
        else "usable",
    }


def native_tables(page, read_cell):
    """Keep topology and characters in the same original PDF coordinate frame."""
    rotation = page.rotation
    try:
        if rotation:
            page.set_rotation(0)
        return _native_tables_unrotated(page, read_cell)
    finally:
        if rotation:
            page.set_rotation(rotation)


def _native_tables_unrotated(page, read_cell, *, clip=None, recover=True):
    """Use physical ruling, not colored header rectangles as extra grid edges."""
    import pymupdf

    options = {"clip": clip} if clip is not None else {}
    if isinstance(page, pymupdf.Page):
        # Page.find_tables may delegate to the optional layout engine and
        # ignore physical strategy/clip. This path must use ruled geometry.
        from pymupdf.table import find_tables
        finder = find_tables(page, strategy="lines_strict", use_layout=False, **options)
    else:
        finder = page.find_tables(strategy="lines_strict", **options)
    raw_text = page.get_text("rawdict", sort=True)
    lines = (
        [
            line
            for block in raw_text.get("blocks", [])
            for line in block.get("lines", [])
        ]
        if isinstance(raw_text, dict)
        else []
    )

    def text_in_cell(box):
        if not isinstance(raw_text, dict):
            return read_cell(page, box)
        parts = []
        for line in lines:
            chars = [
                char
                for run in line.get("spans", [])
                for char in run.get("chars", [])
                if box[0] <= (char["bbox"][0] + char["bbox"][2]) / 2 < box[2]
                and box[1] <= (char["bbox"][1] + char["bbox"][3]) / 2 < box[3]
            ]
            if chars:
                parts.append("".join(char["c"] for char in chars).strip())
        return "\n".join(part for part in parts if part)

    results = []
    for number, table in enumerate(finder.tables, 1):
        raw = list(
            dict.fromkeys(tuple(box) for row in table.rows for box in row.cells if box)
        )
        if not raw:
            continue
        xs = sorted({round(box[axis], 2) for box in raw for axis in (0, 2)})
        ys = sorted({round(box[axis], 2) for box in raw for axis in (1, 3)})
        cells = []
        for box in raw:
            x0, y0, x1, y1 = [round(value, 2) for value in box]
            row, column = ys.index(y0), xs.index(x0)
            cells.append(
                {
                    "row": row,
                    "column": column,
                    "rowspan": ys.index(y1) - row,
                    "colspan": xs.index(x1) - column,
                    "text": text_in_cell(box),
                    "bbox": list(box),
                }
            )
        cells.sort(key=lambda cell: (cell["row"], cell["column"]))
        reasons = []
        try:
            header = table.header
            external = header.external
            known_header = any(header.names)
        except AttributeError, ReferenceError:
            external, known_header = False, False
        header_rows = 0
        if known_header and not external:
            # First-band cells spanning several rows delimit the whole header.
            header_rows = max(cell["rowspan"] for cell in cells if cell["row"] == 0)
        if external:
            reasons.append("external_header_unresolved")
        bbox = getattr(table, "bbox", (xs[0], ys[0], xs[-1], ys[-1]))
        # find_tables works in unrotated page coordinates (same as get_textbox).
        bbox = list(pymupdf.Rect(bbox))
        results.append(
            finish_table(
                cells,
                table_id=f"t{number}",
                page=page.number + 1,
                bbox=bbox,
                origin="native",
                header_rows=header_rows,
                reasons=reasons,
            )
        )
    if recover:
        results = [_recover_frame_grid(page, read_cell, table) for table in results]
    return results


def _recover_frame_grid(page, read_cell, table):
    """Separate a ruled schedule from a surrounding engineering drawing frame.

    A nearly touching outer frame can produce a giant overlapping cell that
    swallows the rightmost column. Re-detect only within a contiguous band of
    physical row cells; never infer values from the requested VOR quantity.
    Accept the replacement only when its topology and headers are usable.
    """
    frame_header = "header_unresolved" in table["reasons"] and any(
        cell["row"] == 0 and cell["colspan"] >= 3 for cell in table["cells"])
    if "overlapping_logical_cells" not in table["reasons"] and not frame_header:
        return table
    from collections import defaultdict
    import pymupdf

    bands = defaultdict(list)
    for cell in table["cells"]:
        box = cell["bbox"]
        bands[(round(box[1], 1), round(box[3], 1))].append(box)
    groups = []
    for (top, bottom), boxes in sorted(bands.items()):
        if len(boxes) < 3:
            continue
        if groups and abs(groups[-1][-1][1] - top) <= 1:
            groups[-1].append((top, bottom, boxes))
        else:
            groups.append([(top, bottom, boxes)])
    groups = [group for group in groups if len(group) >= 3]
    if not groups:
        return table
    band = max(groups, key=lambda group: (len(group), group[-1][1] - group[0][0]))
    left = min(box[0] for _, _, boxes in band for box in boxes)
    region = pymupdf.Rect(left - 1, band[0][0] - 1.5,
                          table["bbox"][2], band[-1][1] + 1.5) & page.rect
    candidates = _native_tables_unrotated(page, read_cell, clip=region, recover=False)
    usable = [candidate for candidate in candidates if candidate["quality"] == "usable"
              and candidate["row_count"] >= len(band)]
    if len(usable) != 1:
        return table
    restored = usable[0]
    for word in page.get_text("words", clip=region):
        x, y = (word[0] + word[2]) / 2, (word[1] + word[3]) / 2
        if not any(cell["bbox"][0] <= x < cell["bbox"][2]
                   and cell["bbox"][1] <= y < cell["bbox"][3] for cell in restored["cells"]):
            return table  # A clipped-away quantity column is not a repaired grid.
    return finish_table(restored["cells"], table_id=table["table_id"], page=table["page"],
                        bbox=restored["bbox"], origin="native", header_rows=restored["header_rows"])


@lru_cache(maxsize=1)
def raster_engine():
    """One lazily materialized CPU OCR engine for the lifetime of a worker."""
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    ocr = RapidOCR(
        params={
            "Rec.lang_type": LangRec.CYRILLIC,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.rec_batch_num": 16,
            "Det.limit_side_len": 960,
            "Det.limit_type": "min",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )
    return ocr


def warm_raster_engine():
    """Materialize lazy Cyrillic weights while the image still builds as root."""
    ocr = raster_engine()
    ocr._load_rec_model()
    ocr._load_det_model()
    ocr._load_cls_model()


def physical_table_regions(grid, width, height):
    """Find the grid inside a drawing frame, rather than its enclosing frame."""
    import cv2
    contours, hierarchy = cv2.findContours(grid, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    children = {}
    for i, item in enumerate(hierarchy[0]):
        children.setdefault(int(item[3]), []).append(i)
    regions = []
    for index, contour in enumerate(contours):
        if len(children.get(index, [])) < 4:
            continue
        x,y,w,h = cv2.boundingRect(contour)
        if w >= width*.2 and h >= height*.08:
            regions.append((max(0,x-4),max(0,y-4),min(width,x+w+4),min(height,y+h+4)))
    return sorted(set(regions),key=lambda box:(box[1],box[0]))


def station_value(text):
    """A literal station/range is body data even when adjacent OCR is blank."""
    compact=re.sub(r'\s+','',text).upper().replace('K','К').replace('O','О')
    station=r'(?:ПК)?[\dО]+\+\d+(?:[.,]\d+)?'
    return bool(re.fullmatch(station+r'(?:[-–—]'+station+r')?',compact))


def physical_data_value(text):
    return bool(re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?',text.strip())) or station_value(text)


def physical_header_rows(cells):
    """Conservative first numeric data row; h=0.12/unit headers are not data."""
    candidates=[]
    for row in sorted({c['row'] for c in cells}):
        values = [c['text'].strip() for c in cells if c['row']==row and c['text'].strip()]
        numeric = sum(bool(re.fullmatch(r'[+-]?\d+(?:[.,]\d+)?', value)) for value in values)
        station=any(c['row']==row and c.get('column')==0 and c.get('colspan',1)==1
            and station_value(c['text']) for c in cells)
        if 0 < row <= 12 and station and numeric>=1:
            return row
        if 0 < row <= 12 and numeric >= 2 and numeric >= len(values)*.5:
            candidates.append(row)
    return candidates[0] if candidates else None


def ruled_geometry(grid):
    """Recover closed physical cells; accept only a complete rectangular tiling."""
    import cv2
    import numpy as np

    contours, hierarchy = cv2.findContours(grid, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return None
    outer = max(
        range(len(contours)), key=lambda index: cv2.contourArea(contours[index])
    )
    boxes = []
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] != outer:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w > 10 and h > 10 and w * h > 150:
            boxes.append((x, y, x + w, y + h))
    if len(boxes) < 4:
        return None

    def edges(values):
        groups = []
        for value in sorted(values):
            if groups and value - groups[-1][-1] <= 6:
                groups[-1].append(value)
            else:
                groups.append([value])
        return [sum(group) / len(group) for group in groups]

    xs = edges([box[axis] for box in boxes for axis in (0, 2)])
    ys = edges([box[axis] for box in boxes for axis in (1, 3)])
    if min(len(xs), len(ys)) < 3:
        return None
    snapped, logic, occupied = [], [], set()
    for box in sorted(boxes, key=lambda item: (item[1], item[0])):
        c0, c1 = [
            min(range(len(xs)), key=lambda i: abs(xs[i] - box[axis])) for axis in (0, 2)
        ]
        r0, r1 = [
            min(range(len(ys)), key=lambda i: abs(ys[i] - box[axis])) for axis in (1, 3)
        ]
        slots = {(row, col) for row in range(r0, r1) for col in range(c0, c1)}
        if not slots or slots & occupied:
            return None
        occupied.update(slots)
        snapped.append([xs[c0], ys[r0], xs[c1], ys[r1]])
        logic.append([r0, r1 - 1, c0, c1 - 1])
    if len(occupied) != (len(xs) - 1) * (len(ys) - 1):
        return None
    return np.asarray(snapped), np.asarray(logic)


def _recognize_crop(image, ocr, engine, geometry=None):
    """Assign OCR boxes to physical predicted cells, retaining empty cells."""
    import numpy as np

    recognized = ocr(image)
    if recognized.boxes is None:
        return []
    tables = []
    if geometry is None:
        result = engine(image)
        geometries = zip(result.cell_bboxes, result.logic_points, strict=False)
    else:
        geometries = [geometry]
    for number, (boxes, logic) in enumerate(geometries, 1):
        if len(boxes) != len(logic):
            raise ValueError("Raster table geometry/structure cell count mismatch")
        cells = []
        for box, (r0, r1, c0, c1) in zip(boxes, logic, strict=True):
            points = np.asarray(box).reshape(-1, 2)
            bbox = [
                float(points[:, 0].min()),
                float(points[:, 1].min()),
                float(points[:, 0].max()),
                float(points[:, 1].max()),
            ]
            cells.append(
                {
                    "row": int(r0),
                    "column": int(c0),
                    "rowspan": int(r1 - r0 + 1),
                    "colspan": int(c1 - c0 + 1),
                    "bbox": bbox,
                    "text": "",
                    "words": [],
                }
            )
        unassigned = []
        for box, text, confidence in zip(
            recognized.boxes, recognized.txts, recognized.scores, strict=False
        ):
            points = np.asarray(box).reshape(-1, 2)
            word = {
                "text": str(text),
                "confidence": float(confidence),
                "bbox": [
                    float(points[:, 0].min()),
                    float(points[:, 1].min()),
                    float(points[:, 0].max()),
                    float(points[:, 1].max()),
                ],
            }
            x, y = points.mean(axis=0)
            matches = [
                cell
                for cell in cells
                if cell["bbox"][0] <= x <= cell["bbox"][2]
                and cell["bbox"][1] <= y <= cell["bbox"][3]
            ]
            if not matches:
                unassigned.append(word)
                continue
            cell = min(
                matches,
                key=lambda item: (
                    (item["bbox"][2] - item["bbox"][0])
                    * (item["bbox"][3] - item["bbox"][1])
                ),
            )
            cell["words"].append(word)
            if len(matches) > 1 or confidence < (
                0.8 if re.search(r"\d", text) else 0.6
            ):
                cell["quality"] = "uncertain"
        for cell in cells:
            cell["words"].sort(key=lambda word: (word["bbox"][1], word["bbox"][0]))
            cell["text"] = "\n".join(word["text"] for word in cell["words"])
        if not cells:
            continue
        # A neural grid alone cannot establish whether leading prose is a header.
        # Keep it uncertain; the first textual row is a header candidate, not proof.
        header_rows = physical_header_rows(cells) if geometry is not None else None
        tables.append(
            finish_table(
                cells,
                table_id=f"t{number}",
                page=1,
                bbox=[
                    min(c["bbox"][0] for c in cells),
                    min(c["bbox"][1] for c in cells),
                    max(c["bbox"][2] for c in cells),
                    max(c["bbox"][3] for c in cells),
                ],
                origin="ocr",
                header_rows=header_rows or max(
                    (cell["rowspan"] for cell in cells if cell["row"] == 0), default=1
                ),
                reasons=[] if header_rows is not None else ["ocr_header_requires_review"],
            )
        )
        tables[-1]["unassigned_words"] = unassigned
    return tables


def recognize_image(image_path):
    """Locate ruled regions before inference so page titles do not become cells."""
    import cv2
    import numpy as np
    from PIL import Image

    image = np.asarray(Image.open(image_path).convert("RGB"))
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    ink = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 2
    )
    height, width = gray.shape
    horizontal = cv2.morphologyEx(
        ink,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, width // 25), 1)),
    )
    vertical = cv2.morphologyEx(
        ink,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, height // 25))),
    )
    # Rasterised thin lines leave small gaps at intersections. Close only
    # sub-cell gaps before physical tiling; never synthesize missing numbers.
    grid = cv2.morphologyEx(cv2.bitwise_or(horizontal, vertical), cv2.MORPH_CLOSE,
        np.ones((5, 5), dtype=np.uint8))
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    regions = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < width * 0.2 or h < height * 0.08:
            continue
        hcount = cv2.connectedComponents(horizontal[y : y + h, x : x + w])[0] - 1
        vcount = cv2.connectedComponents(vertical[y : y + h, x : x + w])[0] - 1
        if hcount >= 3 and vcount >= 3:
            regions.append(
                (
                    max(0, x - 4),
                    max(0, y - 4),
                    min(width, x + w + 4),
                    min(height, y + h + 4),
                )
            )
    regions = physical_table_regions(grid, width, height) or sorted(regions, key=lambda box: (box[1], box[0]))[:8]
    # Unruled tables remain candidates, never asserted as a reliable grid.
    regions = regions or [(0, 0, width, height)]
    ocr = raster_engine()
    engine = None
    tables = []
    for x0, y0, x1, y1 in regions:
        geometry = ruled_geometry(grid[y0:y1, x0:x1])
        if geometry is None and engine is None:
            from rapid_table import RapidTable
            from rapid_table.utils.typings import RapidTableInput
            engine = RapidTable(RapidTableInput(use_ocr=False))
        for table in _recognize_crop(image[y0:y1, x0:x1], ocr, engine, geometry):
            table["table_id"] = f"t{len(tables) + 1}"
            for item in [
                table,
                *table["cells"],
                *table.get("unassigned_words", []),
                *(word for cell in table["cells"] for word in cell.get("words", [])),
            ]:
                box = item["bbox"]
                item["bbox"] = [box[0] + x0, box[1] + y0, box[2] + x0, box[3] + y0]
            # Regenerate IDs after assigning the page-local table identity.
            unassigned = table.get("unassigned_words", [])
            table = finish_table(
                table["cells"],
                table_id=table["table_id"],
                page=1,
                bbox=table["bbox"],
                origin="ocr",
                header_rows=table["header_rows"],
                reasons=table["reasons"],
            )
            table["unassigned_words"] = unassigned
            tables.append(table)
    return tables


def _ruled_record(tables, words=(), errors=()):
    errors = list(dict.fromkeys(errors))
    rows = []
    for table in tables:
        pending = [cell["cell_id"] for cell in table["cells"]
            if (cell.get("recognition") or {}).get("state") not in {
                "recognized", "uncertain", "literal_empty", "literal_dash"}]
        table["recognition"]["state"] = "incomplete" if pending else "complete"
        table["quality"] = "uncertain"
        for row in range(table["row_count"]):
            text = " | ".join(cell["text"] for cell in sorted(
                (c for c in table["cells"] if c["row"] == row),
                key=lambda c: c["column"]))
            if text.strip(" |"):
                rows.append(text)
    incomplete = any(table["recognition"]["state"] == "incomplete" for table in tables)
    if incomplete:
        if "incomplete_cell_observation" not in errors:
            errors.insert(0, "incomplete_cell_observation")
    else:
        errors = [error for error in errors if error != "incomplete_cell_observation"]
    prose = "\n".join(word["text"] for word in words if word["text"].strip())
    return dict(text="\n".join(part for part in [prose, *rows] if part),
        words=list(words), tables=tables, errors=errors,
        quality=dict(state="uncertain", origin="ocr", engine="rapidocr",
            algoVersion=RULED_OCR_VERSION, algo=RULED_OCR_VERSION,
            recognition_state="incomplete" if incomplete or errors else "complete",
            reasons=list(errors), layout=dict(resolved=False, reason="word_level_outside_tables")))


_UNIT_TEXT_RE = re.compile(r'^[МмMm]\s*([23])$')


def _normalize_unit_text(text):
    """Collapse a whole-cell unit reading to its canonical superscript form.

    Only the exact two-glyph shape 'м+2/3' qualifies ('M3', 'M 3', 'м 2'
    become 'м²'/'м³'); anything else returns None and stays literal. The
    original reading is preserved in the reading's normalized_from field.
    """
    match = _UNIT_TEXT_RE.match(text)
    if not match:
        return None
    return 'м' + ('²' if match.group(1) == '2' else '³')


def _unit_glyph_text(glyphs):
    """Read one measured 'м+raised digit' pair without OCR guessing.

    Returns (text, proof): base 'м' plus raised '2'/'3' become the exact
    Unicode superscript ('м²'/'м³'). Only the measured two-component shape
    qualifies; anything else returns None and keeps normal recognition.
    """
    import cv2
    import numpy as np
    gray = np.array(glyphs.convert('L'))
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink)
    parts = sorted((list(map(int, s)) for s in stats[1:] if s[4] >= 3), key=lambda s: s[0])
    if len(parts) != 2:
        return None
    (x0, y0, w0, h0, _), (x1, y1, w1, h1, _) = parts
    # Base glyph at the bottom, exponent fully above its vertical middle and
    # strictly right of its center: the measured source unit layout.
    if not (x1 >= x0 + w0 / 2 and y1 + h1 <= y0 + h0 * .5 and y1 + h1 / 2 < y0 + h0 / 2):
        return None
    base = glyphs.crop((x0, y0, x0 + w0, y0 + h0))
    exponent = glyphs.crop((x1, y1, x1 + w1, y1 + h1))
    ocr = raster_engine()
    base_result = ocr.recognize_txt([np.asarray(base.convert('RGB'))])
    exponent_result = ocr.recognize_txt([np.asarray(exponent.convert('RGB'))])
    base_text, base_score = base_result.txts[0].strip(), float(base_result.scores[0])
    exponent_text, exponent_score = exponent_result.txts[0].strip(), float(exponent_result.scores[0])
    base_letter = 'м' if base_text.casefold() in {'м', 'm'} else None
    exponent_digit = exponent_text.strip() if exponent_text.strip() in {'2', '3'} else None
    if base_letter is None or exponent_digit is None:
        return None
    text = base_letter + ('²' if exponent_digit == '2' else '³')
    proof = dict(base_text=base_text, base_confidence=base_score,
        exponent_text=exponent_text, exponent_confidence=exponent_score,
        components=parts)
    return text, proof


def _refresh_ruled_cell(cell):
    rec = cell["recognition"]
    readings = rec["readings"]
    if rec["state"] in {"literal_empty", "literal_dash"}:
        return
    text = _reading_text(readings)
    if not text.strip():
        rec["state"] = "unreadable" if readings or rec.get("segmentation_error") else "not_attempted"
        rec["confidence"] = None
        return
    normalized = _normalize_unit_text(text)
    if normalized is not None and normalized != text:
        # User-approved: whole-cell unit readings collapse to superscript.
        # Raw OCR stays in readings; only the exported cell text changes.
        for reading in readings:
            reading.setdefault("normalized_from", reading["text"])
        text = normalized
    cell["text"] = text
    lines = [r for r in readings if r.get("method") == "batch_line"]
    selected = lines if "\n".join(r["text"] for r in lines).strip() else [
        next(r for r in readings if r["text"].strip())]
    confidence = min(r["confidence"] for r in selected if r["text"].strip())
    conflict = any(r["text"].strip() and r["text"] != text and _normalize_unit_text(r["text"]) != text
        for r in readings if r.get("method") != "batch_line")
    if text != _reading_text(readings):
        # Normalized display text replaces the raw line words; raw OCR stays
        # in readings with normalized_from for audit.
        cell["words"] = [dict(text=text, confidence=confidence,
            bbox=list(readings[0]["crop_bbox"]))]
    partial = len(lines) < rec.get("line_count", 0) or any(not r["text"].strip() for r in lines)
    rec.update(confidence=confidence, state="uncertain" if confidence < .95
        or conflict or partial or rec.get("error") or rec.get("segmentation_error") else "recognized")
    rec["conflicting_readings"] = conflict


def _rapid_words(result, *, offset=(0, 0), bounds=None):
    """Keep detector boxes, without inventing character-level coordinates."""
    import numpy as np

    if result.boxes is None:
        return []
    words = []
    for box, text, score in zip(result.boxes, result.txts or (), result.scores or (), strict=True):
        points = np.asarray(box).reshape(-1, 2)
        bbox = [float(points[:, 0].min()) + offset[0],
            float(points[:, 1].min()) + offset[1],
            float(points[:, 0].max()) + offset[0],
            float(points[:, 1].max()) + offset[1]]
        confidence = float(score)
        if not _observation_confidence(confidence) or not _observation_box(bbox, bounds):
            raise ValueError("RapidOCR returned an invalid source observation")
        if str(text).strip():
            words.append(dict(text=str(text), confidence=confidence, bbox=bbox))
    return sorted(words, key=lambda word: (word["bbox"][1], word["bbox"][0]))


def _recognize_ruled_image(image_path, *, dpi, candidate_regions=(), seconds=40.0,
        _checkpoint=None):
    """Read accepted closed cells; None means only that no grid was accepted."""
    import numpy as np
    from PIL import Image, ImageOps
    from pdf_cell_ocr import clean_glyphs
    from pdf_empty_cells import empty_paper, printed_dash
    from pdf_header_lines import line_boxes
    from pdf_ruled_recovery import closed_geometries, raster_grid, rebuild

    deadline = time.monotonic() + max(0.0, seconds)
    image = Image.open(image_path).convert("RGB")
    raster = np.asarray(image)
    width, height = image.size
    regions = list(candidate_regions)
    geometries: list[tuple[list[list[float]], list[list[int]]]] = []
    seen = set()
    if not regions:
        regions = physical_table_regions(raster_grid(raster), width, height)
    if len(regions) > 8:
        raise ValueError('geometry_region_budget:count=%d,limit=8' % len(regions))
    for region in regions:
        if not _observation_box(region, [0, 0, width, height]):
            raise ValueError('Invalid ruled candidate region')
        x0, y0 = math.floor(region[0]), math.floor(region[1])
        x1, y1 = math.ceil(region[2]), math.ceil(region[3])
        for boxes, logical in closed_geometries(raster_grid(raster[y0:y1, x0:x1])):
            boxes = [[b[0] + x0, b[1] + y0, b[2] + x0, b[3] + y0] for b in boxes]
            identity = tuple(tuple(round(v, 3) for v in b) for b in boxes)
            if identity in seen:
                continue
            seen.add(identity)
            geometries.append((boxes, logical))
    if not geometries:
        return None
    tables = []
    for boxes, logical in geometries:
        table = rebuild(boxes, logical, [], page=1)
        table["recognition"] = dict(version=RULED_OCR_VERSION, engine="rapidocr",
            rec_model=RULED_REC_MODEL, state="incomplete", physical_cells=len(boxes))
        for cell in table["cells"]:
            cell.update(quality="uncertain", recognition=dict(version=RULED_OCR_VERSION,
                state="not_attempted", confidence=None, readings=[]))
        tables.append(table)
    errors, outside = [], []

    def refresh_headers():
        for table in tables:
            first_data = physical_header_rows(table['cells'])
            header_rows = first_data if first_data is not None else 1
            refreshed = finish_table(table['cells'], table_id=table['table_id'], page=1,
                bbox=table['bbox'], origin='ocr', header_rows=header_rows,
                reasons=[] if first_data is not None else ['recovered_header_unresolved'])
            refreshed['unassigned_words'] = table.get('unassigned_words', [])
            refreshed['recognition'] = table['recognition']
            table.update(refreshed)

    def checkpoint():
        refresh_headers()
        record = _ruled_record(tables, outside, errors)
        if _checkpoint is not None:
            _checkpoint(record)
        return record

    # This checkpoint is durable before any model is loaded or called.
    checkpoint()
    jobs, crops = [], {}
    try:
        for table in tables:
            for cell in table["cells"]:
                if time.monotonic() >= deadline - .5:
                    errors.append("ruled_ocr_deadline")
                    return checkpoint()
                box = cell["bbox"]
                crop_box = [math.floor(box[0]), math.floor(box[1]),
                    math.ceil(box[2]), math.ceil(box[3])]
                crop = image.crop(crop_box)
                crops[cell["cell_id"]] = (crop, crop_box)
                proof = empty_paper(crop)
                state, text = "literal_empty", ""
                if proof is None:
                    proof = printed_dash(crop)
                    state, text = "literal_dash", "–"
                if proof is not None:
                    cell["text"] = text
                    cell["literal_marker"] = dict(proof, bbox=list(box))
                    cell["recognition"]["state"] = state
                    continue
                cleaned = clean_glyphs(crop, preserve_gray=True, return_bbox=True)
                if cleaned is None:
                    cell["recognition"]["segmentation_error"] = "no_clean_glyphs"
                    cell["recognition"]["state"] = "unreadable"
                    continue
                glyphs, clean_box = cleaned
                unit = _unit_glyph_text(glyphs)
                if unit is not None:
                    text, proof = unit
                    confidence = min(proof["base_confidence"], proof["exponent_confidence"])
                    cell["text"] = text
                    cell["recognition"].update(
                        state="recognized" if confidence >= .95 else "uncertain",
                        confidence=confidence, line_count=1, unit_glyph_proof=proof,
                        readings=[dict(method="unit_glyphs", text=text,
                            confidence=confidence, crop_bbox=list(box), proof=proof)])
                    cell["words"] = [dict(text=text, confidence=confidence, bbox=list(box))]
                    continue
                lines = line_boxes(glyphs, gap=max(1, round(dpi / 100)), threshold=250)
                if not lines or len(lines) > 12:
                    cell["recognition"]["segmentation_error"] = (
                        "line_budget_exceeded" if len(lines) > 12 else "no_segmented_lines")
                    cell["recognition"]["state"] = "unreadable"
                    continue
                cell["recognition"]["line_count"] = len(lines)
                for line in lines:
                    line_box = [crop_box[0] + clean_box[0] + line[0],
                        crop_box[1] + clean_box[1] + line[1],
                        crop_box[0] + clean_box[0] + line[2],
                        crop_box[1] + clean_box[1] + line[3]]
                    # The conservative observation cannot extend beyond its physical cell.
                    line_box = [max(box[0], line_box[0]), max(box[1], line_box[1]),
                        min(box[2], line_box[2]), min(box[3], line_box[3])]
                    if not _observation_box(line_box, box):
                        raise ValueError("Segmented line lies outside its physical cell")
                    jobs.append((cell, line_box, np.asarray(ImageOps.expand(
                        glyphs.crop(line).convert("RGB"), border=4, fill="white"))))
        checkpoint()
        if time.monotonic() >= deadline - .5:
            errors.append("ruled_ocr_deadline")
            return checkpoint()
        ocr = raster_engine()
        batches = [jobs[start:start + 16] for start in range(0, len(jobs), 16)]
        for number, batch in enumerate(batches):
            if time.monotonic() >= deadline - .5:
                errors.append("ruled_ocr_deadline")
                return checkpoint()
            result = ocr.recognize_txt([job[2] for job in batch])
            texts = result.txts if result.txts is not None else ()
            scores = result.scores if result.scores is not None else ()
            if len(texts) != len(batch) or len(scores) != len(batch):
                raise ValueError("RapidOCR batch observation count mismatch")
            for (cell, box, _), text, score in zip(batch, texts, scores, strict=True):
                confidence = float(score)
                if not _observation_confidence(confidence):
                    raise ValueError("RapidOCR returned invalid confidence")
                # Line images carry a 4px white border for the recognizer. The
                # model sometimes echoes it as leading/trailing spaces (' 1,16'
                # for '1,16'). Strip only the padding echo: interior spacing
                # ('смесь  фр.') stays literal and remains an honest mismatch.
                text = str(text).strip()
                reading = dict(method="batch_line", text=text,
                    confidence=confidence, crop_bbox=box)
                cell["recognition"]["readings"].append(reading)
                if text:
                    cell["words"].append(dict(text=text, confidence=confidence, bbox=list(box)))
                _refresh_ruled_cell(cell)
            if number in {0, len(batches) - 1}:
                checkpoint()
        rereads: list[tuple[dict, tuple]] = []
        for table in tables:
            for cell in table["cells"]:
                if cell["recognition"]["state"] in {"literal_empty", "literal_dash", "recognized"}:
                    continue
                if cell["text"].strip():
                    # A targeted re-read never rewrites a nonempty first observation:
                    # agreement keeps the state, conflict keeps the text as uncertain.
                    # Either way the outcome is already final, so skip the costly
                    # detection pass. Only empty/unreadable cells can gain text.
                    continue
                if not cell["recognition"].get("line_count"):
                    # Empty segmentation means there were no clean text rows to
                    # recognize. Detection over a sparse crop is a multi-second
                    # no-op; keep the explicit segmentation error instead.
                    reason = cell["recognition"].get("segmentation_error", "no_segmented_lines")
                    cell["recognition"].update(state="unreadable", confidence=None, segmentation_error=reason)
                    continue
                if cell["cell_id"] not in crops:
                    continue
                rereads.append((cell, crops[cell["cell_id"]]))
        final_states = {"recognized", "uncertain", "literal_empty", "literal_dash"}
        deadline_margin = 4.0 if rereads else .5
        if time.monotonic() >= deadline - deadline_margin:
            # Batched recognition already finished. Optional targeted detection
            # must not turn completed physical cells into a missed deadline.
            for cell, _ in rereads:
                if cell["recognition"]["state"] not in final_states:
                    cell["recognition"].update(state="unreadable", confidence=None,
                        segmentation_error=cell["recognition"].get("segmentation_error", "no_segmented_lines"))
            errors.append("ruled_ocr_deadline:reread_skipped")
            return checkpoint()
        # Detection is far costlier than batched recognition. Isolate it to
        # cells that can actually gain missing text; empty-only fallthroughs
        # are sub-second even when a line or two was spuriously segmented.
        for cell, (crop, crop_box) in rereads:
            if time.monotonic() >= deadline - .5:
                errors.append("ruled_ocr_deadline")
                return checkpoint()
            try:
                result = ocr(np.asarray(crop), use_det=True, use_cls=True,
                    use_rec=True, text_score=0.0)
                if getattr(result, "boxes", None) is None and cell["recognition"]["readings"]:
                    # The sparse crop contains only faint residuals: drop empty
                    # readings, keep the explicit segmentation reason, and stop.
                    cell["recognition"].update(state="unreadable", confidence=None)
                    continue
                words = _rapid_words(result, offset=crop_box[:2])
                # Detector coordinates may touch the rounded crop border; retain
                # only the physical source intersection, never a neighbouring cell.
                for word in words:
                    b, box = word["bbox"], cell["bbox"]
                    word["bbox"] = [max(b[0], box[0]), max(b[1], box[1]),
                        min(b[2], box[2]), min(b[3], box[3])]
                    if not _observation_box(word["bbox"], box):
                        raise ValueError("Targeted OCR lies outside its physical cell")
                text = "\n".join(word["text"] for word in words)
                cell["recognition"]["readings"].append(dict(method="target_cell", text=text,
                    confidence=min((w["confidence"] for w in words), default=None),
                    crop_bbox=list(cell["bbox"]), words=words))
                if not cell["text"].strip() and text.strip():
                    cell["words"] = words
                _refresh_ruled_cell(cell)
            except Exception as exc:
                reason = "ruled_cell_ocr_failed:%s" % type(exc).__name__
                cell["recognition"]["error"] = reason
                errors.append(reason)
                _refresh_ruled_cell(cell)
        if rereads:
            checkpoint()
        masked = raster.copy()
        for table in tables:
            b = table["bbox"]
            masked[math.floor(b[1]):math.ceil(b[3]), math.floor(b[0]):math.ceil(b[2])] = 255
        if np.any(masked < 250):
            if time.monotonic() >= deadline - 4.0:
                record = checkpoint()
                record["quality"]["outside_prose"] = "skipped_deadline_margin"
                return record
            result = ocr(masked, use_det=True, use_cls=True, use_rec=True, text_score=0.0)
            outside = [dict(word, conf=round(100 * word["confidence"]))
                for word in _rapid_words(result, bounds=[0, 0, width, height])]
    except Exception as exc:
        errors.append("ruled_ocr_failed:%s" % type(exc).__name__)
    refresh_headers()
    return checkpoint()


def _write_ruled_checkpoint(path, record):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def prepare_ruled_page(page, *, rotation=0, timeout=40.0, cache_dir=None, store_image=None):
    """Render once, isolate inference, and return observations in source PDF points."""
    import hashlib
    import pymupdf
    import ocr_engine
    from pdf_geometry_contract import validate_tables
    from pdf_table_checks import check_table
    started = time.monotonic()
    dpi = ocr_engine.bounded_render_dpi(page.rect.width, page.rect.height,
        dpi=300, max_pixels=24_000_000, max_side=4960)
    scale = dpi / 72
    # Small source digits merge at 247dpi ('1, 16' for '1,16'); at 300dpi the
    # same production line images read exactly at >=.99 confidence. The old
    # 12M/4096 budget silently downscaled an A3 landscape page to 247dpi.
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    base = ocr_engine._pil_from_pixmap(pix)
    image = ocr_engine._rotate(base, rotation)
    image_width, image_height = image.size
    visible = page.rect * page.derotation_matrix
    candidates = []
    for info in sorted(page.get_image_info(), key=lambda item: (item['bbox'][1], item['bbox'][0])):
        original = pymupdf.Rect(info['bbox'])
        rect = original & visible
        if rect.width < visible.width * .2 or rect.height < visible.height * .08:
            continue
        # The embedded bitmap can clip its enclosing physical frame.
        rect = pymupdf.Rect(rect.x0 - 4, rect.y0 - 4, rect.x1 + 4, rect.y1 + 4) & visible
        displayed = rect * page.rotation_matrix
        base_box = [displayed.x0 * scale, displayed.y0 * scale, displayed.x1 * scale, displayed.y1 * scale]
        if rotation % 360 == 0:
            candidate = base_box
        elif rotation % 360 == 90:
            candidate = [base_box[1], base.width - base_box[2], base_box[3], base.width - base_box[0]]
        elif rotation % 360 == 180:
            candidate = [base.width - base_box[2], base.height - base_box[3], base.width - base_box[0], base.height - base_box[1]]
        elif rotation % 360 == 270:
            candidate = [base.height - base_box[3], base_box[0], base.height - base_box[1], base_box[2]]
        else:
            raise ValueError('Orientation supports only 0/90/180/270')
        candidate = [max(0, candidate[0]), max(0, candidate[1]),
            min(image_width, candidate[2]), min(image_height, candidate[3])]
        if candidate[0] >= candidate[2] or candidate[1] >= candidate[3]:
            continue
        if all(sum(abs(a - b) for a, b in zip(candidate, old)) >= scale for old in candidates):
            candidates.append(candidate)
    if len(candidates) > 8:
        return _ruled_record([], errors=['geometry_region_budget:count=%d,limit=8' % len(candidates)])
    try:
        with TemporaryDirectory(prefix="smeta-ruled-") as directory:
            image_path = Path(directory) / "page.png"
            manifest_path = Path(directory) / "manifest.json"
            result_path = Path(directory) / "result.json"
            image.save(image_path)
            image_metadata = {}
            if store_image is not None:
                data = image_path.read_bytes()
                image_metadata["image_sha256"] = hashlib.sha256(data).hexdigest()
                image_metadata["image_key"] = store_image(data).key
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return _ruled_record([], errors=["ruled_ocr_deadline:render"])
            manifest_path.write_text(json.dumps(dict(image_path=str(image_path), dpi=dpi,
                candidate_regions=candidates, seconds=remaining)), encoding="utf-8")
            failure = None
            try:
                subprocess.run([sys.executable, "-m", "pdf_tables", "--ruled",
                    str(manifest_path), str(result_path)], check=True, capture_output=True,
                    timeout=max(.001, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                failure = "ruled_ocr_deadline:worker"
            except subprocess.CalledProcessError:
                failure = "ruled_ocr_failed:CalledProcessError"
            if result_path.exists():
                record = json.loads(result_path.read_text(encoding="utf-8"))
                if record is None and failure is None:
                    return None
            else:
                record = None
            if record is None:
                return _ruled_record([], errors=[failure or "ruled_ocr_failed:missing_checkpoint"])
            if failure:
                record = _ruled_record(record["tables"], record.get("words", []),
                    [*record.get("errors", []), failure])
    except Exception as exc:
        return _ruled_record([], errors=["ruled_ocr_failed:%s" % type(exc).__name__])

    def mapped(box):
        b = ocr_engine._unrotate_box(box, rotation, image.size)
        rect = pymupdf.Rect(pix.x + b[0], pix.y + b[1], pix.x + b[2], pix.y + b[3])
        rect = pymupdf.Rect(rect.x0 / scale, rect.y0 / scale, rect.x1 / scale, rect.y1 / scale)
        return list(rect * page.derotation_matrix)

    for table in record["tables"]:
        table["page"] = page.number + 1
        table["bbox"] = mapped(table["bbox"])
        table["recovery"].update(coordinate_system="original PDF points", dpi=dpi,
            pixmap_origin=[pix.x, pix.y], original_rotation=page.rotation, ocr_rotation=rotation)
        for cell in table["cells"]:
            cell["bbox"] = mapped(cell["bbox"])
            for word in cell.get("words", []):
                word["bbox"] = mapped(word["bbox"])
            if "literal_marker" in cell:
                cell["literal_marker"]["bbox"] = mapped(cell["literal_marker"]["bbox"])
            for reading in cell["recognition"]["readings"]:
                reading["crop_bbox"] = mapped(reading["crop_bbox"])
                for word in reading.get("words", []):
                    word["bbox"] = mapped(word["bbox"])
    for word in record["words"]:
        word["bbox"] = mapped(word["bbox"])
    geometry = validate_tables(page, record["tables"])
    for table in record["tables"]:
        pending = [cell["cell_id"] for cell in table["cells"]
            if (cell.get("recognition") or {}).get("state") not in {
                "recognized", "uncertain", "literal_empty", "literal_dash"}]
        # The worker already reports terminal observation states. Only geometry
        # rejection can reopen them here; a stale checkpoint marker cannot make
        # finished cells incomplete again.
        table["recognition"]["state"] = "incomplete" if pending else "complete"
        if pending:
            if "incomplete_cell_observation" not in record["errors"]:
                record["errors"].append("incomplete_cell_observation")
        else:
            record["errors"] = [error for error in record["errors"]
                if error != "incomplete_cell_observation"]
        table["consistency"] = check_table(table)
    record["quality"].update(rotation=rotation, page_rotation=page.rotation,
        dpi_overview=dpi, dpi_crop=dpi, geometry=geometry, **image_metadata)
    record["quality"]["reasons"] = list(record["errors"])
    record["quality"]["recognition_state"] = "incomplete" if record["errors"] else "complete"
    return record


def raster_tables(page, *, rotation=0, timeout=90):
    """Bound image memory and inference time; map all boxes to original PDF points."""
    import pymupdf
    import ocr_engine

    dpi = ocr_engine.bounded_render_dpi(page.rect.width, page.rect.height)
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    image = ocr_engine._rotate(ocr_engine._pil_from_pixmap(pix), rotation)
    with TemporaryDirectory(prefix="smeta-table-") as directory:
        image_path = Path(directory) / "page.png"
        result_path = Path(directory) / "tables.json"
        image.save(image_path)
        # A cold OCR/table model on a complex sheet can exceed the first
        # budget. Retry once on that technical failure only, reusing the same
        # rendered image. A second timeout remains an explicit page error.
        for attempt, budget in enumerate((timeout, timeout * 2)):
            try:
                subprocess.run(
                    [sys.executable, "-m", "pdf_tables", str(image_path), str(result_path)],
                    check=True,
                    capture_output=True,
                    timeout=budget,
                )
                break
            except subprocess.TimeoutExpired:
                if attempt:
                    raise
        tables = json.loads(result_path.read_text())

    def mapped(box):
        box = ocr_engine._unrotate_box(box, rotation, image.size)
        rect = (
            pymupdf.Rect([value * 72 / dpi for value in box]) * page.derotation_matrix
        )
        return list(rect)

    for table in tables:
        table["page"] = page.number + 1
        table["bbox"] = mapped(table["bbox"])
        for word in table.get("unassigned_words", []):
            word["bbox"] = mapped(word["bbox"])
        for cell in table["cells"]:
            cell["bbox"] = mapped(cell["bbox"])
            for word in cell.get("words", []):
                word["bbox"] = mapped(word["bbox"])
    # Separate embedded tables can have different column grids. Reuse the
    # words already recognised above, but restore closed physical cells before
    # independent numeric/header verification. Recovery never confirms OCR.
    from pdf_ruled_recovery import recover_page
    return recover_page(page, tables, rotation=rotation)['tables']


if __name__ == "__main__":
    if sys.argv[1:] == ["--warm"]:
        warm_raster_engine()
    elif sys.argv[1:2] == ["--ruled"]:
        manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        try:
            result = _recognize_ruled_image(**manifest,
                _checkpoint=lambda record: _write_ruled_checkpoint(sys.argv[3], record))
        except Exception as exc:
            result = _ruled_record([], errors=["ruled_ocr_failed:%s" % type(exc).__name__])
        _write_ruled_checkpoint(sys.argv[3], result)
    else:
        Path(sys.argv[2]).write_text(
            json.dumps(recognize_image(sys.argv[1]), ensure_ascii=False)
        )
