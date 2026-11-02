"""Canonical physical cells shared by native grids and bounded raster OCR.

No model libraries are imported by LLM workers. Raster inference runs in a
short-lived CPU subprocess; model weights are downloaded during image build.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

VERSION = "physical-cells-v3-frame-recovery"


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


def raster_engine():
    from rapid_table import RapidTable
    from rapid_table.utils.typings import RapidTableInput
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    ocr = RapidOCR(
        params={
            "Rec.lang_type": LangRec.CYRILLIC,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )
    table = RapidTable(RapidTableInput(use_ocr=False))
    return ocr, table


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
        geometries = zip(result.cell_bboxes, result.logic_points)
    else:
        geometries = [geometry]
    for number, (boxes, logic) in enumerate(geometries, 1):
        if len(boxes) != len(logic):
            raise ValueError("Raster table geometry/structure cell count mismatch")
        cells = []
        for box, (r0, r1, c0, c1) in zip(boxes, logic):
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
            recognized.boxes, recognized.txts, recognized.scores
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
                header_rows=max(
                    (cell["rowspan"] for cell in cells if cell["row"] == 0), default=1
                ),
                reasons=["ocr_header_requires_review"],
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
    grid = cv2.bitwise_or(horizontal, vertical)
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
    regions = sorted(regions, key=lambda box: (box[1], box[0]))[:8]
    # Unruled tables remain candidates, never asserted as a reliable grid.
    regions = regions or [(0, 0, width, height)]
    ocr, engine = raster_engine()
    tables = []
    for x0, y0, x1, y1 in regions:
        geometry = ruled_geometry(grid[y0:y1, x0:x1])
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
        subprocess.run(
            [sys.executable, "-m", "pdf_tables", str(image_path), str(result_path)],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
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
    return tables


if __name__ == "__main__":
    if sys.argv[1:] == ["--warm"]:
        raster_engine()
    else:
        Path(sys.argv[2]).write_text(
            json.dumps(recognize_image(sys.argv[1]), ensure_ascii=False)
        )
