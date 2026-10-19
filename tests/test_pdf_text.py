"""Tests for pdf_text.PDFTextSource: structured Markdown index, bounded OCR."""

import json
import os
import re
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_core import QuoteValidationError, parse_audit_response
from lxml import html
from pdf_text import CACHE_SCHEMA_VERSION, PDFTextSource


class FakeRect:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class FakePage:
    def __init__(self, native, ocr_text="", fail=None, rect=(595, 842),
                 calls=None, idx=0, images=True, drawings=0,
                 tables=(), table_calls=None):
        self._native = native
        self._ocr_text = ocr_text
        self._fail = fail
        self.rect = FakeRect(*rect)
        self._calls = calls if calls is not None else []
        self._idx = idx
        self._images = images
        self._drawings = drawings
        self._tables = tables
        self._table_calls = table_calls if table_calls is not None else []

    def get_text(self, sort=True, textpage=None):
        if textpage is not None:
            return textpage
        if isinstance(self._native, Exception):
            raise self._native
        return self._native

    def get_images(self):
        return [("scan-image",)] if self._images else []

    def get_drawings(self):
        return [{}] * self._drawings

    def get_textpage_ocr(self, language="rus+eng", dpi=200, full=False):
        self._calls.append({"idx": self._idx, "language": language,
                            "dpi": dpi, "full": full})
        if self._fail is not None:
            raise self._fail
        return self._ocr_text

    def find_tables(self):
        from types import SimpleNamespace
        self._table_calls.append(self._idx)
        tables = [SimpleNamespace(extract=lambda rows=rows: rows) for rows in self._tables]
        return SimpleNamespace(tables=tables)


_CLONE_ORIGINS = {}


class FakeDoc:
    def __init__(self, pages, payload=b"fake-pdf-bytes"):
        self._pages = pages
        self.name = ""
        self.is_dirty = False
        self._payload = payload
        # Private-clone lookup for the patched pymupdf.open: the last doc
        # registered under a payload wins. Every test below constructs its
        # PDFTextSource before creating the next doc, so the lookup always
        # resolves to the doc under test.
        _CLONE_ORIGINS[payload] = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, idx):
        return self._pages[idx]

    def tobytes(self, **kwargs):
        return self._payload


def make_doc(native_texts, ocr_texts=None, **kwargs):
    calls = kwargs.pop("calls", None)
    if calls is None:
        calls = []
    table_calls = kwargs.pop("table_calls", None)
    if table_calls is None:
        table_calls = []
    table_pages = kwargs.pop("table_pages", None)
    payload = kwargs.pop("payload", b"fake-pdf-bytes")
    pages = []
    for idx, native in enumerate(native_texts):
        ocr = (ocr_texts[idx] if ocr_texts is not None else "распознанный текст %d" % idx)
        tables = table_pages.get(idx, ()) if table_pages else ()
        pages.append(FakePage(native, ocr_text=ocr, calls=calls, idx=idx,
                              tables=tables, table_calls=table_calls, **kwargs))
    return FakeDoc(pages, payload=payload), calls


def _fake_to_markdown(doc, pages=None, **kwargs):
    """Per-page Markdown chunk derived from the FakeDoc native text."""
    idx = list(pages or [0])[0]
    return [{"text": doc[idx].get_text(sort=True),
             "metadata": {"page": idx},
             "toc_items": [],
             "page_boxes": []}]


def _fake_open_pdf(*args, **kwargs):
    """Yield the FakeDoc itself as the private Markdown clone."""
    stream = kwargs.get("stream", args[0] if args else None)
    try:
        return _CLONE_ORIGINS[stream]
    except KeyError:
        raise AssertionError("FakeSourceTests: unknown PDF stream for pymupdf.open")


class FakeSourceTests(unittest.TestCase):
    """Fake-document tests: clone opening and Markdown extraction are faked.

    Production Markdown calls the private clone, never the source doc, so the
    clone opening is faked here as well. Real-PDF tests do not inherit this
    base and exercise PyMuPDF4LLM directly.
    """

    def setUp(self):
        markdown = patch("pdf_text.pymupdf4llm.to_markdown",
                         side_effect=_fake_to_markdown)
        markdown.start()
        self.addCleanup(markdown.stop)
        reopen = patch("pdf_text.pymupdf.open", side_effect=_fake_open_pdf)
        reopen.start()
        self.addCleanup(reopen.stop)

    def test_preparation_reports_every_page_including_cache_hits(self):
        with TemporaryDirectory() as tmp:
            doc = FakeDoc([FakePage("Source text " * 20), FakePage("More text " * 20)])
            progress = []
            first = PDFTextSource(doc, cache_dir=tmp, progress=lambda done,total: progress.append((done,total)))
            self.assertEqual(progress, [(1, 2), (2, 2)])
            first.close()
            progress.clear()
            second = PDFTextSource(doc, cache_dir=tmp, progress=lambda done,total: progress.append((done,total)))
            self.assertEqual(progress, [(1, 2), (2, 2)])
            self.assertEqual(second.extraction_hits, 2)
            second.close()

    def test_dense_vector_drawing_preserves_text_but_requires_manual_review(self):
        page = FakePage("Drawing labels " * 20)
        page.get_cdrawings = lambda: [None] * 5001
        with TemporaryDirectory() as tmp, patch("pdf_text.pymupdf4llm.to_markdown") as markdown:
            source = PDFTextSource(FakeDoc([page]), cache_dir=tmp)
            markdown.assert_not_called()
            self.assertIn(0, source._native_errors)
            self.assertIn("Drawing labels", source._native_texts[0])
            self.assertEqual(source._table_rows_for(0), ())
            self.assertEqual(page._table_calls, [])
            source.close()


def _insert_ruled_table(page, rows):
    """Draw a ruled grid with one text cell per column for real-PDF tests."""
    column_widths = [260, 80, 90]
    xs = [40]
    for width in column_widths:
        xs.append(xs[-1] + width)
    top, row_height = 50, 42
    bottom = top + row_height * len(rows)
    for x in xs:
        page.draw_line((x, top), (x, bottom))
    for row in range(len(rows) + 1):
        y = top + row * row_height
        page.draw_line((xs[0], y), (xs[-1], y))
    for r, cells in enumerate(rows):
        for c, value in enumerate(cells):
            # Match specification tables: short cells are centred alongside
            # wrapped descriptions, not aligned with only their first line.
            baseline = top + r * row_height + row_height / 2 + 3.5
            baseline -= (len(value.splitlines()) - 1) * 6.875
            page.insert_text((xs[c] + 10, baseline), value, fontsize=10)


def pending_of(index):
    return index.pending_ocr


class NativeIndexTests(FakeSourceTests):
    def test_no_eager_ocr_on_init(self):
        natives = ["настоящий текстовый слой страницы. " * 20 for _ in range(3)]
        natives[1] = "короткий скан"
        doc, calls = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
        self.assertEqual(calls, [])
        self.assertEqual(source.ocr_count, 0)
        self.assertEqual(pending_of(source.native_index), (1,))

    def test_native_pages_search_entire_index(self):
        natives = ["Кабель медный 45 метров. " * 20,
                   "Насосная станция общие данные. " * 20]
        doc, _ = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([0], "Кабель", "45")
        self.assertEqual(snapshot.candidates("Насосная станция", "", "", "2"), [1])

    def test_drawing_padding_does_not_displace_area_from_bounded_context(self):
        native = "\n".join(" " * 300 + line for line in (
            ["Укладка экрана Дорнит"] * 30 + ["Площадь крепления - 526м² (площадка КНС)."]))
        doc, _ = make_doc([native], images=False)
        with TemporaryDirectory() as cache:
            snapshot = PDFTextSource(doc, cache_dir=cache).for_position([0], "Укладка экрана Дорнит", "526")
        fragment = snapshot.fragment(0, "Укладка экрана Дорнит", "", "", "526", 1500)
        self.assertIn("Площадь крепления - 526м² (площадка КНС).", fragment)
        self.assertIn("Укладка экрана Дорнит", fragment)


class SelectionTests(FakeSourceTests):
    def test_selected_scans_max_4_per_position(self):
        natives = [""] * 10
        doc, calls = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([1, 3], "Кабель", "45", max_ocr_pages=4)
        self.assertEqual(source.ocr_count, 4)
        self.assertEqual(len(calls), 4)
        # Distant scans outside refs/neighbours/top-candidates stay untouched.
        self.assertNotIn(9, [c["idx"] for c in calls])
        self.assertEqual(len(pending_of(snapshot)), 10 - 4)

    def test_unprocessed_scans_remain_pending(self):
        natives = [""] * 6
        doc, _ = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([0], "Кабель", "45", max_ocr_pages=2)
            remaining = set(pending_of(snapshot))
        self.assertEqual(len(remaining), 4)
        for idx in remaining:
            self.assertEqual(snapshot.texts[idx], "")
            self.assertNotIn(idx, snapshot.errors)


class CachedRankingTests(FakeSourceTests):
    def test_cached_scan_ranks_without_current_reference(self):
        natives = [""] * 6
        station = "Насосная станция кабельный ввод 45 метров. " * 10
        cached_ocrs = ["распознанный текст %d" % i for i in range(6)]
        cached_ocrs[5] = station
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives, ocr_texts=cached_ocrs)
            PDFTextSource(doc1, cache_dir=cache).for_position(
                [5], "Насосная станция", "2", max_ocr_pages=4)
            doc2, _ = make_doc(natives, ocr_texts=[""] * 6)
            second = PDFTextSource(doc2, cache_dir=cache)
            snapshot = second.for_position(
                [0], "Насосная станция", "2", max_ocr_pages=4)
            self.assertIn("Насосная", snapshot.texts[5])
            self.assertIn(
                5, snapshot.candidates("Насосная станция", "", "", "2"))
            self.assertLessEqual(second.ocr_count, 4)


class DiscoveryTests(FakeSourceTests):
    def test_wrong_reference_uses_spare_allowance_in_page_order(self):
        natives = [""] * 10
        doc, calls = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([0], "Кабель", "45", max_ocr_pages=4)
        self.assertEqual(source.ocr_count, 4)
        self.assertEqual([c["idx"] for c in calls], [0, 1, 2, 3])
        self.assertEqual(len(pending_of(snapshot)), 6)
        self.assertNotIn(9, [c["idx"] for c in calls])


class TableTests(FakeSourceTests):
    def test_related_native_pages_keep_flattened_table_rows(self):
        natives = [("Кабель медный в смете. " * 20),
                   ("Насосная станция общие данные. " * 20)]
        table_calls = []
        doc, _ = make_doc(natives, table_calls=table_calls,
                           table_pages={0: [[["Кабель медный", "45", "м"]]]})
        with TemporaryDirectory() as cache:
            snapshot = PDFTextSource(doc, cache_dir=cache).for_position([0], "Кабель", "45")
        self.assertEqual(snapshot.table_rows[0], ("Кабель медный | 45 | м",))
        self.assertEqual(snapshot.table_rows[1], ())
        self.assertIn(0, table_calls)

    def test_table_extraction_is_bounded_and_reused(self):
        natives = ["Кабель медный позиция %d. " % idx * 20 for idx in range(12)]
        table_calls = []
        doc, _ = make_doc(natives, table_calls=table_calls,
                           table_pages={idx: [[["Кабель медный", "45", "м"]]] for idx in range(12)})
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            source.for_position([0], "Кабель", "45")
            first_pass = list(table_calls)
            source.for_position([0], "Кабель", "45")
        self.assertLessEqual(len(first_pass), 8)
        self.assertEqual(table_calls, first_pass)


class CacheTests(FakeSourceTests):
    def test_cached_ocr_reused_across_source_instances(self):
        natives = [""] * 6
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            first = PDFTextSource(doc1, cache_dir=cache)
            first.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(first.ocr_count, 2)
            doc2, calls2 = make_doc(natives)
            second = PDFTextSource(doc2, cache_dir=cache)
            snapshot = second.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(calls2, [])
            self.assertEqual(second.ocr_count, 0)
            self.assertGreater(second.cache_hits, 0)
            self.assertNotEqual(snapshot.texts[0], "")

    def test_changed_pdf_invalidates_cache(self):
        natives = [""] * 6
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives, payload=b"pdf-version-1")
            PDFTextSource(doc1, cache_dir=cache).for_position(
                [0], "Кабель", "45", max_ocr_pages=2)
            doc2, calls2 = make_doc(natives, payload=b"pdf-version-2")
            second = PDFTextSource(doc2, cache_dir=cache)
            second.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(len(calls2), 2)
            self.assertEqual(second.cache_hits, 0)

    def test_changed_settings_invalidate_cache(self):
        natives = [""] * 6
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            PDFTextSource(doc1, cache_dir=cache, dpi=200).for_position(
                [0], "Кабель", "45", max_ocr_pages=2)
            doc2, calls2 = make_doc(natives)
            second = PDFTextSource(doc2, cache_dir=cache, dpi=300)
            second.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(len(calls2), 2)
            self.assertEqual(second.cache_hits, 0)

    def test_cached_markdown_reused_without_extractor_calls(self):
        natives = [("Кабель медный позиция %d. " % idx) * 20 for idx in range(3)]
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            first = PDFTextSource(doc1, cache_dir=cache)
            self.assertEqual(first.extraction_hits, 0)
            self.assertEqual(first.extraction_misses, 3)
            doc2, _ = make_doc(natives)
            with patch(
                "pdf_text.pymupdf4llm.to_markdown",
                side_effect=AssertionError("must use cache"),
            ):
                second = PDFTextSource(doc2, cache_dir=cache)
            self.assertEqual(second.extraction_hits, 3)
            self.assertEqual(second.extraction_misses, 0)
            self.assertEqual(second.native_index.texts, first.native_index.texts)
            self.assertEqual(second.pending_ocr, first.pending_ocr)

    def test_changed_pdf_invalidates_extraction(self):
        natives = [("Кабель медный в смете. " * 20)] * 2
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives, payload=b"pdf-version-1")
            PDFTextSource(doc1, cache_dir=cache)
            doc2, _ = make_doc(natives, payload=b"pdf-version-2")
            second = PDFTextSource(doc2, cache_dir=cache)
            self.assertEqual(second.extraction_hits, 0)
            self.assertEqual(second.extraction_misses, 2)

    def test_changed_parser_invalidates_extraction_only(self):
        natives = [""] * 6
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            first = PDFTextSource(doc1, cache_dir=cache)
            first.for_position([0], "Кабель", "45", max_ocr_pages=2)
            with patch(
                "pdf_text._parser_fingerprint",
                return_value=("1.28.2", "9.9.9", "1.28.2"),
            ):
                doc2, calls2 = make_doc(natives)
                second = PDFTextSource(doc2, cache_dir=cache)
                snapshot = second.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(second.extraction_hits, 0)
            self.assertEqual(second.extraction_misses, 6)
            self.assertEqual(calls2, [])
            self.assertGreater(second.cache_hits, 0)
            self.assertNotEqual(snapshot.texts[0], "")

    def test_changed_ocr_settings_keep_markdown_but_invalidate_ocr(self):
        natives = [""] * 6
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            PDFTextSource(doc1, cache_dir=cache, dpi=200).for_position(
                [0], "Кабель", "45", max_ocr_pages=2)
            doc2, calls2 = make_doc(natives)
            second = PDFTextSource(doc2, cache_dir=cache, dpi=300)
            second.for_position([0], "Кабель", "45", max_ocr_pages=2)
            self.assertEqual(second.extraction_misses, 0)
            self.assertEqual(second.extraction_hits, 6)
            self.assertEqual(len(calls2), 2)
            self.assertEqual(second.cache_hits, 0)

    def test_corrupt_and_foreign_envelopes_are_recomputed(self):
        natives = [("Кабель медный позиция %d. " % idx) * 20 for idx in range(3)]
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives)
            first = PDFTextSource(doc1, cache_dir=cache)
            cache_root = Path(cache)
            envelopes = sorted(cache_root.rglob("page-*.json"))
            self.assertEqual(len(envelopes), 3)
            envelopes[0].write_bytes(b"{not-json")
            foreign = json.loads(envelopes[1].read_text(encoding="utf-8"))
            foreign["pdf_sha256"] = "0" * 64
            envelopes[1].write_text(json.dumps(foreign), encoding="utf-8")
            stale = json.loads(envelopes[2].read_text(encoding="utf-8"))
            stale["schema"] = CACHE_SCHEMA_VERSION + 1
            envelopes[2].write_text(json.dumps(stale), encoding="utf-8")
            doc2, _ = make_doc(natives)
            second = PDFTextSource(doc2, cache_dir=cache)
            self.assertEqual(second.extraction_hits, 0)
            self.assertEqual(second.extraction_misses, 3)
            self.assertEqual(second.native_index.texts, first.native_index.texts)

    def test_failed_markdown_is_explicit_and_not_cached(self):
        natives = [("Кабель медный в смете. " * 20), ("Насосная станция. " * 20)]
        doc, _ = make_doc(natives)
        with TemporaryDirectory() as cache:
            real_markdown = _fake_to_markdown

            def failing_markdown(markdown_doc, pages=None, **kwargs):
                if list(pages or [0])[0] == 1:
                    raise RuntimeError("broken page")
                return real_markdown(markdown_doc, pages=pages, **kwargs)

            with patch("pdf_text.pymupdf4llm.to_markdown",
                       side_effect=failing_markdown):
                source = PDFTextSource(doc, cache_dir=cache)
            self.assertEqual(source.native_index.texts[1], "")
            self.assertIn(1, source.native_index.errors)
            self.assertEqual(source.extraction_misses, 2)
            envelopes = {path.name for path in Path(cache).rglob("page-*.json")}
            self.assertEqual(envelopes, {"page-00000.json"})

    def test_cached_table_rows_reused_across_source_instances(self):
        natives = [("Кабель медный в смете. " * 20),
                   ("Насосная станция общие данные. " * 20)]
        tables = {0: [[["Кабель медный", "45", "м"]]]}
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives, table_pages=tables)
            first = PDFTextSource(doc1, cache_dir=cache)
            first.for_position([0], "Кабель", "45")
            self.assertEqual(first.table_cache_hits, 0)
            table_calls = []
            doc2, _ = make_doc(natives, table_calls=table_calls, table_pages=tables)
            second = PDFTextSource(doc2, cache_dir=cache)
            snapshot = second.for_position([0], "Кабель", "45")
            self.assertEqual(table_calls, [])
            self.assertGreater(second.table_cache_hits, 0)
            self.assertEqual(snapshot.table_rows[0], ("Кабель медный | 45 | м",))

    def test_changed_table_settings_invalidate_table_cache_only(self):
        natives = [("Кабель медный в смете. " * 20)]
        tables = {0: [[["Кабель медный", "45", "м"]]]}
        with TemporaryDirectory() as cache:
            doc1, _ = make_doc(natives, table_pages=tables)
            PDFTextSource(doc1, cache_dir=cache).for_position([0], "Кабель", "45")
            table_calls = []
            doc2, _ = make_doc(
                natives, table_calls=table_calls, table_pages=tables
            )
            with patch("pdf_text.MAX_TABLE_CELL_CHARS", 50):
                second = PDFTextSource(doc2, cache_dir=cache)
                snapshot = second.for_position([0], "Кабель", "45")
            self.assertEqual(second.extraction_hits, 1)
            self.assertEqual(second.extraction_misses, 0)
            self.assertEqual(second.table_cache_hits, 0)
            self.assertIn(0, table_calls)
            self.assertEqual(snapshot.table_rows[0], ("Кабель медный | 45 | м",))

    def test_failing_scans_explicit_and_not_cached(self):
        pages = [FakePage("", ocr_text="ok-%d" % i, calls=[]) for i in range(3)]
        pages[1] = FakePage("", fail=RuntimeError("Tesseract unavailable"),
                            calls=pages[0]._calls, idx=1)
        doc = FakeDoc(pages)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([1], "Кабель", "45", max_ocr_pages=4)
            self.assertIn(1, snapshot.errors)
            self.assertIn("Tesseract", snapshot.errors[1])
            self.assertEqual(list(Path(cache).rglob("page-00001.txt")), [])
            # OCR text merged with native text where present.
            self.assertIn("ok-", snapshot.texts[0] + snapshot.texts[2])


class ThreadingTests(FakeSourceTests):
    def test_native_and_ocr_access_main_thread_only(self):
        natives = ["текст. " * 60]
        doc, _ = make_doc(natives)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertRaises(
                    RuntimeError,
                    pool.submit(source.for_position, [0],
                                "Кабель", "45").result)


class BoundsTests(FakeSourceTests):
    def test_huge_drawing_never_renders_above_limits(self):
        max_pixels, max_side, dpi = 12000000, 4096, 200
        natives = [""] * 2
        doc, calls = make_doc(natives, rect=(100000, 100000))
        with TemporaryDirectory() as cache:
            PDFTextSource(doc, cache_dir=cache, dpi=dpi,
                          max_pixels=max_pixels,
                          max_side=max_side).for_position(
                              [0], "Кабель", "45", max_ocr_pages=2)
        self.assertTrue(calls)
        for call in calls:
            used = call["dpi"]
            self.assertLessEqual(used, dpi)
            width_px = 100000 * used / 72.0
            height_px = 100000 * used / 72.0
            self.assertLessEqual(width_px * height_px, max_pixels)
            self.assertLessEqual(max(width_px, height_px), max_side)


class NativeDetectionTests(FakeSourceTests):
    def test_good_short_native_page_not_forced_ocr(self):
        doc, calls = make_doc(["Кабель медный 45 метров. Коротко, но ясно."],
                              images=False)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            snapshot = source.for_position([0], "Кабель", "45", max_ocr_pages=4)
        self.assertEqual(pending_of(source.native_index), ())
        self.assertEqual(snapshot.pending_ocr, ())
        self.assertEqual(calls, [])
        self.assertEqual(source.ocr_count, 0)

    def test_outlined_drawings_page_needs_ocr_but_blank_stays_skipped(self):
        calls = []
        pages = [FakePage("", ocr_text="распознанный чертеж", calls=calls,
                          idx=0, images=False, drawings=120),
                 FakePage("", ocr_text="не должен распознаваться", calls=calls,
                          idx=1, images=False, drawings=0)]
        doc = FakeDoc(pages)
        with TemporaryDirectory() as cache:
            source = PDFTextSource(doc, cache_dir=cache)
            self.assertEqual(pending_of(source.native_index), (0,))
            snapshot = source.for_position([0], "Кабель", "45", max_ocr_pages=4)
        self.assertEqual([c["idx"] for c in calls], [0])
        self.assertEqual(snapshot.pending_ocr, ())


class RealPDFTests(unittest.TestCase):
    def test_short_native_page_does_not_need_ocr(self):
        import pymupdf
        with pymupdf.open() as doc, TemporaryDirectory() as cache:
            page = doc.new_page()
            page.insert_text((50, 50), "Cable 45 m")
            source = PDFTextSource(doc, cache_dir=cache)
            index = source.for_position([0], "Cable", "45")
            self.assertEqual(index.pending_ocr, ())
            self.assertEqual(source.ocr_count, 0)
            self.assertIn("Cable 45 m", index.texts[0])

    def test_html_table_preserves_multiline_work_quantity_and_unit(self):
        import pymupdf
        with pymupdf.open() as doc:
            page = doc.new_page()
            _insert_ruled_table(page, [["Work", "Quantity", "Unit"],
                                       ["Copper cable\ninstallation", "45", "m"],
                                       ["Steel pipe", "10", "pcs"]])
            with TemporaryDirectory() as cache:
                snapshot = PDFTextSource(doc, cache_dir=cache).for_position(
                    [0], "Copper cable", "45")

        def has_work_row(text):
            for row in html.fromstring(text).xpath(".//tr"):
                cells = [" ".join(cell.itertext()).strip() for cell in row]
                if cells == ["Copper cable installation", "45", "m"]:
                    return True
            return False

        self.assertTrue(has_work_row(snapshot.texts[0]), snapshot.texts[0])
        fragment = snapshot.fragment(0, "Copper cable", "", "", "45", 2500)
        self.assertTrue(has_work_row(fragment), fragment)

    def test_two_page_fragments_validate_exact_quotes_and_reject_fabrication(self):
        import pymupdf
        with pymupdf.open() as doc:
            first = doc.new_page()
            first.insert_textbox(pymupdf.Rect(40, 40, 550, 300),
                                 "Copper cable installation scope of work. " * 8,
                                 fontsize=11)
            second = doc.new_page()
            _insert_ruled_table(second, [["Work", "Quantity", "Unit"],
                                         ["Copper cable", "45", "m"],
                                         ["Steel pipe", "10", "pcs"]])
            with TemporaryDirectory() as cache:
                snapshot = PDFTextSource(doc, cache_dir=cache).for_position(
                    [0], "Copper cable", "45")
                context = {
                    1: snapshot.fragment(0, "Copper cable", "", "", "45", 2500),
                    2: snapshot.fragment(1, "Copper cable", "", "", "45", 2500),
                }
        work_line = next(line for line in context[1].splitlines()
                         if "Copper cable" in line)
        row_line = next(row for row in re.findall(r"<tr\b[^>]*>.*?</tr>", context[2], re.DOTALL)
                        if "Copper cable" in row and ">45<" in row)
        data = {
            "work_found": True,
            "volume_confirmed": True,
            "unit_confirmed": True,
            "comment_confirmed": False,
            "found_volume": "45",
            "found_unit": "m",
            "found_page": "2",
            "evidence": [
                {"page": 1, "quote": work_line, "kind": "work"},
                {"page": 2, "quote": row_line, "kind": "work"},
                {"page": 2, "quote": row_line, "kind": "volume"},
                {"page": 2, "quote": row_line, "kind": "unit"},
            ],
            "discrepancy_details": "Confirmed",
        }
        result = parse_audit_response(json.dumps(data), context,
                                      comment_required=False)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_page"], "2")
        self.assertEqual(result["found_pages"], [1, 2])
        fabricated = json.loads(json.dumps(data))
        fabricated["found_volume"] = "999"
        fabricated["evidence"][2] = {"page": 2, "quote": "Copper cable 999 m",
                                     "kind": "volume"}
        with self.assertRaises(QuoteValidationError):
            parse_audit_response(json.dumps(fabricated), context,
                                 comment_required=False)

    def test_rotated_source_document_is_never_mutated(self):
        import pymupdf
        with pymupdf.open() as build:
            page = build.new_page()
            page.insert_text((50, 50), "Cable 45 m")
            page.set_rotation(90)
            pdf_bytes = build.tobytes()
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
            pristine = doc.tobytes(no_new_id=True)
            self.assertEqual(doc[0].rotation, 90)
            self.assertFalse(doc.is_dirty)
            with TemporaryDirectory() as cache:
                source = PDFTextSource(doc, cache_dir=cache)
                self.assertEqual(doc[0].rotation, 90)
                self.assertFalse(doc.is_dirty)
                self.assertEqual(doc.tobytes(no_new_id=True), pristine)
                source.for_position([0], "Cable", "45")
            self.assertEqual(doc[0].rotation, 90)
            self.assertEqual(doc.tobytes(no_new_id=True), pristine)

    @unittest.skipUnless(os.environ.get("TESSDATA_PREFIX"), "Set TESSDATA_PREFIX for real OCR")
    def test_real_scan_cache_survives_pdf_reopen(self):
        import pymupdf
        with pymupdf.open() as native:
            page = native.new_page()
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 700), "Copper cable 45 meters. " * 12, fontsize=18)
            image = page.get_pixmap(dpi=150).tobytes("png")
        with pymupdf.open() as scan:
            page = scan.new_page()
            page.insert_image(page.rect, stream=image)
            pdf_bytes = scan.tobytes()
        with TemporaryDirectory() as cache:
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                first = PDFTextSource(doc, cache_dir=cache)
                index = first.for_position([0], "Copper cable", "45")
                self.assertEqual(first.ocr_count, 1)
                self.assertIn("copper cable", index.texts[0].lower())
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                second = PDFTextSource(doc, cache_dir=cache)
                cached = second.for_position([0], "Copper cable", "45", max_ocr_pages=0)
                self.assertEqual(second.ocr_count, 0)
                self.assertEqual(second.cache_hits, 1)
                self.assertEqual(cached.texts, index.texts)
                self.assertEqual(cached.pending_ocr, ())


class ClosedDocumentTests(FakeSourceTests):
    def test_closed_document_is_rejected_before_indexing(self):
        doc, _ = make_doc(["Copper cable 45 m. " * 20])
        doc.is_closed = True
        with TemporaryDirectory() as cache:
            with self.assertRaisesRegex(ValueError, "закрыт"):
                PDFTextSource(doc, cache_dir=cache)


class PrivateOwnershipTests(unittest.TestCase):
    def test_snapshot_and_tables_survive_original_document_close(self):
        import pymupdf
        with TemporaryDirectory() as cache:
            doc = pymupdf.open()
            source = None
            try:
                page = doc.new_page()
                page.insert_textbox(pymupdf.Rect(40, 40, 550, 300),
                                    "Copper cable installation. " * 10)
                page = doc.new_page()
                _insert_ruled_table(page, [["Work", "Quantity", "Unit"],
                                           ["Cable", "45", "m"]])
                source = PDFTextSource(doc, cache_dir=cache)
                self.assertIsNot(source._doc, doc)
                doc.close()
                snapshot = source.for_position([0, 1], "Copper cable", "45")
                self.assertIn("Copper cable installation", snapshot.texts[0])
                self.assertIn("Cable | 45 | m", snapshot.table_rows[1])
                source.close()
                source.close()
                self.assertTrue(source._doc.is_closed)
                self.assertIn("Copper cable installation", snapshot.texts[0])
            finally:
                if source is not None:
                    source.close()
                if not doc.is_closed:
                    doc.close()


if __name__ == "__main__":
    unittest.main()
