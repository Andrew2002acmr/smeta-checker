"""Structured PDF Markdown indexing and bounded OCR of related scans."""

from __future__ import annotations

from billiard.exceptions import SoftTimeLimitExceeded

import hashlib
import json
import os
import re
import pymupdf
import pymupdf4llm
import threading
import shutil
from importlib.metadata import version as _pkg_version
from pathlib import Path

from audit_core import PageTextIndex
from audit_tracing import span
import ocr_engine as _ocr_engine

# Layout's native GNN analysis can stall on engineering drawings. Use the
# deterministic Markdown/table extractor; OCR remains explicitly bounded below.
pymupdf4llm.use_layout(False)

# Short text with raster content needs OCR; short native-only pages do not.
NATIVE_MIN_CHARS = 150
# Locally ranked candidates (plus neighbours) eligible for OCR per position.
TOP_CANDIDATES_PER_POSITION = 5
# Structurally scanned pages per position, bounded like OCR allowances.
MAX_TABLE_PAGES_PER_POSITION = 8
MAX_TABLE_CELL_CHARS = 200
MAX_VECTOR_PATHS = 5000
# Pages per to_markdown call: one pipeline init per batch, not per page.
_MARKDOWN_BATCH = 8
# Grid cache v3 retains canonical cells, spans and headers alongside row views.
# OCR TSV envelopes carry their own format/schema and settings key, so the
# string-only legacy OCR records are never reused.
CACHE_SCHEMA_VERSION = 3
# PyMuPDF4LLM table rendering; part of the Markdown cache key.
TABLE_OUTPUT = "html"
_MARKDOWN_KWARGS = {
    "page_chunks": True,
    "table_output": TABLE_OUTPUT,
    "use_ocr": False,
    "write_images": False,
    "embed_images": False,
    "show_progress": False,
    # Otherwise every single-page call rescans the entire document for font
    # sizes: 303 pages become 303 full-document scans.
    "hdr_info": False,
    # Ignore vector graphics past the complexity budget: borderline pages
    # (2-5K paths) extract ~2x faster with identical text; true monsters
    # are still diverted by _too_complex before reaching markdown.
    "graphics_limit": MAX_VECTOR_PATHS,
}


def _compact_text(text):
    """Drop layout padding before character budgets, preserving source line order."""
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


# Lines repeated on most pages (running headers/footers) inflate native
# length without content. They are excluded from scan detection and search.
_REPEATED_LINE_MIN_CHARS = 8
_REPEATED_LINE_MIN_SHARE = 0.5


def _repeated_lines(texts):
    """Normalized lines repeated on most pages, e.g. running headers."""
    counts = {}
    for text in texts:
        seen = set()
        for line in str(text or "").splitlines():
            cleaned = re.sub(r"\s+", " ", line).strip()
            if len(cleaned) >= _REPEATED_LINE_MIN_CHARS:
                seen.add(cleaned)
        for cleaned in seen:
            counts[cleaned] = counts.get(cleaned, 0) + 1
    threshold = max(3, int(len(texts) * _REPEATED_LINE_MIN_SHARE))
    return {line for line, count in counts.items() if count >= threshold}


def _strip_repeated_lines(text, repeated):
    """Remove running header/footer lines, preserving source line order."""
    if not repeated:
        return str(text or "")
    kept = []
    for line in str(text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned in repeated:
            continue
        kept.append(line)
    return "\n".join(kept)


# Some CAD exports (ISOCPEUR GOST fonts) map Cyrillic glyphs onto Latin
# Extended codepoints (broken ToUnicode CMap). The shift is constant per
# font face, verified by render+OCR calibration: U+01D3 -> К, etc.
# Control chars in the same spans are CMap holes: dropped, never guessed.
_CMAP_SHIFTS = {"ISOCPEURItalic": 583, "ISOCPEUR": 581}


def _fix_cmap_shifts(page, text):
    """Repair CMap-shifted Cyrillic using span fonts; order-preserving.

    Only words containing Latin Extended chars are touched; everything
    else passes through byte-identical. Returns (fixed_text, fixed_count).
    """
    if not any(0x100 <= ord(c) <= 0x1FF for c in text):
        return text, 0
    fixes = {}
    try:
        blocks = page.get_text("dict", sort=True)["blocks"]
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return text, 0
    for block in blocks:
        for line in block.get("lines", []):
            for run in line.get("spans", []):
                shift = _CMAP_SHIFTS.get(run.get("font", ""))
                if not shift:
                    continue
                for word in run.get("text", "").split():
                    if not any(0x100 <= ord(c) <= 0x1FF for c in word):
                        continue
                    fixed = "".join(
                        chr(ord(c) + shift) if 0x100 <= ord(c) <= 0x1FF else c
                        for c in word
                    )
                    fixed = "".join(c for c in fixed if ord(c) >= 32)
                    if (
                        fixed
                        and fixed != word
                        and all(not (0x100 <= ord(c) <= 0x1FF) for c in fixed)
                    ):
                        fixes[word] = fixed
    count = 0
    for broken, fixed in sorted(fixes.items(), key=lambda kv: -len(kv[0])):
        if broken in text:
            text = text.replace(broken, fixed)
            count += 1
    return text, count


def _deglue_numbers(page, text):
    """Split digit-runs glued by sort() from adjacent words.

    On drawings, labels from both sides of a line land edge-to-edge
    (x1 == x0) and sort() concatenates them: '137,12' + '4+01,65'.
    A space is inserted only when both neighbours carry digits and the
    glued form is present verbatim. Returns (fixed_text, fixed_count).
    """
    try:
        words = page.get_text("words", sort=True)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        return text, 0
    count = 0
    for first, second in zip(words, words[1:]):
        a, b = first[4], second[4]
        if not a or not b:
            continue
        if not (any(c.isdigit() for c in a) and any(c.isdigit() for c in b)):
            continue
        gap = second[0] - first[2]
        vertical = abs((first[1] + first[3]) / 2 - (second[1] + second[3]) / 2)
        height = max(first[3] - first[1], second[3] - second[1], 1)
        if gap > height * 0.3 or vertical > height:
            continue
        glued = a + b
        if glued in text:
            text = text.replace(glued, a + " " + b)
            count += 1
    return text, count


def _ru_decimal_point(text):
    """Normalize OCR decimal points to Russian commas inside numbers.

    Engines emit '113.36' while the workbook and remarks use '113,36';
    string matching then misses. Dates (dd.mm.yyyy) keep their dots.
    """
    parts = re.split(r"(\b\d{2}\.\d{2}\.\d{4}\b)", text)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"(?<![A-Za-zА-Яа-яЁё]\d)\.(?=\d)", ",", parts[i])
    return "".join(parts)


def _engine_fingerprint(language="rus+eng"):
    """Fingerprint active OCR language contents, not installation timestamps."""
    engine = pymupdf.VersionBind
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if not tessdata_dir and shutil.which("tesseract"):
        try:
            tessdata_dir = pymupdf.get_tessdata()
        except RuntimeError:
            tessdata_dir = ""
    entries = []
    if tessdata_dir:
        for name in sorted(set(language.split("+"))):
            path = Path(tessdata_dir) / f"{name}.traineddata"
            if path.is_file():
                with path.open("rb") as data:
                    digest = hashlib.file_digest(data, "sha256").hexdigest()
                entries.append((path.name, path.stat().st_size, digest))
            else:
                entries.append((path.name, None, None))
    return engine, repr((tessdata_dir, entries))


def _parser_fingerprint():
    """Extractor versions reflected in the Markdown/table cache key."""
    try:
        layout = _pkg_version("pymupdf-layout")
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        layout = "unknown"
    try:
        extractor = _pkg_version("pymupdf4llm")
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        extractor = getattr(pymupdf4llm, "__version__", "unknown")
    return (
        str(getattr(pymupdf, "VersionBind", "unknown")),
        str(extractor),
        str(layout),
    )


def _cache_payload(payload):
    """Canonical cache envelope serialization: stable key across runs."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def bounded_page_dpi(page, dpi=200, max_pixels=12000000, max_side=4096):
    """Bound OCR and inspector rasters before allocating a pixmap."""
    return _ocr_engine.bounded_render_dpi(
        float(page.rect.width), float(page.rect.height), dpi, max_pixels, max_side
    )


class PDFTextSource:
    """PyMuPDF4LLM Markdown index with a versioned persistent extraction cache."""

    def __init__(
        self,
        doc,
        cache_dir=".audit_cache/pdf",
        language="rus+eng",
        dpi=200,
        max_pixels=12000000,
        max_side=4096,
        progress=None,
        checkpoint=None,
    ):
        self._progress = progress
        self._checkpoint = checkpoint
        self._owner_thread = threading.get_ident()
        if min(dpi, max_pixels, max_side) <= 0:
            raise ValueError("Параметры OCR должны быть положительными")
        if getattr(doc, "is_closed", False):
            raise ValueError(
                "PDF-документ закрыт: переоткройте файл и повторите подготовку позиции"
            )
        self._pdf_bytes = b""
        self._tmp_path = None
        if getattr(doc, "name", "") and not getattr(doc, "is_dirty", False):
            self._doc = pymupdf.open(doc.name)
        else:
            # Nameless (stream) docs: spill to a temp file once instead of
            # holding tobytes() copies in RAM (78MB PDF -> 234MB peak).
            import tempfile

            Path(cache_dir).parent.mkdir(parents=True, exist_ok=True)
            fd, self._tmp_path = tempfile.mkstemp(
                prefix="smeta-pdf-", suffix=".pdf", dir=str(Path(cache_dir).parent)
            )
            try:
                with open(fd, "wb") as tmp:
                    tmp.write(doc.tobytes(no_new_id=True))
            except BaseException:
                Path(self._tmp_path).unlink(missing_ok=True)
                self._tmp_path = None
                raise
            self._doc = pymupdf.open(self._tmp_path)
        try:
            self._pdf_hash = self._pdf_sha256(self._doc)
        except BaseException:
            self.close()
            raise
        self._cache_dir = Path(cache_dir)
        self.language = language
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.max_side = max_side
        self.ocr_count = 0
        self.cache_hits = 0
        self.extraction_hits = 0
        self.extraction_misses = 0
        self.table_cache_hits = 0
        self._ocr_texts = {}
        self._ocr_errors = {}
        # Per-page OCR evidence: {idx: {"text": str, "words": [...], "quality": {...}}}.
        # ``quality`` carries pending/usable/uncertain/failed with reasons; it is
        # the only per-page OCR state that enters snapshots/hashes.
        self._ocr_pages = {}
        self._table_rows = {}
        self._structured_tables = {}
        self._table_errors = {}
        self._table_origins = {}
        self._extraction_key = self._extraction_key_for(TABLE_OUTPUT)
        self._settings_key = self._settings_key_for(language, dpi, max_pixels, max_side)
        self._table_key = self._table_key_for(MAX_TABLE_CELL_CHARS)
        try:
            (self._native_texts, self._native_errors, pending, self._repeated_lines) = (
                self._read_native()
            )
        except BaseException:
            self.close()
            raise
        self._pending = set(pending)
        self.native_index = PageTextIndex(
            self._native_texts,
            self._native_errors,
            pending,
            repeated_lines=self._repeated_lines,
        )

    @property
    def page_count(self):
        return len(self._native_texts)

    @property
    def pending_ocr(self):
        return tuple(sorted(self._pending))

    def close(self):
        """Закрыть приватный документ; общий документ пула не трогает."""
        try:
            if self._doc is not None and not getattr(self._doc, "is_closed", False):
                self._doc.close()
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            pass
        tmp = getattr(self, "_tmp_path", None)
        if tmp:
            try:
                Path(tmp).unlink(missing_ok=True)
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                pass
            self._tmp_path = None

    def __del__(self):
        # Tests and error paths may skip close(): never leak the spill file.
        try:
            self.close()
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            pass

    def _require_owner_thread(self, what):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "PDFTextSource: %s доступно только в потоке подготовки индекса; "
                "в рабочие потоки передавайте только готовые снимки "
                "PageTextIndex и строки." % what
            )

    @staticmethod
    def _has_vector_drawings(page):
        # Outlined (curve-converted) text has no images and almost no
        # extractable text, but hundreds of vector paths. A handful of paths
        # is just borders/rules, so require a bulk of drawings before
        # treating the page as a scan candidate.
        drawings_fn = getattr(page, "get_drawings", None)
        if not callable(drawings_fn):
            return False
        try:
            return len(drawings_fn()) >= 50
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return False

    @staticmethod
    def _too_complex(page):
        drawings = getattr(page, "get_cdrawings", None)
        return callable(drawings) and len(drawings()) > MAX_VECTOR_PATHS

    def _read_native(self):
        texts, errors, scans = [], {}, []
        doc = self._doc
        # PyMuPDF4LLM removes page rotation in-place. Keep the source used by
        # the inspector, OCR and document hash unchanged.
        if doc.name and not doc.is_dirty:
            markdown_doc = pymupdf.open(doc.name)
        else:
            markdown_doc = pymupdf.open(
                stream=doc.tobytes(no_new_id=True), filetype="pdf"
            )
        with markdown_doc:
            # Pass 1 (cheap): cache hits, native text, scan/complex flags.
            natives: dict[int, str] = {}
            pending: list[int] = []
            # Running headers are detected after pass 1 collects natives;
            # scan eligibility is decided on content without them.
            deferred: list[int] = []
            for idx in range(len(doc)):
                cached = self._read_cached_extraction(idx)
                if cached is not None:
                    text, is_scan = cached
                    texts.append(text)
                    if is_scan:
                        scans.append(idx)
                    self.extraction_hits += 1
                    if self._progress:
                        self._progress(idx + 1, len(doc))
                    continue
                texts.append("")  # placeholder, filled in pass 2
                try:
                    page = markdown_doc[idx]
                    raw = page.get_text(sort=True)
                    raw, _ = _fix_cmap_shifts(page, raw)
                    raw, _ = _deglue_numbers(page, raw)
                    natives[idx] = _compact_text(raw)
                    deferred.append(idx)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    errors[idx] = "Сбой чтения PDF (PyMuPDF4LLM): %s" % str(exc)[:200]
                    if self._progress:
                        self._progress(idx + 1, len(doc))
            repeated = _repeated_lines(
                [natives[idx] for idx in deferred]
                + [texts[idx] for idx in range(len(doc)) if texts[idx]]
            )
            for idx in deferred:
                try:
                    page = markdown_doc[idx]
                    if self._too_complex(page):
                        texts[idx] = natives[idx]
                        errors[idx] = (
                            "Сложный векторный чертёж: анализ разметки ограничен; требуется ручная проверка страницы"
                        )
                        if self._progress:
                            self._progress(idx + 1, len(doc))
                        continue
                    # Vector drawings are expensive to enumerate: only scan
                    # candidates (short content + images) pay for the check.
                    content = _strip_repeated_lines(natives[idx], repeated)
                    short = len(_compact_text(content)) < NATIVE_MIN_CHARS
                    images = page.get_images() if short else []
                    is_scan = short and bool(
                        images
                        or (
                            PDFTextSource._has_vector_drawings(page)
                            if not images
                            else False
                        )
                    )
                    if is_scan:
                        # to_markdown with use_ocr=False returns '' on scans
                        # after ~2s of work; OCR lands separately via
                        # for_position. Keep the short digital text, if any.
                        scans.append(idx)
                        texts[idx] = natives[idx]
                        self._write_cached_extraction(idx, natives[idx], True)
                        if self._progress:
                            self._progress(idx + 1, len(doc))
                        continue
                    pending.append(idx)
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    errors[idx] = "Сбой чтения PDF (PyMuPDF4LLM): %s" % str(exc)[:200]
                    if self._progress:
                        self._progress(idx + 1, len(doc))
            # Pass 2 (expensive): one pipeline init per batch, not per page.
            for start in range(0, len(pending), _MARKDOWN_BATCH):
                batch = pending[start : start + _MARKDOWN_BATCH]
                self.extraction_misses += len(batch)
                try:
                    chunks = pymupdf4llm.to_markdown(
                        markdown_doc, pages=batch, **_MARKDOWN_KWARGS
                    )
                    by_page = dict(zip(batch, chunks))
                except SoftTimeLimitExceeded:
                    raise
                except Exception:
                    # One rotten page must not kill its batch neighbours:
                    # retry the batch page by page, isolating the failure.
                    by_page = {}
                    for idx in batch:
                        try:
                            single = pymupdf4llm.to_markdown(
                                markdown_doc, pages=[idx], **_MARKDOWN_KWARGS
                            )
                            by_page[idx] = single[0] if single else None
                        except SoftTimeLimitExceeded:
                            raise
                        except Exception as exc:
                            texts[idx] = ""
                            errors[idx] = (
                                "Сбой чтения PDF (PyMuPDF4LLM): %s" % str(exc)[:200]
                            )
                            if self._progress:
                                self._progress(idx + 1, len(doc))
                    for idx in batch:
                        if idx in errors:
                            continue
                        chunk = by_page.get(idx)
                        text = (
                            _compact_text(chunk["text"])
                            if chunk
                            else natives.get(idx, "")
                        )
                        texts[idx] = text
                        self._write_cached_extraction(idx, text, idx in scans)
                        if self._progress:
                            self._progress(idx + 1, len(doc))
                    continue
                # Chunks come back in pages order; metadata.page is empty.
                by_page = dict(zip(batch, chunks))
                for idx in batch:
                    chunk = by_page.get(idx)
                    text = (
                        _compact_text(chunk["text"]) if chunk else natives.get(idx, "")
                    )
                    texts[idx] = text
                    self._write_cached_extraction(idx, text, idx in scans)
                    if self._progress:
                        self._progress(idx + 1, len(doc))
        repeated_native = _repeated_lines(list(texts))
        return tuple(texts), errors, scans, repeated_native

    @staticmethod
    def _pdf_sha256(doc):
        # Hash original bytes when available: serialization can change PDF IDs.
        if doc.name and not doc.is_dirty:
            with Path(doc.name).open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()
        return hashlib.sha256(doc.tobytes(no_new_id=True)).hexdigest()

    @staticmethod
    def _extraction_key_for(table_output):
        pymupdf_version, extractor_version, layout_version = _parser_fingerprint()
        raw = _cache_payload(
            {
                "schema": CACHE_SCHEMA_VERSION,
                "engine": "rag-no-layout-v2",
                "max_vector_paths": MAX_VECTOR_PATHS,
                "pymupdf": pymupdf_version,
                "pymupdf4llm": extractor_version,
                "pymupdf_layout": layout_version,
                "table_output": table_output,
                "markdown": _MARKDOWN_KWARGS,
                "native_min_chars": NATIVE_MIN_CHARS,
                "repeated_lines": {
                    "min_chars": _REPEATED_LINE_MIN_CHARS,
                    "min_share": _REPEATED_LINE_MIN_SHARE,
                },
            }
        )
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _settings_key_for(language, dpi, max_pixels, max_side):
        identity = {
            "format": _ocr_engine.CACHE_FORMAT,
            "schema": _ocr_engine.CACHE_SCHEMA,
            "algo": _ocr_engine.ALGO_VERSION,
            "language": language,
            "dpi": dpi,
            "max_pixels": max_pixels,
            "max_side": max_side,
            "psm_overview": _ocr_engine.OVERVIEW_PSM,
            "psm_crop": _ocr_engine.CROP_PSM,
            "orientations": list(_ocr_engine.ORIENTATIONS),
            "quadrant_dpi": _ocr_engine.QUADRANT_DPI,
            "quadrant_overlap": _ocr_engine.QUADRANT_OVERLAP,
            "label_min_conf": _ocr_engine.LABEL_MIN_CONF,
            "numeric_min_conf": _ocr_engine.NUMERIC_MIN_CONF,
            "max_tsv_passes": _ocr_engine.MAX_TSV_PASSES,
            "pass_timeout_s": _ocr_engine.PASS_TIMEOUT_S,
            "tesseract": _ocr_engine.tesseract_version(),
            "engine": _engine_fingerprint(language),
        }
        return hashlib.sha256(_cache_payload(identity)).hexdigest()[:16]

    @staticmethod
    def _table_key_for(max_cell_chars):
        engine = pymupdf.VersionBind
        from pdf_tables import VERSION

        raw = _cache_payload(
            {
                "schema": CACHE_SCHEMA_VERSION,
                "pymupdf": str(engine),
                "max_cell_chars": max_cell_chars,
                "physical_cells": VERSION,
                "rapidocr": _pkg_version("rapidocr"),
                "rapid_table": _pkg_version("rapid-table"),
                "opencv": _pkg_version("opencv-python"),
            }
        )
        return hashlib.sha256(raw).hexdigest()[:16]

    def _extraction_path(self, idx):
        return (
            self._cache_dir
            / self._pdf_hash
            / self._extraction_key
            / ("page-%05d.json" % idx)
        )

    def _table_path(self, idx):
        return (
            self._cache_dir
            / self._pdf_hash
            / ("tables-%s" % self._table_key)
            / ("page-%05d.json" % idx)
        )

    def _cache_path(self, idx):
        return (
            self._cache_dir
            / self._pdf_hash
            / ("ocr-%s" % self._settings_key)
            / ("page-%05d.json" % idx)
        )

    def _read_cached_extraction(self, idx):
        """Read one reusable Markdown page; failures mean recompute, never reuse."""
        try:
            raw = self._extraction_path(idx).read_bytes()
        except OSError:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("schema") != CACHE_SCHEMA_VERSION:
            return None
        if envelope.get("page") != idx:
            return None
        if envelope.get("pdf_sha256") != self._pdf_hash:
            return None
        if envelope.get("extraction_key") != self._extraction_key:
            return None
        text = envelope.get("markdown")
        is_scan = envelope.get("is_scan")
        if not isinstance(text, str) or not isinstance(is_scan, bool):
            return None
        return _compact_text(text), is_scan

    def _write_cached_extraction(self, idx, text, is_scan):
        """Persist one Markdown page; failures are ignored and never poison reads."""
        envelope = {
            "schema": CACHE_SCHEMA_VERSION,
            "page": idx,
            "pdf_sha256": self._pdf_hash,
            "extraction_key": self._extraction_key,
            "markdown": text,
            "is_scan": bool(is_scan),
        }
        self._write_cache_bytes(self._extraction_path(idx), _cache_payload(envelope))

    def _write_cache_bytes(self, path, payload):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(
                "%s.tmp-%d-%d" % (path.name, os.getpid(), threading.get_ident())
            )
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return
        # Yield only after durable progress, never after a failed OCR/cache
        # write which would repeat forever at the next preparation slice.
        if self._checkpoint:
            self._checkpoint()

    def _ocr_settings_identity(self):
        """Full OCR identity for caching: engine, settings, geometry knobs."""
        return {
            "format": _ocr_engine.CACHE_FORMAT,
            "schema": _ocr_engine.CACHE_SCHEMA,
            "algo": _ocr_engine.ALGO_VERSION,
            "language": self.language,
            "dpi": self.dpi,
            "max_pixels": self.max_pixels,
            "max_side": self.max_side,
            "psm_overview": _ocr_engine.OVERVIEW_PSM,
            "psm_crop": _ocr_engine.CROP_PSM,
            "orientations": list(_ocr_engine.ORIENTATIONS),
            "quadrant_dpi": _ocr_engine.QUADRANT_DPI,
            "quadrant_overlap": _ocr_engine.QUADRANT_OVERLAP,
            "label_min_conf": _ocr_engine.LABEL_MIN_CONF,
            "numeric_min_conf": _ocr_engine.NUMERIC_MIN_CONF,
            "max_tsv_passes": _ocr_engine.MAX_TSV_PASSES,
            "pass_timeout_s": _ocr_engine.PASS_TIMEOUT_S,
            "tesseract": _ocr_engine.tesseract_version(),
            "engine": _engine_fingerprint(self.language),
        }

    def _ocr_page_result(self, idx):
        """Bounded Tesseract TSV recognition of one scan page.

        Direct PNG-bytes ``tesseract`` TSV passes (orientation scoring +
        quadrant crops); no legacy string-only fallback. Raises only when no
        recognition could run at all; unreadable passes yield explicit
        quality reasons through the returned record instead.
        """
        self._require_owner_thread("OCR сканов PDF")
        result = _ocr_engine.recognize_page(
            self._doc[idx],
            language=self.language,
            dpi=self.dpi,
            max_pixels=self.max_pixels,
            max_side=self.max_side,
        )
        quality = dict(result.get("quality") or {})
        quality["page"] = idx + 1
        return {
            "text": _ru_decimal_point(_compact_text(result.get("text") or "")),
            "words": list(result.get("words") or []),
            "passes_used": int(result.get("passes_used") or 0),
            "quality": quality,
        }

    def _ocr_cache_envelope(self, idx, record):
        identity = self._ocr_settings_identity()
        return {
            "format": identity["format"],
            "schema": identity["schema"],
            "page": idx,
            "pdf_sha256": self._pdf_hash,
            "settings_key": self._settings_key,
            "settings": identity,
            "text": record.get("text", ""),
            "words": record.get("words", []),
            "quality": record.get("quality", {}),
        }

    def _read_cached_ocr(self, idx):
        """Read a reusable TSV OCR envelope; legacy records never qualify."""
        try:
            raw = self._cache_path(idx).read_bytes()
        except OSError:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        if (
            envelope.get("format") != _ocr_engine.CACHE_FORMAT
            or envelope.get("schema") != _ocr_engine.CACHE_SCHEMA
            or envelope.get("page") != idx
            or envelope.get("pdf_sha256") != self._pdf_hash
            or envelope.get("settings_key") != self._settings_key
        ):
            return None
        text = envelope.get("text")
        words = envelope.get("words")
        quality = envelope.get("quality")
        if not isinstance(text, str) or not isinstance(words, list):
            return None
        if not isinstance(quality, dict) or quality.get("state") not in (
            "usable",
            "uncertain",
        ):
            # Failures are never cached as text; only usable/uncertain reads.
            return None
        return {
            "text": _ru_decimal_point(_compact_text(text)),
            "words": words,
            "quality": quality,
        }

    def _peek_cached_ocr(self, idx):
        """Populate memory from reusable cache without new OCR or hit accounting.

        Hit counting and pending resolution stay in the per-position OCR loop
        so a preloaded page is counted exactly once when the position uses it.
        Cache read failures are ignored and never persisted.
        """
        record = self._ocr_pages.get(idx)
        if record is not None:
            return record["text"]
        record = self._read_cached_ocr(idx)
        if record is not None:
            self._ocr_pages[idx] = record
            self._ocr_texts[idx] = record["text"]
            return record["text"]
        return ""

    def _get_ocr_record(self, idx, allow_ocr=True):
        """Return the OCR evidence record, using memory/disk cache or real TSV OCR."""
        record = self._ocr_pages.get(idx)
        if record is not None:
            self.cache_hits += 1
            return record
        record = self._read_cached_ocr(idx)
        if record is not None:
            self._ocr_pages[idx] = record
            self._ocr_texts[idx] = record["text"]
            self.cache_hits += 1
            return record
        if not allow_ocr:
            return None
        try:
            record = self._ocr_page_result(idx)
            self.ocr_count += 1
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            self._ocr_errors[idx] = "OCR не выполнен (Tesseract %s): %s" % (
                self.language,
                str(exc)[:200],
            )
            return None
        state = (record.get("quality") or {}).get("state")
        if state in ("usable", "uncertain"):
            self._ocr_pages[idx] = record
            self._ocr_texts[idx] = record["text"]
            self._write_cache_bytes(
                self._cache_path(idx),
                _cache_payload(self._ocr_cache_envelope(idx, record)),
            )
            return record
        reasons = "; ".join(
            str(reason) for reason in (record.get("quality") or {}).get("reasons", [])
        )[:200]
        self._ocr_errors[idx] = "OCR %s (Tesseract %s)%s" % (
            state or "failed",
            self.language,
            (": %s" % reasons) if reasons else "",
        )
        return None

    def _get_ocr_text(self, idx, allow_ocr=True):
        """Compatibility: OCR text, using memory/disk cache or real TSV OCR."""
        record = self._get_ocr_record(idx, allow_ocr=allow_ocr)
        return record["text"] if record is not None else ""

    @staticmethod
    def _merge_text(native, ocr):
        if not ocr:
            return native
        if not native or native in ocr:
            return ocr
        return native + "\n" + ocr

    @staticmethod
    def _native_cell_text(page, cell):
        """Read one native grid cell in true glyph order via its bbox.

        ``table.extract()`` strings can merge rotated stacked glyphs in the
        wrong order (``631,2`` becomes ``6312\\n,``). ``get_textbox`` on the
        unrotated cell rect follows the native text layout, so punctuation
        stays attached; only whitespace is compacted. Failures fall back to
        an empty string so the grid keeps slots without fake text.
        """
        try:
            if cell is None or len(tuple(cell)) != 4:
                return ""
            rect = pymupdf.Rect(tuple(float(value) for value in cell))
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                return ""
            return _compact_text(page.get_textbox(rect)).strip()
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return ""

    def _native_grid_for(self, idx):
        """Physical grid plus full header ancestry, cached with canonical cells."""
        if idx in self._table_rows:
            return self._table_rows[idx]
        cached = self._read_cached_table_rows(idx)
        if cached is not None:
            self._table_rows[idx] = cached
            self.table_cache_hits += 1
            return cached
        page = self._doc[idx]
        if self._too_complex(page):
            self._table_errors[idx] = "native_table_vector_limit"
            return (), ()
        from pdf_tables import native_tables

        try:
            tables = native_tables(page, self._native_cell_text)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            self._table_errors[idx] = (
                f"native_table_failed:{type(exc).__name__}:{str(exc)[:160]}"
            )
            return (), ()
        return self._store_tables(idx, tables)

    def _store_tables(self, idx, tables, *, origin="native"):
        rows, locators = [], []
        for table in tables:
            cells = table["cells"]
            for row in range(table["row_count"]):
                slots = [
                    next(
                        (
                            cell["text"]
                            for cell in cells
                            if cell["row"] == row and cell["column"] == column
                        ),
                        "",
                    )
                    for column in range(table["col_count"])
                ]
                rows.append(" | ".join(slots))
            by_id = {cell["cell_id"]: cell for cell in cells}
            for cell in cells:
                locator = {
                    **cell,
                    "document_id": "pdf-%s" % self._pdf_hash[:12],
                    "page": idx + 1,
                    "table_id": table["table_id"],
                    "row_id": f"{table['table_id']}:r{cell['row'] + 1}",
                    "column_id": f"{table['table_id']}:c{cell['column'] + 1}",
                    "group_id": next(
                        (
                            f"{table['table_id']}:r{by_id[key]['row'] + 1}"
                            for key in cell["header_ids"]
                            if by_id[key]["role"] == "group"
                        ),
                        "",
                    ),
                    "rotation": 0,
                }
                locator["source_id"] = self._locator_id(locator)
                locators.append(locator)
        result = tuple(rows), tuple(locators)
        self._structured_tables[idx] = tables
        self._table_errors.pop(idx, None)
        self._table_origins[idx] = origin
        self._table_rows[idx] = result
        self._write_cached_table_rows(idx, result)
        return result

    def _scan_grid_for(self, idx):
        if self._table_origins.get(idx) == "ocr":
            return
        cached = self._read_cached_table_rows(idx)
        if cached is not None and self._table_origins.get(idx) == "ocr":
            self._table_rows[idx] = cached
            return
        from pdf_tables import raster_tables

        try:
            orientation = (self._ocr_pages.get(idx, {}).get("quality") or {}).get(
                "rotation", 0
            )
            tables = raster_tables(self._doc[idx], rotation=int(orientation))
            # Reuse the pipeline's locale normalization without changing source words.
            for table in tables:
                for cell in table["cells"]:
                    cell["text"] = _ru_decimal_point(cell["text"])
            self._store_tables(idx, tables, origin="ocr")
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            self._table_errors[idx] = (
                f"scan_table_failed:{type(exc).__name__}:{str(exc)[:160]}"
            )

    def _locator_id(self, locator):
        raw = _cache_payload(
            {
                "pdf": self._pdf_hash,
                "extraction": self._extraction_key,
                "table_key": self._table_key,
                "page": locator.get("page"),
                "origin": locator.get("origin", "native"),
                "table": locator.get("table_id", ""),
                "row": locator.get("row_id", ""),
                "column": locator.get("column_id", ""),
                "text": locator.get("text", ""),
                "bbox": locator.get("bbox", []),
            }
        )
        return "n%s" % hashlib.sha256(raw).hexdigest()[:16]

    def _ocr_locator_id(self, page, bbox, text, rotation, crop):
        raw = _cache_payload(
            {
                "pdf": self._pdf_hash,
                "extraction": self._extraction_key,
                "settings": self._settings_key,
                "page": page,
                "origin": "ocr",
                "rotation": rotation,
                "crop": crop,
                "text": text,
                "bbox": bbox,
            }
        )
        return "o%s" % hashlib.sha256(raw).hexdigest()[:16]

    def _ocr_line_id(self, page, rotation, crop, row_id, text):
        raw = _cache_payload(
            {
                "pdf": self._pdf_hash,
                "extraction": self._extraction_key,
                "settings": self._settings_key,
                "page": page,
                "origin": "ocr",
                "rotation": rotation,
                "crop": crop,
                "row": row_id,
                "text": text,
            }
        )
        return "o%s" % hashlib.sha256(raw).hexdigest()[:16]

    def _block_id(self, idx, origin="native"):
        raw = _cache_payload(
            {
                "pdf": self._pdf_hash,
                "extraction": self._extraction_key,
                "page": idx + 1,
                "origin": origin,
                "kind": "block",
            }
        )
        return "b%s" % hashlib.sha256(raw).hexdigest()[:16]

    def _table_rows_for(self, idx):
        if self._too_complex(self._doc[idx]):
            return ()
        """Compatibility: flattened rows for legacy scoring paths."""
        rows, _ = self._native_grid_for(idx)
        return rows

    def _read_cached_table_rows(self, idx):
        """Read reusable grid rows+locators; failures mean recompute, never reuse."""
        try:
            raw = self._table_path(idx).read_bytes()
        except OSError:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        if envelope.get("schema") != CACHE_SCHEMA_VERSION:
            return None
        if envelope.get("page") != idx:
            return None
        if envelope.get("pdf_sha256") != self._pdf_hash:
            return None
        if envelope.get("table_key") != self._table_key:
            return None
        rows = envelope.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
            return None
        locators = envelope.get("locators", [])
        if not isinstance(locators, list) or any(
            not isinstance(item, dict) for item in locators
        ):
            return None
        tables = envelope.get("tables")
        if not isinstance(tables, list):
            return None
        self._structured_tables[idx] = tables
        self._table_origins[idx] = envelope.get("origin", "native")
        return (tuple(rows), tuple(locators))

    def _write_cached_table_rows(self, idx, grid_result):
        """Persist grid rows+locators, including empty results; failures are ignored."""
        rows, locators = grid_result
        envelope = {
            "schema": CACHE_SCHEMA_VERSION,
            "page": idx,
            "pdf_sha256": self._pdf_hash,
            "table_key": self._table_key,
            "rows": list(rows),
            "locators": [dict(item) for item in locators],
            "tables": self._structured_tables.get(idx, []),
            "origin": self._table_origins.get(idx, "native"),
        }
        self._write_cache_bytes(self._table_path(idx), _cache_payload(envelope))

    def _ocr_locators(self):
        """Build origin-``ocr`` word/line locators for recognized pages.

        Fail-closed binding: a word becomes a ``cell`` only when the engine
        resolved one repeated column strip AND the strip is a header column
        (its top-row cell is a confident label, conf>=60). Otherwise words
        stay word-level with their row band; no invented columns or headers.
        """
        grouped: dict[int, list[dict]] = {}
        document_id = "pdf-%s" % self._pdf_hash[:12]
        for idx, record in self._ocr_pages.items():
            quality = record.get("quality") or {}
            state = quality.get("state")
            if state not in ("usable", "uncertain"):
                continue
            rotation = int(quality.get("rotation") or 0)
            words = [dict(word) for word in (record.get("words") or [])]
            if not words:
                continue
            layout = quality.get("layout") or {}
            cols = int(layout.get("cols") or 0)
            resolved = bool(layout.get("resolved"))
            # Header column: top row band holds a confident label in this
            # strip. Numeric-only strips never become header columns.
            header_cols: set[str] = set()
            if resolved and cols:
                top_rows = sorted(
                    {
                        str(word.get("row_id") or "")
                        for word in words
                        if word.get("row_id")
                    }
                )[:1]
                for word in words:
                    if (
                        top_rows
                        and str(word.get("row_id") or "") == top_rows[0]
                        and word.get("column_id")
                        and int(word.get("conf", -1)) >= 60
                        and word.get("kind") == "label"
                    ):
                        header_cols.add(str(word.get("column_id")))
            locators: list[dict] = []
            for word in words:
                bbox = [float(value) for value in (word.get("bbox") or [])]
                if len(bbox) != 4:
                    continue
                text = str(word.get("text") or "")
                if not text:
                    continue
                crop = str(word.get("crop") or "full")
                column_id = str(word.get("column_id") or "")
                # Unique row + header column required; else word-level.
                bound = (
                    resolved
                    and bool(column_id)
                    and column_id in header_cols
                    and bool(word.get("row_id"))
                )
                role = "cell" if bound else "word"
                if not bound:
                    column_id = ""
                locators.append(
                    {
                        "source_id": self._ocr_locator_id(
                            idx + 1, bbox, text, rotation, crop
                        ),
                        "document_id": document_id,
                        "page": idx + 1,
                        "text": text[:MAX_TABLE_CELL_CHARS],
                        "bbox": bbox,
                        "origin": "ocr",
                        "table_id": "",
                        "row_id": str(word.get("row_id") or ""),
                        "column_id": column_id,
                        "header_ids": [column_id] if bound else [],
                        "group_id": "",
                        "scope_key": "",
                        "role": role,
                        "quality": state,
                        "rotation": rotation,
                        "crop": crop,
                        "transform": str(quality.get("transform") or ""),
                        "confidence": int(word.get("conf", -1)),
                    }
                )
            bands: dict[str, list[dict]] = {}
            for locator in locators:
                bands.setdefault(locator["row_id"] or "r1", []).append(locator)
            for row_id, members in sorted(bands.items()):
                members = sorted(members, key=lambda item: item["bbox"][0])
                line = " ".join(item["text"] for item in members).strip()
                if not line:
                    continue
                x0 = min(item["bbox"][0] for item in members)
                y0 = min(item["bbox"][1] for item in members)
                x1 = max(item["bbox"][2] for item in members)
                y1 = max(item["bbox"][3] for item in members)
                crop = members[0].get("crop", "full")
                locators.append(
                    {
                        "source_id": self._ocr_line_id(
                            idx + 1, rotation, str(crop), row_id, line
                        ),
                        "document_id": document_id,
                        "page": idx + 1,
                        "text": line[:MAX_TABLE_CELL_CHARS],
                        "bbox": [x0, y0, x1, y1],
                        "origin": "ocr",
                        "table_id": "",
                        "row_id": row_id,
                        "column_id": "",
                        "header_ids": [],
                        "group_id": "",
                        "scope_key": "",
                        "role": "line",
                        "quality": state,
                        "rotation": rotation,
                        "crop": str(crop),
                        "transform": str(quality.get("transform") or ""),
                        "confidence": min(
                            int(item.get("confidence", -1)) for item in members
                        ),
                    }
                )
            grouped[idx] = locators
        return grouped

    def for_position(
        self,
        target_indices,
        work_name,
        volume,
        reference="",
        comment="",
        max_ocr_pages=4,
        expected_scope=None,
    ):
        """OCR at most max_ocr_pages related scans; return immutable snapshot.

        Reusable memory/disk OCR for all pending scans is preloaded before
        ranking (no new OCR charge) and ranking uses the merged current text,
        so a cached scan unrelated to the current reference can still rank.
        New OCR prioritizes referenced scans, then ranked/neighbour scans,
        then bounded discovery of remaining pending scans in page order while
        spare allowance remains. Native pages are always fully present.
        Flattened table rows and native grid locators are extracted only
        for related native pages, at most MAX_TABLE_PAGES_PER_POSITION per
        position, and reused from the persistent cache. ``expected_scope``
        is accepted for signature parity with related_context callers and
        only threads through ranking helpers; native extraction itself
        never filters by scope. Text block locators are derived from the
        final merged page texts with unrotated page bboxes; OCR blocks
        stay ``native``-origin here until stage 5 quality states land.
        """
        self._require_owner_thread("OCR сканов PDF")
        page_count = len(self._native_texts)
        targets = [
            i
            for i in dict.fromkeys(target_indices)
            if isinstance(i, int) and 0 <= i < page_count
        ]
        for idx in sorted(self._pending):
            if idx not in self._ocr_errors:
                self._peek_cached_ocr(idx)
        merged = [
            self._merge_text(native, self._ocr_texts.get(idx, ""))
            for idx, native in enumerate(self._native_texts)
        ]
        current_errors = dict(self._native_errors)
        current_errors.update(self._ocr_errors)
        # Ranking uses page text only. Reuse tokenization and weights until
        # newly recognized pages actually change that text.
        merged = tuple(merged)
        if getattr(self, "_ranking_texts", None) != merged:
            self._ranking_index = PageTextIndex(merged)
            self._ranking_texts = merged
        ranked = self._ranking_index.candidates(work_name, comment, reference, volume)
        top = ranked[:TOP_CANDIDATES_PER_POSITION]
        pool = set(targets) | set(top)
        for idx in list(targets) + list(top):
            if idx - 1 >= 0:
                pool.add(idx - 1)
            if idx + 1 < page_count:
                pool.add(idx + 1)
        eligible = [idx for idx in targets if idx in self._pending]
        eligible += [
            idx for idx in sorted(pool) if idx in self._pending and idx not in eligible
        ]
        discovery = [idx for idx in sorted(self._pending) if idx not in eligible]
        # Cached related pages do not consume the new-recognition allowance.
        remaining = max(0, max_ocr_pages)
        started_remaining = remaining
        for idx in eligible:
            remaining = self._ocr_one(idx, remaining)
        if not eligible or remaining < started_remaining:
            # Nothing related to try (missing reference), or the related area
            # was fresh territory: spend spare allowance on remaining pending
            # scans in page order. A fully cached related area means the
            # neighbourhood is already known, so do not burn more budget.
            for idx in discovery:
                remaining = self._ocr_one(idx, remaining, allow_deferred_skip=True)
        self._pending.difference_update(self._ocr_texts)
        texts = [
            self._merge_text(native, self._ocr_texts.get(idx, ""))
            for idx, native in enumerate(self._native_texts)
        ]
        errors = dict(self._native_errors)
        errors.update(self._ocr_errors)
        table_pages = [
            idx
            for idx in dict.fromkeys(targets + top + sorted(pool))
            if self._native_texts[idx].strip() and idx not in self._ocr_pages
        ]
        table_pages = table_pages[:MAX_TABLE_PAGES_PER_POSITION]
        flattened = [() for _ in texts]
        page_locators: list[list[dict]] = [[] for _ in texts]
        for idx in table_pages:
            rows, locators = self._native_grid_for(idx)
            flattened[idx] = rows
            page_locators[idx] = list(locators)
            document_id = "pdf-%s" % self._pdf_hash[:12]
            for locator in page_locators[idx]:
                locator["document_id"] = document_id
        for idx, record in self._ocr_locators().items():
            page_locators[idx].extend(record)
        for idx in [page for page in self._ocr_pages if page in pool][
            :MAX_TABLE_PAGES_PER_POSITION
        ]:
            if not self._table_errors.get(idx, "").startswith("scan_table_failed"):
                self._scan_grid_for(idx)
                if idx in self._structured_tables and idx in self._table_rows:
                    rows, locators = self._table_rows[idx]
                    flattened[idx] = rows
                    page_locators[idx] = list(locators) + page_locators[idx]
                    if rows:
                        texts[idx] += "\n" + "\n".join(rows)
        for idx, text in enumerate(texts):
            native = self._native_texts[idx]
            if text.strip():
                has_native = bool(native.strip())
                has_ocr = idx in self._ocr_pages
                origin = "native" if has_native else ("ocr" if has_ocr else "native")
                quality_state = "usable"
                if has_ocr:
                    quality_state = (self._ocr_pages[idx].get("quality") or {}).get(
                        "state", "uncertain"
                    )
                page_locators[idx].append(
                    {
                        "source_id": self._block_id(idx, origin),
                        "document_id": "pdf-%s" % self._pdf_hash[:12],
                        "page": idx + 1,
                        "text": text[:MAX_TABLE_CELL_CHARS],
                        "bbox": [],
                        "origin": origin,
                        "table_id": "",
                        "row_id": "",
                        "column_id": "",
                        "header_ids": [],
                        "group_id": "",
                        "scope_key": "",
                        "role": "block",
                        "quality": quality_state,
                        "rotation": 0,
                    }
                )
        quality = {}
        for idx in range(len(texts)):
            record = self._ocr_pages.get(idx)
            if record is not None:
                state = dict(record.get("quality") or {})
                # Stable hashed evidence only: volatile run counters and raw
                # word lists stay outside quality; identity lives in metadata.
                state.pop("words", None)
                quality[idx] = state
            elif idx in self._pending:
                quality[idx] = {
                    "state": "pending",
                    "origin": "ocr",
                    "page": idx + 1,
                    "reasons": ["not_recognized_allowance_or_deferred"],
                }
        metadata = {
            "pdf_sha256": self._pdf_hash,
            "extraction_key": self._extraction_key,
            "ocr_settings_key": self._settings_key,
            "ocr": self._ocr_settings_identity(),
            "table_errors": {
                str(idx + 1): reason for idx, reason in self._table_errors.items()
            },
        }
        repeated = set(self._repeated_lines) | _repeated_lines(list(texts))
        return PageTextIndex(
            tuple(texts),
            errors,
            tuple(sorted(self._pending)),
            tuple(flattened),
            locators=tuple(page_locators),
            quality=quality,
            metadata=metadata,
            tables=tuple(
                self._structured_tables.get(idx, []) for idx in range(len(texts))
            ),
            repeated_lines=repeated,
        )

    def _ocr_one(self, idx, remaining, allow_deferred_skip=False):
        """OCR one pending scan; return the remaining new-OCR allowance."""
        if idx in self._ocr_errors:
            return remaining
        if allow_deferred_skip and idx not in self._ocr_texts and remaining <= 0:
            return remaining
        before = self.cache_hits
        with span(
            "read-ocr-page",
            as_type="tool",
            input={
                "page": idx + 1,
                "allow_new_ocr": remaining > 0,
                "language": self.language,
                "requested_dpi": self.dpi,
                "max_pixels": self.max_pixels,
                "max_side": self.max_side,
            },
        ) as observation:
            record = self._get_ocr_record(idx, allow_ocr=remaining > 0)
            text = record["text"] if record is not None else ""
            error = self._ocr_errors.get(idx)
            cache_hit = self.cache_hits > before
            deferred = idx not in self._ocr_texts and not error and remaining <= 0
            quality = (record.get("quality") or {}) if record is not None else {}
            if error:
                outcome = error
            elif cache_hit:
                outcome = f"Кеш: {len(text)} симв."
            elif deferred:
                outcome = "Отложен: исчерпан лимит новых OCR"
            elif text:
                outcome = f"Новый OCR: {len(text)} симв."
            else:
                outcome = "OCR вернул пустой текст"
            observation.update(
                output={
                    "text": text,
                    "error": error,
                    "cache_hit": cache_hit,
                    "deferred": deferred,
                    "quality": quality.get("state") if quality else None,
                    "rotation": quality.get("rotation") if quality else None,
                    "passes": quality.get("passes") if quality else None,
                    "words_kept": quality.get("words_kept") if quality else None,
                },
                level="ERROR" if error else "DEFAULT",
                status_message=outcome,
            )
        if self.cache_hits == before and remaining > 0:
            remaining -= 1
        if idx in self._ocr_texts:
            self._pending.discard(idx)
        return remaining
