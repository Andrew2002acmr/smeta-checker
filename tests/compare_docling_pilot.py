"""Compare PyMuPDF4LLM and Docling page extraction without live LLM requests.

Run: python tests/compare_docling_pilot.py --pdf <path.pdf> --expected <path.json> --output <dir>
Optional: --vor <path.xlsx> to read positions from a real VOR workbook.

The script intentionally has no test runner assertions: it is an offline
measurement harness. Docling is an optional pilot-only dependency and must
not be added to pyproject.toml. All output goes under the chosen output
directory; inputs are never modified.
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit_core import PageTextIndex


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_expected(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("expected JSON must be a non-empty list of positions")
    normalized = []
    for idx, case in enumerate(cases):
        work = str(case.get("work", "")).strip()
        volume = str(case.get("volume", "")).strip()
        if not work or not volume:
            raise ValueError(f"position {idx + 1}: need work and volume")
        normalized.append({
            "work": work,
            "volume": volume,
            "unit": str(case.get("unit", "") or "").strip(),
            "reference": str(case.get("reference", "") or "").strip(),
            "comment": str(case.get("comment", "") or "").strip(),
            "expected_pages": [int(p) for p in case.get("expected_pages", [])],
        })
    return normalized


def load_vor_positions(path):
    """Read positions from a real VOR workbook without starting Streamlit."""
    import pandas as pd

    raw = pd.read_excel(path, header=None)
    header_idx = 0
    for idx, row in raw.iterrows():
        joined = " ".join(str(v).lower() for v in row.tolist() if str(v) != "nan")
        if "№ п.п." in joined.lower() or "наименование работ" in joined:
            header_idx = idx
            break
    frame = pd.read_excel(path, skiprows=header_idx)
    frame.columns = [str(c).strip() for c in frame.columns]

    def find_column(*markers):
        lowered = [c.lower().replace("ё", "е") for c in frame.columns]
        for marker in markers:
            for column, text in zip(frame.columns, lowered):
                if marker in text:
                    return column
        return None

    name_col = find_column("наименование работ", "наименование")
    vol_col = find_column("объем", "количество")
    unit_col = find_column("ед.", "единица измерения", "единицы измерения")
    ref_col = find_column("ссылка")
    comment_col = find_column("дополнительная", "комментарий")
    page_col = find_column("номер страниц", "номера страниц")
    missing = [label for label, col in (
        ("наименование работ", name_col),
        ("объем / количество", vol_col),
        ("единица измерения", unit_col)) if col is None]
    if missing:
        raise ValueError(f"VOR misses required columns: {', '.join(missing)}")
    positions = []
    for _, row in frame.iterrows():
        work = str(row[name_col]).strip()
        volume = str(row[vol_col]).strip()
        if not work or work.lower() == "nan" or not volume or volume.lower() == "nan":
            continue
        if str(row.iloc[0]).strip() == "1" and work == "2":
            continue
        positions.append({
            "work": work,
            "volume": volume,
            "unit": str(row[unit_col]).strip(),
            "reference": str(row[ref_col]).strip() if ref_col else "",
            "comment": str(row[comment_col]).strip() if comment_col else "",
            "expected_pages": [],
            "vor_pages": str(row[page_col]).strip() if page_col else "",
        })
    if not positions:
        raise ValueError("no usable positions found in VOR")
    return positions


def page_items(document):
    """Group Docling items by physical page without assuming item order."""
    pages = {}
    for collection in (document.texts, document.tables):
        for item in collection:
            for provenance in getattr(item, "prov", []) or []:
                page_no = getattr(provenance, "page_no", None)
                if isinstance(page_no, int) and page_no >= 1:
                    pages.setdefault(page_no, []).append(item)
    return pages


def docling_page_texts(document, page_count):
    grouped = page_items(document)
    texts, table_rows = [], []
    for page_no in range(1, page_count + 1):
        parts, rows = [], []
        for item in grouped.get(page_no, []):
            text = getattr(item, "text", "") or ""
            if text.strip():
                parts.append(text.strip())
            data = getattr(item, "data", None)
            grid = getattr(data, "grid", None) if data is not None else None
            if grid is None:
                continue
            for grid_row in grid:
                cells = []
                for cell in grid_row:
                    cell_text = getattr(cell, "text", "") or ""
                    if cell_text.strip():
                        cells.append(cell_text.strip()[:200])
                if cells:
                    rows.append(" | ".join(cells))
        texts.append("\n".join(parts))
        table_rows.append(tuple(rows))
    return texts, table_rows


def rank_metrics(index, cases):
    located, volume_pages = 0, 0
    scored = 0
    details = []
    for case in cases:
        ranked = index.candidates(case["work"], case["comment"], case["reference"], case["volume"])
        support = index.support_candidates(case["work"], case["volume"], case["unit"])
        expected = set(case["expected_pages"])
        hit = bool(expected and any(page + 1 in expected for page in ranked[:4]))
        volume_hit = bool(expected and any(page + 1 in expected for page in support[:4]))
        if expected:
            scored += 1
            located += hit
            volume_pages += volume_hit
        details.append({"work": case["work"], "volume": case["volume"],
                        "expected_pages": sorted(expected),
                        "ranked_pages": [p + 1 for p in ranked[:8]],
                        "support_pages": [p + 1 for p in support[:8]],
                        "location_hit_top4": hit, "volume_hit_top4": volume_hit})
    return {"positions": len(cases), "scored_positions": scored,
            "location_recall_top4": located / scored if scored else None,
            "volume_page_recall_top4": volume_pages / scored if scored else None,
            "details": details}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--vor", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docling-ocr", choices=("none", "auto", "tesseract"), default="auto")
    parser.add_argument("--baseline-only", action="store_true",
                        help="measure only the PyMuPDF baseline (no docling import)")
    args = parser.parse_args()
    if bool(args.expected) == bool(args.vor):
        raise SystemExit("pass exactly one of --expected or --vor")
    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    try:
        import pymupdf
    except ImportError:
        raise SystemExit("PyMuPDF is required for the native baseline")
    docling = None
    if not args.baseline_only:
        try:
            from docling.datamodel.base_models import InputFormat as _InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions as _PdfPipelineOptions
            from docling.datamodel.pipeline_options import TesseractCliOcrOptions as _TessOptions
            from docling.document_converter import DocumentConverter as _Converter
            from docling.document_converter import PdfFormatOption as _PdfFormatOption
            docling = (_InputFormat, _PdfPipelineOptions, _TessOptions, _Converter, _PdfFormatOption)
        except ImportError:
            raise SystemExit("pip install docling  # pilot-only, do NOT add to pyproject.toml")

    from pdf_text import PDFTextSource

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = load_expected(args.expected) if args.expected else load_vor_positions(args.vor)

    started = time.monotonic()
    with pymupdf.open(args.pdf) as native_doc:
        source = PDFTextSource(native_doc)
        try:
            baseline_texts = list(source.native_index.texts)
            baseline_rows = [source._table_rows_for(idx) if text.strip() else ()
                             for idx, text in enumerate(baseline_texts)]
        finally:
            source.close()
    baseline_seconds = round(time.monotonic() - started, 3)
    baseline = PageTextIndex(tuple(baseline_texts), table_rows=tuple(baseline_rows))

    page_count = len(baseline_texts)
    summary = {
        "pdf": str(args.pdf.resolve()),
        "pdf_sha256": file_sha256(args.pdf),
        "page_count": page_count,
        "docling_ocr": args.docling_ocr,
        "positions_source": str((args.expected or args.vor).resolve()),
        "baseline": {"engine": "pymupdf4llm-html-tables+find_tables", "seconds": baseline_seconds,
                     "nonempty_pages": sum(bool(t.strip()) for t in baseline_texts),
                     "table_rows": sum(len(rows) for rows in baseline_rows),
                     **rank_metrics(baseline, cases)},
    }
    if not args.baseline_only:
        InputFormat, PdfPipelineOptions, TesseractCliOcrOptions, DocumentConverter, PdfFormatOption = docling
        options = PdfPipelineOptions(do_table_structure=True, do_ocr=args.docling_ocr != "none")
        if args.docling_ocr == "tesseract":
            options.ocr_options = TesseractCliOcrOptions(lang=["rus", "eng"])
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        started = time.monotonic()
        result = converter.convert(str(args.pdf))
        docling_seconds = round(time.monotonic() - started, 3)
        document = result.document
        docling_texts, docling_rows = docling_page_texts(document, page_count)
        challenger = PageTextIndex(tuple(docling_texts), table_rows=tuple(docling_rows))
        summary["challenger"] = {"engine": "docling", "seconds": docling_seconds,
                                 "nonempty_pages": sum(bool(t.strip()) for t in docling_texts),
                                 "table_rows": sum(len(rows) for rows in docling_rows),
                                 **rank_metrics(challenger, cases)}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    public = {key: value for key, value in summary.items()}
    for side in ("baseline", "challenger"):
        if side in public:
            public[side] = {key: value for key, value in summary[side].items() if key != "details"}
    print(json.dumps(public, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
