"""Lossless KDFP evidence, Docling JSON and self-contained retrieval rows."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pdf_cell_ocr import column_unit
from pdf_geometry_contract import usable_box

VERSION = "kdfp-table-export-v1"


def visible_box(box, geometry):
    """Original unrotated PDF points -> displayed page, top-left PDF points."""
    if not box:
        return None
    if not usable_box({"bbox": box}):
        return None
    a, b, c, d, e, f = geometry["matrix"]
    points = [(a*x+c*y+e, b*x+d*y+f)
              for x in (box[0], box[2]) for y in (box[1], box[3])]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _intersects(left, right):
    return (min(left[2], right[2]) > max(left[0], right[0]) and
            min(left[3], right[3]) > max(left[1], right[1]))


def outside_table_text(page, record):
    """Do not re-index table OCR as unrelated prose or drop native paragraphs."""
    boxes = [t["bbox"] for t in record["tables"] if usable_box(t)]
    native, native_boxes = [], []
    for block in page.get_text("blocks"):
        if len(block) < 7 or block[6] != 0 or not block[4].strip():
            continue
        box = list(block[:4])
        if not any(_intersects(box, table_box) for table_box in boxes):
            native.append(block[4].strip())
            native_boxes.append(box)
    words, seen = [], set()
    for item in record["locators"]:
        if (item.get("origin") != "ocr" or item.get("role") not in {"word", "cell"}
                or item.get("table_id") or not usable_box(item)):
            continue
        box = item["bbox"]
        if any(_intersects(box, other) for other in [*boxes, *native_boxes]):
            continue
        key = (tuple(box), item["text"])
        if key not in seen:
            words.append(item["text"])
            seen.add(key)
    return "\n".join([*native, " ".join(words)]).strip()


def prepare_pages(path, pages, *, cache_dir):
    """Prepare original pages without re-numbering or reserializing the PDF."""
    import pymupdf
    from contextlib import closing
    from pdf_text import PDFTextSource

    path = Path(path)
    with pymupdf.open(path) as document:
        pages = tuple(pages) if pages is not None else tuple(range(1, len(document)+1))
        if not pages or len(set(pages)) != len(pages) or any(
                type(p) is not int or not 1 <= p <= len(document) for p in pages):
            raise ValueError("Pages must be unique original PDF page numbers")
        with closing(PDFTextSource(document, cache_dir=cache_dir,
                selected_pages=[number - 1 for number in pages])) as source:
            for number in pages:
                record = source.prepare_page(number-1)
                record["outside_table_text"] = outside_table_text(document[number-1], record)
                yield record


def source_identity(path):
    path = Path(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return dict(filename=path.name, sha256=digest, mimetype="application/pdf")


def evidence_document(records, source):
    return dict(version=VERSION, source=dict(source),
                coordinate_system="original unrotated PDF points, top-left origin",
                recognition_correctness_asserted=False, pages=list(records))


def table_data(table, geometry):
    """One Docling TableCell per physical cell; never expand merged slots."""
    from docling_core.types.doc import BoundingBox, CoordOrigin, TableCell, TableData

    cells = []
    for cell in table["cells"]:
        box = visible_box(cell.get("bbox"), geometry) if usable_box(cell) else None
        cells.append(TableCell(
            text=cell["text"], row_span=cell.get("rowspan", 1), col_span=cell.get("colspan", 1),
            start_row_offset_idx=cell["row"], end_row_offset_idx=cell["row"]+cell.get("rowspan", 1),
            start_col_offset_idx=cell["column"], end_col_offset_idx=cell["column"]+cell.get("colspan", 1),
            column_header=cell.get("role") == "header", row_section=cell.get("role") == "group",
            bbox=BoundingBox(l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)
            if box else None))
    return TableData(num_rows=table["row_count"], num_cols=table["col_count"], table_cells=cells)


def docling_document(records, source):
    from docling_core.types.doc import (
        BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, DocumentOrigin,
        MiscAnnotation, ProvenanceItem, Size,
    )

    document = DoclingDocument(name=Path(source["filename"]).stem,
        origin=DocumentOrigin(filename=source["filename"], mimetype="application/pdf",
                              binary_hash=source["sha256"]))
    for record in records:
        number, geometry = record["page"], record["geometry"]
        document.add_page(page_no=number, size=Size(width=geometry["width"], height=geometry["height"]))
        prose = record.get("outside_table_text", "")
        if not record["tables"]:
            prose = record["text"]
        if prose:
            document.add_text(label=DocItemLabel.TEXT, text=prose)
        for table in record["tables"]:
            box = visible_box(table.get("bbox"), geometry) if usable_box(table) else None
            provenance = ProvenanceItem(page_no=number, charspan=(0, 0), bbox=BoundingBox(
                l=box[0], t=box[1], r=box[2], b=box[3], coord_origin=CoordOrigin.TOPLEFT)) if box else None
            document.add_table(data=table_data(table, geometry), prov=provenance,
                annotations=[MiscAnnotation(content=dict(
                    version=VERSION, pdf_sha256=source["sha256"], original_page=number,
                    coordinate_system="original unrotated PDF points, top-left origin",
                    page_geometry=deepcopy(geometry), page_errors=list(record.get("errors", [])),
                    table=deepcopy(table), recognition_correctness_asserted=False))])
    return document


def external_documents(records, source):
    """Each retrieval row repeats its own full header paths and evidence status."""
    output = []
    for record in records:
        metadata = dict(source=source["filename"], pdf_sha256=source["sha256"], page=record["page"],
                        extraction_version=VERSION, recognition_correctness_asserted=False)
        prose = record.get("outside_table_text", "") if record["tables"] else record["text"]
        if prose.strip():
            output.append(dict(page_content=prose, metadata=dict(metadata, kind="page_text")))
        for table in record["tables"]:
            lookup = {cell["cell_id"]: cell for cell in table["cells"]}
            rows = defaultdict(list)
            for cell in table["cells"]:
                if cell.get("role") == "header":
                    continue
                for row in range(cell["row"], cell["row"]+cell.get("rowspan", 1)):
                    rows[row].append(cell)
            for row, cells in sorted(rows.items()):
                facts = []
                for cell in sorted(cells, key=lambda c: c["column"]):
                    fact = {key: deepcopy(cell[key]) for key in (
                        "cell_id", "text", "row", "column", "rowspan", "colspan", "bbox", "role",
                        "quality", "numeric_verified", "numeric_preparation", "recognition") if key in cell}
                    fact["context_only"] = row != cell["row"]
                    fact["header_path"] = [dict(cell_id=key, text=lookup[key]["text"],
                        bbox=lookup[key].get("bbox", [])) for key in cell.get("header_ids", []) if key in lookup]
                    fact["unresolved_header_ids"] = [key for key in cell.get("header_ids", []) if key not in lookup]
                    fact["unit"] = column_unit(table, cell) or None
                    headers = [lookup[key] for key in cell.get("header_ids", []) if key in lookup]
                    fact["requires_review"] = (
                        (cell.get("origin", table.get("origin")) == "ocr" and not cell.get("numeric_verified"))
                        or cell.get("quality") != "usable" or table.get("quality") != "usable"
                        or bool(fact["unresolved_header_ids"])
                        or any(h.get("quality") != "usable" or not usable_box(h) for h in headers))
                    facts.append(fact)
                ids = {cell["cell_id"] for cell in cells}
                checks = [check for check in table.get("consistency", {}).get("checks", [])
                          if any(c.get("cell_id") in ids for c in check.get("source_cells", []))]
                content = dict(source=metadata, table_id=table["table_id"], row=row+1,
                    complete_table=False, cells=facts, arithmetic_checks=checks,
                    warning="OCR and headers can be wrong. Consistency is not verification. "
                            "This row is not the complete table; do not infer totals or absence from retrieved rows.",
                    page_errors=list(record.get("errors", [])))
                output.append(dict(page_content=json.dumps(content, ensure_ascii=False),
                    metadata=dict(metadata, kind="table_row", table_id=table["table_id"], row=row+1)))
    return output


def save_exports(records, source, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    document = docling_document(records, source)
    values = {
        "evidence.json": evidence_document(records, source),
        "docling.json": document.model_dump(mode="json", exclude={"tables": {"__all__": {"data": {"grid"}}}}),
        "openwebui.json": external_documents(records, source),
    }
    for name, value in values.items():
        destination = output/name
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    return values
