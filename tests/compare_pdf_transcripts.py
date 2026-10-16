"""Compare native flat-text extraction with the production PyMuPDF4LLM transcript.

Run: python tests/compare_pdf_transcripts.py --pdf <path.pdf> --output <dir>

No LLM calls. The old baseline (page.get_text(sort=True)) is extracted BEFORE
the production module (which imports pymupdf4llm) is loaded, so a global
layout side effect cannot contaminate the baseline. The new transcript always
comes from the production PDFTextSource (native_index); there is no direct
library fallback and no flat-text fallback. All output goes under --output;
inputs are never modified.
"""

import argparse
import hashlib
from html import escape
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))



def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def versions():
    info = {"python": sys.version.split()[0]}
    try:
        import pymupdf
        info["pymupdf"] = pymupdf.__doc__
    except ImportError:
        info["pymupdf"] = None
    try:
        import pymupdf4llm
        info["pymupdf4llm"] = getattr(pymupdf4llm, "__version__", "unknown")
    except ImportError:
        info["pymupdf4llm"] = None
    try:
        from importlib.metadata import version as pkg_version
        info["pymupdf-layout"] = pkg_version("pymupdf-layout")
    except Exception:
        info["pymupdf-layout"] = None
    return info


def html_format_counts(text):
    """Count emitted HTML elements, not the accuracy of reconstructed cells."""
    return {
        "tables": len(re.findall(r"<table\b", text, re.IGNORECASE)),
        "rows": len(re.findall(r"<tr\b", text, re.IGNORECASE)),
        "spans": len(re.findall(r"\b(?:rowspan|colspan)=", text, re.IGNORECASE)),
    }


def write_transcript(path, texts, total, heading):
    with Path(path).open("w", encoding="utf-8") as handle:
        for idx, text in enumerate(texts):
            if idx:
                handle.write("\n\n")
            handle.write(heading(idx, total))
            handle.write("\n\n")
            handle.write(text)


def write_html_transcript(path, texts):
    """Readable local preview: prose as escaped text, tables as actual HTML."""
    with Path(path).open("w", encoding="utf-8") as handle:
        handle.write('<!doctype html><html lang="ru"><meta charset="utf-8">'
                     '<title>PDF transcript — PyMuPDF4LLM</title>'
                     '<style>body{font:16px sans-serif;margin:24px}'
                     'pre{white-space:pre-wrap}table{border-collapse:collapse;margin:16px 0}'
                     'td,th{border:1px solid #888;padding:6px;vertical-align:top}'
                     'section{overflow-x:auto}</style><body>')
        for idx, text in enumerate(texts):
            handle.write(f'<section id="page-{idx + 1}"><h2>Страница {idx + 1}</h2>')
            for part in re.split(r"(<table\b.*?</table>)", text, flags=re.DOTALL | re.IGNORECASE):
                handle.write(part if part.lower().startswith("<table") else f"<pre>{escape(part)}</pre>")
            handle.write("</section>")
        handle.write("</body></html>")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.pdf.is_file():
        raise SystemExit(f"PDF not found: {args.pdf}")

    try:
        import pymupdf
    except ImportError:
        raise SystemExit("PyMuPDF is required for the baseline")

    output = args.output.resolve()
    (output / "pages" / "old").mkdir(parents=True, exist_ok=True)
    (output / "pages" / "new").mkdir(parents=True, exist_ok=True)

    # Old baseline first, before anything imports pymupdf4llm.
    started = time.monotonic()
    old_texts, old_errors = [], {}
    with pymupdf.open(args.pdf) as doc:
        page_count = len(doc)
        for idx in range(page_count):
            try:
                old_texts.append(doc[idx].get_text(sort=True).strip())
            except Exception as exc:
                old_texts.append("")
                old_errors[idx] = "baseline read failed: %s" % str(exc)[:200]
    old_seconds = round(time.monotonic() - started, 3)

    # New transcript: always the production source, no direct-library fallback.
    from pdf_text import PDFTextSource

    started = time.monotonic()
    with pymupdf.open(args.pdf) as doc:
        source = PDFTextSource(doc)
        try:
            new_texts = list(source.native_index.texts)
            new_errors = dict(source.native_index.errors)
            pending_ocr = list(source.pending_ocr)
            ocr_count = source.ocr_count
            cache_hits = source.cache_hits
        finally:
            source.close()
    new_seconds = round(time.monotonic() - started, 3)
    if len(new_texts) != len(old_texts):
        raise SystemExit(
            "page count mismatch: baseline=%d production=%d"
            % (len(old_texts), len(new_texts)))

    fmt = [html_format_counts(text) for text in new_texts]
    per_page = [
        {"page": idx + 1,
         "old_chars": len(old_texts[idx]),
         "new_chars": len(new_texts[idx]),
         "old_empty": not old_texts[idx].strip(),
         "new_empty": not new_texts[idx].strip(),
         "old_error": old_errors.get(idx, ""),
         "new_error": new_errors.get(idx, ""),
         "html_tables": fmt[idx]["tables"],
         "html_rows": fmt[idx]["rows"],
         "html_spans": fmt[idx]["spans"]}
        for idx in range(page_count)
    ]
    for idx in range(page_count):
        (output / "pages" / "old" / ("p%05d.txt" % (idx + 1))).write_text(
            old_texts[idx], encoding="utf-8")
        (output / "pages" / "new" / ("p%05d.md" % (idx + 1))).write_text(
            new_texts[idx], encoding="utf-8")
    write_transcript(output / "old.txt", old_texts, page_count,
                     lambda i, n: "===== Page %d / %d =====" % (i + 1, n))
    write_transcript(output / "new.md", new_texts, page_count,
                     lambda i, n: "# Page %d / %d" % (i + 1, n))
    write_html_transcript(output / "new.html", new_texts)

    summary = {
        "pdf": str(args.pdf.resolve()),
        "pdf_sha256": file_sha256(args.pdf),
        "page_count": page_count,
        "versions": versions(),
        "new_source": "PDFTextSource.native_index",
        "table_output": "html",
        "timings": {"old_seconds": old_seconds, "new_seconds": new_seconds},
        "totals": {
            "old_chars": sum(len(t) for t in old_texts),
            "new_chars": sum(len(t) for t in new_texts),
            "old_empty_pages": sum(1 for t in old_texts if not t.strip()),
            "new_empty_pages": sum(1 for t in new_texts if not t.strip()),
            "old_error_pages": sorted(old_errors),
            "new_error_pages": sorted(new_errors),
            # HTML element counts, not table/cell accuracy.
            "html_table_pages": sum(bool(f["tables"]) for f in fmt),
            "html_tables": sum(f["tables"] for f in fmt),
            "html_rows": sum(f["rows"] for f in fmt),
            "html_spans": sum(f["spans"] for f in fmt),
        },
        "pending_ocr": pending_ocr,
        "ocr_count": ocr_count,
        "cache_hits": cache_hits,
        "old_errors": {str(k): v for k, v in old_errors.items()},
        "new_errors": {str(k): v for k, v in new_errors.items()},
        "per_page": per_page,
        "artifacts": {"old_full": "old.txt", "new_full": "new.md", "new_preview": "new.html",
                      "old_pages": "pages/old/pNNNNN.txt",
                      "new_pages": "pages/new/pNNNNN.md"},
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output),
                      "page_count": page_count,
                      "totals": summary["totals"],
                      "timings": summary["timings"],
                      "pending_ocr": pending_ocr,
                      "old_errors": summary["old_errors"],
                      "new_errors": summary["new_errors"]},
                     ensure_ascii=False, indent=2), flush=True)
    if old_errors or new_errors:
        print("page-level extraction errors present, failing visibly",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
