"""Native PDF indexing and bounded OCR of scans related to a VOR position."""

from __future__ import annotations

import hashlib
import os
import math
import re
import pymupdf
import threading
import shutil
from pathlib import Path

from audit_core import PageTextIndex
from audit_tracing import span

# Short text with raster content needs OCR; short native-only pages do not.
NATIVE_MIN_CHARS = 150
# Locally ranked candidates (plus neighbours) eligible for OCR per position.
TOP_CANDIDATES_PER_POSITION = 5
# Structurally scanned pages per position, bounded like OCR allowances.
MAX_TABLE_PAGES_PER_POSITION = 8
# Cell text kept per row; long descriptions do not need full text for row matching.
MAX_TABLE_CELL_CHARS = 200

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
    """Native text index plus bounded, cached OCR for selected scans."""

    def __init__(self, doc, cache_dir=".audit_cache/ocr", language="rus+eng",
                 dpi=200, max_pixels=12000000, max_side=4096):
        self._owner_thread = threading.get_ident()
        if min(dpi, max_pixels, max_side) <= 0:
            raise ValueError("Параметры OCR должны быть положительными")
        self._doc = doc
        self._cache_dir = Path(cache_dir)
        self.language = language
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.max_side = max_side
        self.ocr_count = 0
        self.cache_hits = 0
        self._ocr_texts = {}
        self._ocr_errors = {}
        self._table_rows = {}
        self._native_texts, self._native_errors, pending = self._read_native(doc)
        self._pdf_hash = self._pdf_sha256(doc)
        self._settings_key = self._settings_key_for(
            language, dpi, max_pixels, max_side)
        self._pending = set(pending)
        self.native_index = PageTextIndex(
            self._native_texts, self._native_errors, pending)

    @property
    def page_count(self):
        return len(self._native_texts)

    @property
    def pending_ocr(self):
        return tuple(sorted(self._pending))

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
    def _read_native(doc):
        texts, errors, scans = [], {}, []
        for idx in range(len(doc)):
            try:
                page = doc[idx]
                text = _compact_text(page.get_text(sort=True))
                texts.append(text)
                if len(text) >= NATIVE_MIN_CHARS:
                    continue
                if page.get_images():
                    scans.append(idx)
                elif PDFTextSource._has_vector_drawings(page):
                    scans.append(idx)
            except Exception as exc:
                if len(texts) <= idx:
                    texts.append("")
                errors[idx] = "Сбой чтения PDF: %s" % str(exc)[:200]
        return tuple(texts), errors, scans

    @staticmethod
    def _pdf_sha256(doc):
        # Hash original bytes when available: serialization can change PDF IDs.
        if doc.name and not doc.is_dirty:
            with Path(doc.name).open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()
        return hashlib.sha256(doc.tobytes(no_new_id=True)).hexdigest()

    @staticmethod
    def _settings_key_for(language, dpi, max_pixels, max_side):
        engine, tess_fp = _engine_fingerprint()
        raw = repr((language, dpi, max_pixels, max_side, engine, tess_fp))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _cache_path(self, idx):
        return (self._cache_dir / self._pdf_hash
                / ("p%05d_%s.txt" % (idx, self._settings_key)))

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
            try:
                path = self._cache_path(idx)
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(
                    "%s.tmp-%d-%d" % (path.name, os.getpid(),
                                     threading.get_ident()))
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
            except Exception:
                pass
        return text

    @staticmethod
    def _merge_text(native, ocr):
        if not ocr:
            return native
        if not native or native in ocr:
            return ocr
        return native + "\n" + ocr

    def _table_rows_for(self, idx):
        """Extract flattened table rows once; vectors keep row-level matches."""
        if idx in self._table_rows:
            return self._table_rows[idx]
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
        self._table_rows[idx] = tuple(rows)
        return self._table_rows[idx]

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
        at most MAX_TABLE_PAGES_PER_POSITION per position.
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
