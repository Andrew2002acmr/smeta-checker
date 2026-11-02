"""Focused OCR quality/geometry tests for the bounded Tesseract TSV path."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ocr_engine
from pdf_text import PDFTextSource


def test_tsv_literal_quote_does_not_swallow_following_rows():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    rows = '5\t1\t1\t1\t1\t1\t10\t20\t50\t10\t95\t"Мост\n5\t1\t2\t1\t1\t1\t100\t220\t40\t12\t98\t345,77\n'
    words = ocr_engine.parse_tsv(header+rows)
    assert [w["text"] for w in words] == ['"Мост', '345,77']
    assert words[1]["top"] == 220


class OrientationTests(unittest.TestCase):
    def test_rotation_choice_ignores_expected_values(self):
        filler = ("наполнение " * 40).strip()
        expected_like = ocr_engine.score_orientation([{"text": "631,2", "conf": 95}])
        neutral = ocr_engine.score_orientation([{"text": filler, "conf": 70}])
        self.assertGreater(neutral, expected_like)

    def test_tie_break_prefers_lower_rotation(self):
        words = [{"text": "кабель", "conf": 80}]
        scores = [(ocr_engine.score_orientation(words), index, angle)
                  for index, angle in enumerate(ocr_engine.ORIENTATIONS)]
        self.assertEqual(min(scores)[2], 0)


class GeometryTests(unittest.TestCase):
    def test_unrotated_box_is_identity(self):
        box = (10.0, 20.0, 60.0, 44.0)
        self.assertEqual(tuple(ocr_engine._unrotate_box(box, 0, (100, 200))), box)

    def test_rotate_sizes_swap_on_quarter_turns(self):
        image = ocr_engine.Image.new("RGB", (100, 200), "white")
        self.assertEqual(tuple(ocr_engine._rotate(image, 0).size), (100, 200))
        self.assertEqual(tuple(ocr_engine._rotate(image, 90).size), (200, 100))
        self.assertEqual(tuple(ocr_engine._rotate(image, 180).size), (100, 200))
        self.assertEqual(tuple(ocr_engine._rotate(image, 270).size), (200, 100))

    def test_quadrants_overlap(self):
        boxes = ocr_engine.quadrant_boxes(1000, 800)
        self.assertEqual([name for name, _ in boxes], ["q1", "q2", "q3", "q4"])
        self.assertGreater(boxes[0][1][2], 500)
        self.assertLess(boxes[1][1][0], 500)

    def test_dedup_keeps_best_confidence(self):
        words = [
            {"text": "Кабель", "conf": 60, "bbox": [1, 1, 10, 5]},
            {"text": "кабель", "conf": 90, "bbox": [1, 1, 10, 5]},
        ]
        kept = ocr_engine.deduplicate_words(words)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["conf"], 90)

    def test_numeric_gate_above_label_gate(self):
        self.assertEqual(ocr_engine.gate_for("value"), 80)
        self.assertEqual(ocr_engine.gate_for("label"), 60)

    def test_compound_numeric_unit_gates_at_80(self):
        for token in ("500г/м2", "45м", "0,34м3", "25/25кН/м"):
            self.assertEqual(ocr_engine.measurement_kind(token), "value", token)
        self.assertEqual(ocr_engine.measurement_kind("геотекстиль"), "label")

    def test_rotated_page_boxes_land_inside_unrotated_rect(self):
        # Deterministic rotated-page mapping: the pixmap lives in the
        # rotated display frame (842x595); derotation must bring boxes
        # into the unrotated page frame (595x842), not leave them in the
        # display frame.
        rect = FakeRect(0, 0, 842, 595)
        page = FakeRotatedPage(rect, rotation=90)
        word = {"text": "631,2", "conf": 95, "left": 100, "top": 50,
                "width": 60, "height": 16}
        bbox, _ = ocr_engine._word_to_pdf(
            word, 0, (842, 595), None, (842, 595), rect, page.derotation)
        self.assertTrue(0 <= bbox[0] <= bbox[2] <= 595)
        self.assertTrue(0 <= bbox[1] <= bbox[3] <= 842)
        # Without derotation the box stays in the display frame: same
        # numbers, wrong coordinate system.
        raw, _ = ocr_engine._word_to_pdf(
            word, 0, (842, 595), None, (842, 595), rect, None)
        self.assertEqual(raw, [100.0, 50.0, 160.0, 66.0])
        self.assertNotEqual(list(bbox), raw)

    def test_transposed_row_banding_keeps_columns_apart(self):
        # Two text columns read at 90°: TSV-frame banding must not merge
        # the columns into one row.
        words = [
            {"text": "Марка", "conf": 95, "bbox": [10, 10, 60, 20],
             "_tbox": [10, 10, 60, 100], "_tx": 10, "_ty": 10,
             "_tx1": 60, "_txc": 35},
            {"text": "Т-500", "conf": 95, "bbox": [10, 30, 60, 40],
             "_tbox": [10, 120, 60, 210], "_tx": 10, "_ty": 120,
             "_tx1": 60, "_txc": 35},
            {"text": "Прочность", "conf": 95, "bbox": [200, 10, 280, 20],
             "_tbox": [200, 10, 280, 100], "_tx": 200, "_ty": 10,
             "_tx1": 280, "_txc": 240},
            {"text": "25", "conf": 95, "bbox": [200, 30, 280, 40],
             "_tbox": [200, 120, 280, 210], "_tx": 200, "_ty": 120,
             "_tx1": 280, "_txc": 240},
        ]
        rows = ocr_engine._row_bands(words, 90)
        self.assertEqual(len(rows), 2)
        self.assertEqual([word["text"] for word in rows[0]["words"]],
                         ["Марка", "Прочность"])

    def test_repeated_column_strips_isolated_and_single_line_not(self):
        def word(text, row, x0, conf=95, kind="value"):
            return {"text": text, "conf": conf, "kind": kind,
                    "bbox": [x0, 10.0 + row * 20, x0 + 30, 20.0 + row * 20],
                    "row_id": "r%d" % row,
                    "_tx": x0, "_tx1": x0 + 30, "_txc": x0 + 15}
        grid = [word("Марка", 1, 10, kind="label"),
                word("25", 1, 200), word("Т-500", 2, 10, kind="label"),
                word("30", 2, 200), word("П-100", 3, 10, kind="label"),
                word("35", 3, 200)]
        for item in grid:
            item["_tbox"] = item["bbox"]
            item["_ty"] = item["bbox"][1]
        strips = ocr_engine._column_strips(grid, 0)
        self.assertEqual(len(strips), 2)
        single = [word("Всего", 1, 10, kind="label"), word("45", 1, 300)]
        for item in single:
            item["_tbox"] = item["bbox"]
            item["_ty"] = item["bbox"][1]
        self.assertEqual(ocr_engine._column_strips(single, 0), [])


class FakeRect:
    def __init__(self, x0=0, y0=0, x1=595, y1=842):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def width(self):
        return self.x1 - self.x0

    @property
    def height(self):
        return self.y1 - self.y0


class FakeRotatedPage:
    def __init__(self, rect, rotation=90):
        self.rect = rect
        self.rotation = rotation
        # PyMuPDF derotation for a 90° page over an 842×595 display frame:
        # display (x, y) -> unrotated (y, H - x).
        self.derotation = (0.0, -1.0, 1.0, 0.0, 0.0, 842.0)

    def get_pixmap(self, dpi=200, alpha=False):
        class Pix:
            width, height, n = 842, 595, 3
            samples = bytes(842 * 595 * 3)
        return Pix()


class FakePixmapPage:
    rect = FakeRect()
    rotation = 0
    derotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def get_pixmap(self, dpi=200, alpha=False):
        class Pix:
            width, height, n = 100, 140, 3
            samples = bytes(100 * 140 * 3)
        return Pix()


def _word_tsv(words):
    header = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
              "left\ttop\twidth\theight\tconf\ttext")
    lines = [header]
    for index, (text, conf, left) in enumerate(words, start=1):
        lines.append("5\t1\t1\t1\t1\t%d\t%d\t10\t40\t10\t%d\t%s" % (
            index, left, conf, text))
    return "\n".join(lines) + "\n"


class EngineFailureTests(unittest.TestCase):
    def setUp(self):
        # These cases mock run_tsv and test quality accounting, not an installed
        # executable. Keep them portable to Windows without native Tesseract.
        binary = patch.object(ocr_engine.shutil, "which", return_value="synthetic-tesseract")
        binary.start()
        self.addCleanup(binary.stop)

    def test_failed_passes_record_reasons_not_text(self):
        with patch.object(ocr_engine, "run_tsv", side_effect=RuntimeError("boom")):
            result = ocr_engine.recognize_page(FakePixmapPage())
        self.assertEqual(result["text"], "")
        self.assertEqual(result["quality"]["state"], "failed")
        self.assertTrue(result["quality"]["reasons"])

    def test_low_confidence_words_dropped_and_counted(self):
        tsv = _word_tsv([("хорошо", 90, 5), ("плохо", 10, 50)])
        with patch.object(ocr_engine, "run_tsv", return_value=tsv):
            result = ocr_engine.recognize_page(FakePixmapPage())
        self.assertIn("хорошо", result["text"])
        self.assertNotIn("плохо", result["text"])
        self.assertGreaterEqual(result["quality"]["words_dropped_low_conf"], 1)

    def test_dropped_counter_counts_final_words_only(self):
        overview = _word_tsv([("хорошо", 90, 5), ("плохо", 10, 50)])
        empty = _word_tsv([])

        def by_psm(png, *args, **kwargs):
            # run_tsv(png, language, psm, timeout): overview PSM 11 keeps
            # both words, quadrant PSM 6 passes add nothing new.
            psm = args[1] if len(args) > 1 else kwargs.get("psm", 11)
            return overview if int(psm) == 11 else empty

        with patch.object(ocr_engine, "run_tsv", side_effect=by_psm):
            result = ocr_engine.recognize_page(FakePixmapPage())
        self.assertEqual(result["quality"]["words_dropped_low_conf"], 1)
        self.assertEqual(result["quality"]["words_kept"], 1)


class CacheIdentityTests(unittest.TestCase):
    def test_identity_includes_full_settings_and_engine(self):
        from test_pdf_text import FakeDoc, FakePage, _fake_open_pdf
        pages = [FakePage("короткий скан", calls=[], idx=0)]
        doc = FakeDoc(pages, payload=b"ocr-identity")
        with TemporaryDirectory() as cache, \
                patch("pdf_text.pymupdf4llm.to_markdown",
                      side_effect=lambda d, pages=None, **k: [{
                          "text": d[list(pages or [0])[0]].get_text(),
                          "metadata": {}, "toc_items": [], "page_boxes": []}]), \
                patch("pdf_text.pymupdf.open", side_effect=_fake_open_pdf):
            source = PDFTextSource(doc, cache_dir=cache)
            identity = source._ocr_settings_identity()
        self.assertEqual(identity["format"], ocr_engine.CACHE_FORMAT)
        self.assertIn("tesseract", identity)
        self.assertIn("engine", identity)
        self.assertIn("orientations", identity)


class GlueLineTests(unittest.TestCase):
    def test_date_fragments_join_pk_keeps_space(self):
        def w(t, x0, x1):
            return {"text": t, "bbox": [x0, 100, x1, 112]}

        self.assertEqual(
            ocr_engine._glue_line([w("22.03.20", 0, 40), w("24", 41, 55)]),
            "22.03.2024")
        self.assertEqual(
            ocr_engine._glue_line([w("ПК", 0, 20), w("5+90,00", 21, 70)]),
            "ПК 5+90,00")
        self.assertEqual(
            ocr_engine._glue_line([w("113,36", 0, 40), w("м", 60, 70)]),
            "113,36 м")

    def test_enhance_upscales_small_crops_only(self):
        from PIL import Image

        small = Image.new("RGB", (400, 300), "white")
        big = Image.new("RGB", (2000, 1500), "white")
        self.assertEqual(ocr_engine._enhance_crop(small).size, (800, 600))
        self.assertEqual(ocr_engine._enhance_crop(big).size, (2000, 1500))


if __name__ == "__main__":
    unittest.main()
