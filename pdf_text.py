"""Native PDF indexing and bounded OCR of scans related to a VOR position."""

from __future__ import annotations

import hashlib
import os
import math
import pymupdf
import threading
from pathlib import Path

from audit_core import PageTextIndex
from audit_tracing import span

# Short text with raster content needs OCR; short native-only pages do not.
NATIVE_MIN_CHARS = 150
# Locally ranked candidates (plus neighbours) eligible for OCR per position.
TOP_CANDIDATES_PER_POSITION = 5




def _engine_fingerprint():
    """Practical OCR engine version / tessdata fingerprint for the cache key."""
    engine = pymupdf.VersionBind
    try:
        tessdata_dir = pymupdf.get_tessdata()
    except RuntimeError:
        tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
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
    def _read_native(doc):
        texts, errors, scans = [], {}, []
        for idx in range(len(doc)):
            try:
                page = doc[idx]
                text = page.get_text(sort=True).strip()
                texts.append(text)
                if len(text) < NATIVE_MIN_CHARS and page.get_images():
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
        return page.get_text(textpage=textpage, sort=True).strip()

    def _get_ocr_text(self, idx, allow_ocr=True):
        """Return OCR text, using memory/disk cache or real OCR."""
        if idx in self._ocr_texts:
            self.cache_hits += 1
            return self._ocr_texts[idx]
        path = self._cache_path(idx)
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    self._ocr_texts[idx] = text
                    self.cache_hits += 1
                    return text
        except Exception:
            pass
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

    def for_position(self, target_indices, work_name, volume, reference="",
                     comment="", max_ocr_pages=4):
        """OCR at most max_ocr_pages related scans; return immutable snapshot.

        Only scans among the referenced pages, their neighbours and the
        neighbours of the locally ranked top candidates are eligible. Native
        pages are always fully present in the snapshot.
        """
        self._require_owner_thread("OCR сканов PDF")
        page_count = len(self._native_texts)
        targets = [i for i in dict.fromkeys(target_indices)
                   if isinstance(i, int) and 0 <= i < page_count]
        ranked = self.native_index.candidates(
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
        # Cached related pages do not consume the new-recognition allowance.
        remaining = max(0, max_ocr_pages)
        for idx in eligible:
            if idx in self._ocr_errors:
                continue
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
        texts = [self._merge_text(native, self._ocr_texts.get(idx, ""))
                 for idx, native in enumerate(self._native_texts)]
        errors = dict(self._native_errors)
        errors.update(self._ocr_errors)
        return PageTextIndex(tuple(texts), errors, tuple(sorted(self._pending)))
