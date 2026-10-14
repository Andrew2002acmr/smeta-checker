"""Regression checks run without Streamlit or live API credentials."""

import ast
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import (PageTextIndex, TECHNICAL_ERROR, audit_text_pages,
                        page_chunks, parse_audit_response, parse_page_reference, technical_error)


def response(page="-", status="Ошибка", volume="-", details="Не найдено"):
    return {"status": status, "found_page": page, "found_volume": volume,
            "discrepancy_details": details}


def app_functions(**extra):
    """Load production helpers without executing the Streamlit app at import time."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
    names = {"prepare_pdf_text_index", "audit_position_hybrid", "worker_check_row"}
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"PageTextIndex": PageTextIndex, "TECHNICAL_ERROR": TECHNICAL_ERROR,
                 "audit_text_pages": audit_text_pages, "parse_audit_response": parse_audit_response,
                 "technical_error": technical_error, "time": SimpleNamespace(sleep=lambda _: None),
                 "YANDEX_API_KEY": "test", "YANDEX_FOLDER": "test", "AGNES_API_KEY": "test",
                 "AGNES_BASE_URL": "https://example.invalid", **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), namespace)
    return namespace


class PageReferenceTests(unittest.TestCase):
    def test_excel_float(self):
        self.assertEqual(parse_page_reference(9.0, 20)[0], [8])

    def test_ranges_and_duplicates(self):
        self.assertEqual(parse_page_reference("9–12 10; 14", 20)[2], [9, 10, 11, 12, 14])

    def test_invalid_part_preserves_warning(self):
        indices, description, pages = parse_page_reference("9 99", 20)
        self.assertEqual(indices, [8])
        self.assertIn("недопустимые", description)

    def test_rejects_bad_or_huge_ranges(self):
        for value in ("9.5", "текст 9", "12-9", "0", "1-9999999999999", None, float("nan")):
            with self.subTest(value=value):
                self.assertEqual(parse_page_reference(value, 20)[0], [])


class ResponseTests(unittest.TestCase):
    def test_latin_and_cyrillic_ok_are_equivalent(self):
        for status in ("OK", "ОК", "OК", "ОK", " ok "):
            with self.subTest(status=status):
                result = parse_audit_response(json.dumps(response("3", status, "45 м")), {3})
                self.assertEqual(result["status"], "ОК")

    def test_unknown_status_still_rejected(self):
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(response("3", "success", "45 м")), {3})

    def test_null_details_and_volume_do_not_erase_page(self):
        data = response(14, volume=None, details=None)
        result = parse_audit_response(json.dumps(data), {14})
        self.assertEqual(result["found_page"], "14")
        self.assertEqual(result["discrepancy_details"], "")

    def test_thinking_and_markdown(self):
        raw = '<think>reason</think>```json\n' + json.dumps(response("3", "ОК", "10 м")) + '\n```'
        self.assertEqual(parse_audit_response(raw, {3})["found_page"], "3")

    def test_rejects_hallucinated_page(self):
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(response("99")), {3})

    def test_rejects_invalid_shapes(self):
        for raw in ("", "no json", '{"status":"Ошибка"}', json.dumps(response(True)),
                    json.dumps(response("3", details=[])), json.dumps(response("-", "ОК")),
                    json.dumps(response("3 и 4")), '{"status":"Ошибка","found_page":"3", broken}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_audit_response(raw, {3})


class SearchTests(unittest.TestCase):
    def test_finds_page_outside_wrong_reference(self):
        index = PageTextIndex(["Титульный лист", "Оглавление", "Кабель медный 45 м"])
        called = []
        def ask(page, text):
            called.append(page)
            return response(str(page), "ОК", "45 м", "Подтверждено") if "Кабель" in text else response()
        result = audit_text_pages(index, [0], ask, "Кабель медный", "45")
        self.assertEqual(result["found_page"], "3")
        self.assertEqual(result["status"], "Ошибка")
        self.assertEqual(called[0], 1)

    def test_correct_reference_does_not_call_extra_pages(self):
        index = PageTextIndex(["Кабель", "Кабель"])
        calls = []
        def ask(page, text):
            calls.append(page)
            return response(str(page), "ОК", "45 м", "Подтверждено")
        result = audit_text_pages(index, [0], ask, "Кабель", "45")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(calls, [1])

    def test_volume_mismatch_preserves_actual_page(self):
        index = PageTextIndex(["Кабель 40"])
        result = audit_text_pages(index, [0], lambda p, t: response("1", volume="40 м", details="Отсутствует подтверждение единицы"), "Кабель", "45")
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["status"], "Ошибка")

    def test_checks_end_of_long_page(self):
        text = "Введение " * 1800 + "Кабель 45 м"
        seen = []
        def ask(page, fragment):
            seen.append(fragment)
            return response("1", "ОК", "45 м") if "Кабель 45" in fragment else response()
        result = audit_text_pages(PageTextIndex([text]), [0], ask, "Кабель", "45")
        self.assertEqual(result["found_page"], "1")
        self.assertGreater(len(seen), 2)

    def test_neighbouring_table_continuation(self):
        index = PageTextIndex(["Иная работа", "45 м", "Другой том"])
        result = audit_text_pages(index, [0], lambda p, t: response(str(p), volume="45 м") if p == 2 else response(), "Кабель", "45")
        self.assertEqual(result["found_page"], "2")

    def test_missing_reference_can_search(self):
        index = PageTextIndex(["Введение", "Кабель 45 м"])
        result = audit_text_pages(index, [], lambda p, t: response(str(p), "ОК", "45 м"), "Кабель", "45")
        self.assertEqual(result["found_page"], "2")
        self.assertEqual(result["status"], "Ошибка")

    def test_scan_error_is_technical_failure(self):
        index = PageTextIndex([""], {0: "Нужен OCR"})
        result = audit_text_pages(index, [0], lambda p, t: self.fail("API should not run"), "Кабель", "45")
        self.assertEqual(result["status"], TECHNICAL_ERROR)

    def test_api_failure_does_not_trigger_more_pages(self):
        calls = []
        def ask(page, text):
            calls.append(page)
            return technical_error("429")
        result = audit_text_pages(PageTextIndex(["Кабель"] * 10), [0], ask, "Кабель", "45")
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(calls, [1])

    def test_unreadable_fallback_page_is_reported(self):
        index = PageTextIndex(["Введение", ""], {1: "Нужен OCR"})
        result = audit_text_pages(index, [0], lambda p, t: response(), "Кабель", "45")
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertIn("OCR", result["discrepancy_details"])

    def test_search_covers_all_remaining_pages(self):
        index = PageTextIndex(["Кабель"] * 10 + ["Другое наименование"] * 10)
        candidates = index.candidates("Кабель", "", "", "45", [0])
        self.assertEqual(len(candidates), 19)
        self.assertEqual(set(candidates), set(range(1, 20)))
        self.assertEqual(candidates[0], 1)

    def test_not_found_checks_entire_document(self):
        called = []
        def ask(page, text):
            called.append(page)
            return response()
        result = audit_text_pages(PageTextIndex(["Введение"] * 20), [0], ask, "Кабель", "45")
        self.assertEqual(set(called), set(range(1, 21)))
        self.assertIn("всего PDF", result["discrepancy_details"])

    def test_finds_far_page_without_matching_name(self):
        index = PageTextIndex(["Кабель проектный"] * 15 + ["Проводник медный 45 м"])
        result = audit_text_pages(index, [0], lambda p, t: response(str(p), "ОК", "45 м") if p == 16 else response(), "Кабель", "45")
        self.assertEqual(result["found_page"], "16")
        self.assertEqual(result["status"], "Ошибка")

    def test_wrong_volume_on_reference_continues_search(self):
        called = []
        def ask(page, text):
            called.append(page)
            return response("1", volume="40 м", details="Объем отличается") if page == 1 else response("2", "ОК", "45 м")
        result = audit_text_pages(PageTextIndex(["Кабель 40 м", "Кабель 45 м"]), [0], ask, "Кабель", "45")
        self.assertEqual(called, [1, 2])
        self.assertEqual(result["found_page"], "2")
        self.assertEqual(result["found_volume"], "45 м")
        self.assertEqual(result["status"], "Ошибка")

    def test_mismatch_on_candidate_does_not_stop_search(self):
        def ask(page, text):
            if page == 1:
                return response()
            if page == 2:
                return response("2", volume="40 м", details="Объем отличается")
            return response("3", "ОК", "45 м")
        result = audit_text_pages(PageTextIndex(["Введение", "Кабель 40 м", "Проводник 45 м"]), [0], ask, "Кабель", "45")
        self.assertEqual(result["found_page"], "3")

    def test_api_failure_keeps_already_found_location(self):
        def ask(page, text):
            return response("1", volume="40 м") if page == 1 else technical_error("API unavailable")
        result = audit_text_pages(PageTextIndex(["Кабель 40 м", "Кабель 45 м"]), [0], ask, "Кабель", "45")
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["found_page"], "1")


class FakeAPIError(Exception):
    status_code = 429


class AdapterTests(unittest.TestCase):
    def run_adapter(self, outputs, provider="Agnes AI (Agnes 3.0)"):
        calls, clients = [], []
        class FakeClient:
            def __init__(self, **kwargs):
                clients.append(self)
                self.closed = False
                self.responses = SimpleNamespace(create=self.create)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            def create(self, **kwargs):
                calls.append(kwargs)
                output = outputs.pop(0)
                if isinstance(output, Exception):
                    raise output
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output))], output_text=output)
            def close(self):
                self.closed = True
        namespace = app_functions(openai=SimpleNamespace(OpenAI=FakeClient, APIConnectionError=ConnectionError))
        result = namespace['audit_position_hybrid'](provider, None, "test-model", PageTextIndex(["Кабель 45 м"]), [0], "Кабель", "45", "м", "", "1")
        self.assertTrue(clients[0].closed)
        return result, calls

    def test_retries_malformed_response(self):
        result, calls = self.run_adapter(["broken", json.dumps(response("1", "ОК", "45 м"))])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(len(calls), 2)

    def test_retries_rate_limit(self):
        result, calls = self.run_adapter([FakeAPIError("quota"), json.dumps(response("1", "ОК", "45 м"))])
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(len(calls), 2)

    def test_exhausted_retries_are_technical_error(self):
        result, calls = self.run_adapter(["broken"] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(len(calls), 3)

    def test_yandex_adapter(self):
        result, calls = self.run_adapter([json.dumps(response("1", "ОК", "45 м"))], "Yandex Cloud (Qwen)")
        self.assertEqual(result["status"], "ОК")
        self.assertIn("Кабель 45", calls[0]["input"])

    def test_worker_preserves_page_when_unit_absent(self):
        namespace = app_functions(provider="test", client=None, model_name="test", candidate_limit=8)
        namespace["audit_position_hybrid"] = lambda **kwargs: response("1", volume="-", details="Отсутствует единица")
        task = {"idx": 0, "text_index": PageTextIndex(["Кабель"]), "target_indices": [0],
                "primary_phys_pages": [1], "target_pdf_name": "test.pdf", "w_name": "Кабель",
                "w_vol": "45", "w_unit": "м", "w_ref": "", "w_page": "1", "w_comment": "",
                "w_formula": "", "loc_desc": "Стр. 1"}
        _, result, track = namespace["worker_check_row"](task)
        self.assertEqual(result["Фактическая страница в ПД (найдено моделью)"], "Стр. 1")
        self.assertEqual(track["found_page"], 1)


class PDFExtractionTests(unittest.TestCase):
    def test_pdf_is_read_only_in_main_thread(self):
        class Page:
            def get_text(self, **kwargs):
                self_check.assertEqual(threading.current_thread(), threading.main_thread())
                return "Кабель медный 45 метров. " * 20
        self_check = self
        namespace = app_functions()
        index = namespace["prepare_pdf_text_index"]([Page(), Page()])
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: audit_text_pages(index, [0], lambda p, t: response("1", "ОК", "45 м"), "Кабель", "45"), range(2)))
        self.assertTrue(all(result["status"] == "ОК" for result in results))

    def test_ocr_and_read_errors_do_not_abort_index(self):
        class Scan:
            def get_text(self, **kwargs):
                return ""
            def get_textpage_ocr(self, **kwargs):
                raise RuntimeError("Tesseract unavailable")
        class Broken:
            def get_text(self, **kwargs):
                raise RuntimeError("Damaged page")
        index = app_functions()["prepare_pdf_text_index"]([Scan(), Broken()])
        self.assertEqual(len(index.texts), 2)
        self.assertEqual(len(index.errors), 2)

    def test_actual_pdf_roundtrip(self):
        import fitz
        with fitz.open() as doc:
            for content in ("Introduction. " * 20, "Copper cable 45 meters. " * 20):
                page = doc.new_page()
                page.insert_textbox(fitz.Rect(50, 50, 500, 500), content)
            pdf = doc.tobytes()
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            index = app_functions()["prepare_pdf_text_index"](doc)
            self.assertEqual(index.errors, {})
            self.assertIn("Copper cable", index.texts[1])
        result = audit_text_pages(index, [0], lambda p, t: response(str(p), "ОК", "45 m") if "Copper cable" in t else response(), "Copper cable", "45")
        self.assertEqual(result["found_page"], "2")

    @unittest.skipUnless(os.environ.get("TESSDATA_PREFIX"), "Set TESSDATA_PREFIX to test actual OCR")
    def test_actual_scanned_pdf_ocr(self):
        import fitz
        with fitz.open() as source:
            page = source.new_page()
            page.insert_textbox(fitz.Rect(50, 50, 500, 600), "Copper cable 45 meters. " * 20, fontsize=14)
            image = page.get_pixmap(dpi=200).tobytes("png")
        with fitz.open() as scan:
            page = scan.new_page()
            page.insert_image(page.rect, stream=image)
            self.assertEqual(page.get_text().strip(), "")
            index = app_functions()["prepare_pdf_text_index"](scan)
            self.assertEqual(index.errors, {})
            self.assertIn("copper cable", index.texts[0].lower())


if __name__ == "__main__":
    unittest.main()
