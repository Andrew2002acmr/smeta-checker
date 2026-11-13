"""Benchmark the versioned PDF extraction cache: cold vs warm runs.

Run: uv run python tests/benchmark_pdf_cache.py --pdf <path.pdf> --output <dir>

No LLM calls and no OCR. Two production PDFTextSource constructions against one
empty cache directory: the cold run extracts every Markdown page, the warm run
must reuse every page. Quality is byte-equality of native texts, errors and
scan flags between runs; timings are informational for the tested machine.
"""

import argparse
import hashlib
import json
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
    info: dict[str, str | None] = {"python": sys.version.split()[0]}
    try:
        import pymupdf

        info["pymupdf"] = pymupdf.VersionBind
    except ImportError:
        info["pymupdf"] = None
    try:
        from importlib.metadata import version as pkg_version

        info["pymupdf4llm"] = pkg_version("pymupdf4llm")
        info["pymupdf-layout"] = pkg_version("pymupdf-layout")
    except Exception:
        info["pymupdf4llm"] = None
        info["pymupdf-layout"] = None
    return info


def timed_source(pdf_path, cache_dir):
    import pymupdf
    from pdf_text import PDFTextSource

    started = time.monotonic()
    with pymupdf.open(pdf_path) as doc:
        source = PDFTextSource(doc, cache_dir=str(cache_dir))
        try:
            snapshot = {
                "texts": list(source.native_index.texts),
                "errors": {str(k): v for k, v in source.native_index.errors.items()},
                "pending_ocr": [i + 1 for i in source.pending_ocr],
                "extraction_hits": source.extraction_hits,
                "extraction_misses": source.extraction_misses,
            }
        finally:
            source.close()
    return snapshot, round(time.monotonic() - started, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    cache_dir = output / "cache"
    if any(cache_dir.iterdir()) if cache_dir.is_dir() else False:
        raise SystemExit("cache directory must start empty: %s" % cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    cold, cold_seconds = timed_source(args.pdf, cache_dir)
    warm, warm_seconds = timed_source(args.pdf, cache_dir)
    page_count = len(cold["texts"])
    quality = {
        "texts_equal": warm["texts"] == cold["texts"],
        "errors_equal": warm["errors"] == cold["errors"],
        "pending_equal": warm["pending_ocr"] == cold["pending_ocr"],
    }
    summary = {
        "pdf": str(args.pdf.resolve()),
        "pdf_sha256": file_sha256(args.pdf),
        "page_count": page_count,
        "versions": versions(),
        "timings": {"cold_seconds": cold_seconds, "warm_seconds": warm_seconds},
        "cold": {**cold, "texts": None},
        "warm": {**warm, "texts": None},
        "quality": quality,
        "quality_ok": all(quality.values()),
    }
    summary["cold"]["chars"] = sum(len(t) for t in cold["texts"])
    summary["warm"]["chars"] = sum(len(t) for t in warm["texts"])
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "page_count": page_count,
                "timings": summary["timings"],
                "cold": {
                    k: summary["cold"][k]
                    for k in ("extraction_hits", "extraction_misses", "chars")
                },
                "warm": {
                    k: summary["warm"][k]
                    for k in ("extraction_hits", "extraction_misses", "chars")
                },
                "quality_ok": summary["quality_ok"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    if not summary["quality_ok"]:
        print("warm run differs from cold run, failing visibly", flush=True)
        return 1
    if warm["extraction_misses"]:
        print("warm run re-extracted pages, failing visibly", flush=True)
        return 1
    if cold["extraction_hits"]:
        print(
            "cold run unexpectedly hit a non-empty cache, failing visibly", flush=True
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
