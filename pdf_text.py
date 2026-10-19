"""Structured PDF Markdown indexing and bounded OCR of related scans."""

from __future__ import annotations

import hashlib
import json
import math
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
# Persistent extraction cache schema; bump when envelopes or detection change.
CACHE_SCHEMA_VERSION = 1
# PyMuPDF4LLM table rendering; part of the Markdown cache key.
TABLE_OUTPUT = "html"
# Exact Markdown call parameters; the cache key reuses them so call changes invalidate.
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
}

def _compact_text(text):
    """Drop layout padding before character budgets, preserving source line order."""
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)

def _engine_fingerprint():
    """Practical OCR engine version / tessdata fingerprint for the cache key."""
    engine = pymupdf.VersionBind
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if not tessdata_dir and shutil.which("tesseract"):
        try:
            tessdata_dir = pymupdf.get_tessdata()
        except RuntimeError:
            tessdata_dir = ""
    entries = []
    if tessdata_dir:
        for path in sorted(Path(tessdata_dir).glob("*.traineddata")):
            stat = path.stat()
            entries.append((path.name, stat.st_size, stat.st_mtime_ns))
    return engine, repr((tessdata_dir, entries))


def _parser_fingerprint():
    """Extractor versions reflected in the Markdown/table cache key."""
    try:
        layout = _pkg_version("pymupdf-layout")
    except Exception:
        layout = "unknown"
    try:
        extractor = _pkg_version("pymupdf4llm")
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
    width_pt, height_pt = float(page.rect.width), float(page.rect.height)
    if min(width_pt, height_pt, dpi, max_pixels, max_side) <= 0:
        raise ValueError("Некорректные размеры страницы или лимиты изображения")
    scale = min(dpi / 72.0, max_side / max(width_pt, height_pt),
                math.sqrt(max_pixels / (width_pt * height_pt)))
    dpi = min(dpi, int(scale * 72))
    while dpi > 0:
        width, height = math.ceil(width_pt * dpi / 72), math.ceil(height_pt * dpi / 72)
        if width * height <= max_pixels and max(width, height) <= max_side:
            return dpi
        dpi -= 1
    raise ValueError("Размер страницы не позволяет рендер в пределах бюджета изображения")


class PDFTextSource:
    """PyMuPDF4LLM Markdown index with a versioned persistent extraction cache."""

    def __init__(self, doc, cache_dir=".audit_cache/pdf", language="rus+eng",
                 dpi=200, max_pixels=12000000, max_side=4096, progress=None):
        self._progress = progress
        self._owner_thread = threading.get_ident()
        if min(dpi, max_pixels, max_side) <= 0:
            raise ValueError("Параметры OCR должны быть положительными")
        if getattr(doc, "is_closed", False):
            raise ValueError(
                "PDF-документ закрыт: переоткройте файл и повторите подготовку позиции")
        self._pdf_bytes = b""
        if getattr(doc, "name", "") and not getattr(doc, "is_dirty", False):
            self._doc = pymupdf.open(doc.name)
        else:
            self._pdf_bytes = doc.tobytes(no_new_id=True)
            self._doc = pymupdf.open(stream=self._pdf_bytes, filetype="pdf")
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
        self._table_rows = {}
        self._extraction_key = self._extraction_key_for(TABLE_OUTPUT)
        self._settings_key = self._settings_key_for(
            language, dpi, max_pixels, max_side)
        self._table_key = self._table_key_for(MAX_TABLE_CELL_CHARS)
        try:
            self._native_texts, self._native_errors, pending = self._read_native()
        except BaseException:
            self.close()
            raise
        self._pending = set(pending)
        self.native_index = PageTextIndex(
            self._native_texts, self._native_errors, pending)

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
        except Exception:
            pass

    def _require_owner_thread(self, what):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError(
                "PDFTextSource: %s доступно только в потоке подготовки индекса; "
                "в рабочие потоки передавайте только готовые снимки "
                "PageTextIndex и строки." % what)

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
                stream=doc.tobytes(no_new_id=True), filetype="pdf")
        with markdown_doc:
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
                try:
                    page = markdown_doc[idx]
                    # Scan eligibility uses compacted source text, not Markdown markup.
                    native = _compact_text(page.get_text(sort=True))
                    if self._too_complex(page):
                        texts.append(native)
                        errors[idx] = "Сложный векторный чертёж: анализ разметки ограничен; требуется ручная проверка страницы"
                        if self._progress:
                            self._progress(idx + 1, len(doc))
                        continue
                    is_scan = (
                        len(native) < NATIVE_MIN_CHARS
                        and bool(page.get_images()
                                 or PDFTextSource._has_vector_drawings(page)))
                    if is_scan:
                        scans.append(idx)
                    self.extraction_misses += 1
                    chunks = pymupdf4llm.to_markdown(
                        markdown_doc, pages=[idx], **_MARKDOWN_KWARGS)
                    text = _compact_text(chunks[0]["text"])
                    texts.append(text)
                    self._write_cached_extraction(idx, text, is_scan)
                except Exception as exc:
                    texts.append("")
                    errors[idx] = "Сбой чтения PDF (PyMuPDF4LLM): %s" % str(exc)[:200]
                if self._progress:
                    self._progress(idx + 1, len(doc))
        return tuple(texts), errors, scans

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
                "engine": "rag-no-layout-v1",
                "max_vector_paths": MAX_VECTOR_PATHS,
                "pymupdf": pymupdf_version,
                "pymupdf4llm": extractor_version,
                "pymupdf_layout": layout_version,
                "table_output": table_output,
                "markdown": _MARKDOWN_KWARGS,
                "native_min_chars": NATIVE_MIN_CHARS,
            }
        )
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _settings_key_for(language, dpi, max_pixels, max_side):
        engine, tess_fp = _engine_fingerprint()
        raw = repr((language, dpi, max_pixels, max_side, engine, tess_fp))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _table_key_for(max_cell_chars):
        engine = pymupdf.VersionBind
        raw = _cache_payload(
            {
                "schema": CACHE_SCHEMA_VERSION,
                "pymupdf": str(engine),
                "max_cell_chars": max_cell_chars,
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
            / ("page-%05d.txt" % idx)
        )

    def _read_cached_extraction(self, idx):
        """Read one reusable Markdown page; failures mean recompute, never reuse."""
        try:
            raw = self._extraction_path(idx).read_bytes()
        except OSError:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
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

    @staticmethod
    def _write_cache_bytes(path, payload):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name("%s.tmp-%d-%d" % (path.name, os.getpid(),
                                                 threading.get_ident()))
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        except Exception:
            pass

    def _ocr_page(self, idx):
        """Real PyMuPDF OCR of one scan page; no mock/fallback recognition."""
        page = self._doc[idx]
        textpage = page.get_textpage_ocr(
            language=self.language, full=True,
            dpi=bounded_page_dpi(page, self.dpi, self.max_pixels, self.max_side))
        return _compact_text(page.get_text(textpage=textpage, sort=True))

    def _read_cached_ocr(self, idx):
        """Read reusable disk OCR; failures are ignored and never persisted."""
        try:
            path = self._cache_path(idx)
            if path.is_file():
                text = _compact_text(path.read_text(encoding="utf-8"))
                if text.strip():
                    return text
        except Exception:
            pass
        return ""

    def _peek_cached_ocr(self, idx):
        """Populate memory from reusable cache without new OCR or hit accounting.

        Hit counting and pending resolution stay in the per-position OCR loop
        so a preloaded page is counted exactly once when the position uses it.
        Cache read failures are ignored and never persisted.
        """
        if idx in self._ocr_texts:
            return self._ocr_texts[idx]
        text = self._read_cached_ocr(idx)
        if text.strip():
            self._ocr_texts[idx] = text
            return text
        return ""

    def _get_ocr_text(self, idx, allow_ocr=True):
        """Return OCR text, using memory/disk cache or real OCR."""
        if idx in self._ocr_texts:
            self.cache_hits += 1
            return self._ocr_texts[idx]
        text = self._read_cached_ocr(idx)
        if text.strip():
            self._ocr_texts[idx] = text
            self.cache_hits += 1
            return text
        if not allow_ocr:
            return ""
        try:
            text = self._ocr_page(idx)
            self.ocr_count += 1
        except Exception as exc:
            self._ocr_errors[idx] = (
                "OCR не выполнен (Tesseract %s): %s"
                % (self.language, str(exc)[:200]))
            return ""
        self._ocr_texts[idx] = text
        if text:
            self._write_cache_bytes(self._cache_path(idx), text.encode("utf-8"))
        return text

    @staticmethod
    def _merge_text(native, ocr):
        if not ocr:
            return native
        if not native or native in ocr:
            return ocr
        return native + "\n" + ocr

    def _table_rows_for(self, idx):
        if self._too_complex(self._doc[idx]):
            return ()
        """Flattened table rows, reused from disk; vectors keep row-level matches."""
        if idx in self._table_rows:
            return self._table_rows[idx]
        cached = self._read_cached_table_rows(idx)
        if cached is not None:
            self._table_rows[idx] = cached
            self.table_cache_hits += 1
            return cached
        rows = []
        try:
            finder = self._doc[idx].find_tables()
        except Exception:
            finder = None
        tables = getattr(finder, "tables", None) or []
        for table in tables:
            try:
                extracted = table.extract()
            except Exception:
                continue
            for cells in extracted or []:
                row = " | ".join(str(cell).strip()[:MAX_TABLE_CELL_CHARS]
                                for cell in cells if str(cell).strip())
                if row:
                    rows.append(row)
        rows = tuple(rows)
        self._table_rows[idx] = rows
        self._write_cached_table_rows(idx, rows)
        return rows

    def _read_cached_table_rows(self, idx):
        """Read reusable flattened rows; failures mean recompute, never reuse."""
        try:
            raw = self._table_path(idx).read_bytes()
        except OSError:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
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
        return tuple(rows)

    def _write_cached_table_rows(self, idx, rows):
        """Persist flattened rows, including empty results; failures are ignored."""
        envelope = {
            "schema": CACHE_SCHEMA_VERSION,
            "page": idx,
            "pdf_sha256": self._pdf_hash,
            "table_key": self._table_key,
            "rows": list(rows),
        }
        self._write_cache_bytes(self._table_path(idx), _cache_payload(envelope))

    def for_position(self, target_indices, work_name, volume, reference="",
                     comment="", max_ocr_pages=4):
        """OCR at most max_ocr_pages related scans; return immutable snapshot.

        Reusable memory/disk OCR for all pending scans is preloaded before
        ranking (no new OCR charge) and ranking uses the merged current text,
        so a cached scan unrelated to the current reference can still rank.
        New OCR prioritizes referenced scans, then ranked/neighbour scans,
        then bounded discovery of remaining pending scans in page order while
        spare allowance remains. Native pages are always fully present.
        Flattened table rows are extracted only for related native pages,
        at most MAX_TABLE_PAGES_PER_POSITION per position, and reused
        from the persistent cache.
        """
        self._require_owner_thread("OCR сканов PDF")
        page_count = len(self._native_texts)
        targets = [i for i in dict.fromkeys(target_indices)
                   if isinstance(i, int) and 0 <= i < page_count]
        for idx in sorted(self._pending):
            if idx not in self._ocr_errors:
                self._peek_cached_ocr(idx)
        merged = [self._merge_text(native, self._ocr_texts.get(idx, ""))
                  for idx, native in enumerate(self._native_texts)]
        current_errors = dict(self._native_errors)
        current_errors.update(self._ocr_errors)
        ranked = PageTextIndex(
            tuple(merged), current_errors,
            tuple(sorted(self._pending))).candidates(
                work_name, comment, reference, volume)
        top = ranked[:TOP_CANDIDATES_PER_POSITION]
        pool = set(targets) | set(top)
        for idx in list(targets) + list(top):
            if idx - 1 >= 0:
                pool.add(idx - 1)
            if idx + 1 < page_count:
                pool.add(idx + 1)
        eligible = [idx for idx in targets if idx in self._pending]
        eligible += [idx for idx in sorted(pool) if idx in self._pending and idx not in eligible]
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
        self._pending.difference_update(idx for idx, text in self._ocr_texts.items() if text.strip())
        texts = [self._merge_text(native, self._ocr_texts.get(idx, ""))
                 for idx, native in enumerate(self._native_texts)]
        errors = dict(self._native_errors)
        errors.update(self._ocr_errors)
        table_pages = [idx for idx in targets + top if self._native_texts[idx].strip()]
        table_pages += [idx for idx in sorted(pool) if idx not in table_pages
                        and self._native_texts[idx].strip()]
        table_pages = table_pages[:MAX_TABLE_PAGES_PER_POSITION]
        flattened = [() for _ in texts]
        for idx in table_pages:
            flattened[idx] = self._table_rows_for(idx)
        return PageTextIndex(tuple(texts), errors, tuple(sorted(self._pending)),
                             tuple(flattened))

    def _ocr_one(self, idx, remaining, allow_deferred_skip=False):
        """OCR one pending scan; return the remaining new-OCR allowance."""
        if idx in self._ocr_errors:
            return remaining
        if allow_deferred_skip and idx not in self._ocr_texts and remaining <= 0:
            return remaining
        before = self.cache_hits
        with span("read-ocr-page", as_type="tool",
                  input={"page": idx + 1, "allow_new_ocr": remaining > 0,
                         "language": self.language, "requested_dpi": self.dpi,
                         "max_pixels": self.max_pixels, "max_side": self.max_side}) as observation:
            text = self._get_ocr_text(idx, allow_ocr=remaining > 0)
            error = self._ocr_errors.get(idx)
            cache_hit = self.cache_hits > before
            deferred = not text and not error and remaining <= 0
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
            observation.update(output={"text": text, "error": error,
                                       "cache_hit": cache_hit, "deferred": deferred},
                               level="ERROR" if error else "DEFAULT",
                               status_message=outcome)
        if self.cache_hits == before and remaining > 0:
            remaining -= 1
        if self._ocr_texts.get(idx, "").strip():
            self._pending.discard(idx)
        return remaining
