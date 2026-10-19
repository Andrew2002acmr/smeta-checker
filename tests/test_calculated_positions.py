"""Operation, technology and material quantity must not be conflated."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import INCOMPLETE, PageTextIndex, audit_text_pages, parse_audit_response
from test_audit import response, proof
from audit_llm import _catalog_chunks


class OperationEvidenceTests(unittest.TestCase):
    POSITION = ("Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль\n1879 м2, толщина плит 150 мм")
    def setUp(self):
        self.context = {1: "Монтаж плит с соединением отдельных плит талрепными стяжками.",
                        5: "Спецификация. Наименование и техническая характеристика. Поставщик. Количество. Зажим 328 шт."}
        self.data = response("1", "328", "шт.", True, True, evidence=[
            proof(1, self.context[1]), proof(5, "Зажим 328 шт.", "volume"),
            proof(5, "328 шт.", "unit"), proof(1, self.context[1], "technology")], details="")
        self.data["technology"] = {"status": "confirmed",
            "expected": "соединением", "actual": "соединением отдельных плит"}

    def parse(self, data=None):
        return parse_audit_response(json.dumps(data or self.data), self.context, False,
                                    position_type="Работа",
                                    expected_work="Скрепление плит соединением талрепными; обжимными втулками")

    def test_operation_location_is_not_the_material_quantity_page(self):
        result = self.parse()
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1, 5])
        self.assertEqual(result["quantity_basis"], "direct")

    def test_material_specification_cannot_be_the_only_work_proof(self):
        data = {**self.data, "found_page": "5", "evidence": [
            proof(5, self.context[5]), proof(5, "Зажим 328 шт.", "volume"),
            proof(5, "328 шт.", "unit"), proof(1, self.context[1], "technology")]}
        with self.assertRaises(ValueError):
            self.parse(data)
        data["technology"] = {"status": "not_applicable", "expected": "", "actual": ""}
        material = parse_audit_response(json.dumps(data), self.context, False,
                                        position_type="Материал")
        self.assertEqual(material["status"], "ОК")

    def test_different_technology_preserves_work_and_quantity(self):
        data = {**self.data, "technology": {"status": "different",
                    "expected": "обжимными втулками", "actual": "талрепными стяжками"},
                "evidence": self.data["evidence"] + [proof(1, self.context[1], "technology")]}
        result = self.parse(data)
        self.assertTrue(result["work_found"])
        self.assertTrue(result["volume_confirmed"])
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["technology"]["status"], "different")
        self.assertIn("инженерная сверка", result["discrepancy_details"])

    def test_different_technology_with_broken_quantity_stays_error(self):
        data = {**self.data, "technology": {"status": "different",
                    "expected": "обжимными втулками", "actual": "талрепными стяжками"},
                "volume_confirmed": False,
                "evidence": self.data["evidence"] + [proof(1, self.context[1], "technology")]}
        result = self.parse(data)
        self.assertEqual(result["status"], "Ошибка")

    def test_not_applicable_technology_rejected_for_works(self):
        data = {**self.data,
                "technology": {"status": "not_applicable", "expected": "", "actual": ""}}
        with self.assertRaises(ValueError):
            self.parse(data)

    def test_material_technology_does_not_gate_status(self):
        data = {**self.data, "found_page": "5", "evidence": [
            proof(5, self.context[5]), proof(5, "Зажим 328 шт.", "volume"),
            proof(5, "328 шт.", "unit"), proof(1, self.context[1], "technology")],
            "technology": {"status": "unconfirmed",
                "expected": "Зажим", "actual": "соединением"}}
        result = parse_audit_response(json.dumps(data), self.context, False,
            position_type="Материал", expected_work="Зажим для троса")
        self.assertEqual(result["status"], "ОК")

    def test_confirmed_technology_missing_vor_terms_is_downgraded(self):
        data = {**self.data,
                "technology": {"status": "confirmed",
                    "expected": "обжимными втулками",
                    "actual": "талрепными стяжками"},
                "evidence": self.data["evidence"] + [proof(1, self.context[1], "technology")]}
        result = self.parse(data)
        self.assertEqual(result["technology"]["status"], "unconfirmed")
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertIn("отсутствуют условия ВОР", result["discrepancy_details"])
    def test_technology_cannot_be_invented_or_claimed_without_its_quote(self):
        for actual, expected in (("иной пресс", "обжимными втулками"),
                                 ("талрепными стяжками", "сваркой")):
            data = {**self.data, "technology": {"status": "different", "expected": expected, "actual": actual},
                    "evidence": self.data["evidence"] + [proof(1, self.context[1], "technology")]}
            with self.subTest(actual=actual, expected=expected), self.assertRaises(ValueError):
                self.parse(data)

    def test_unknown_technology_is_not_an_absent_operation(self):
        result = self.parse({**self.data, "technology": {"status": "unconfirmed",
            "expected": "обжимными втулками", "actual": "талрепными стяжками"},
            "evidence": self.data["evidence"] + [proof(1, self.context[1], "technology")]})
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertTrue(result["work_found"])
        self.assertEqual(result["found_volume"], "328")

    def test_unrelated_table_row_cannot_prove_the_requested_operation(self):
        context = {5: self.context[5]}
        data = {**self.data, "found_page": "5", "evidence": [
            proof(5, "Зажим 328 шт.", "work"), proof(5, "Зажим 328 шт.", "volume"),
            proof(5, "328 шт.", "unit")]}
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(data), context, False,
                position_type="Материал", expected_work="Геотекстиль Дорнит плотностью 500")

    def test_operation_term_check_does_not_reject_applicable_material_row(self):
        data = {**self.data, "found_page": "5", "evidence": [
            proof(5, self.context[5]), proof(5, "Зажим 328 шт.", "volume"),
            proof(5, "328 шт.", "unit")],
            "technology": {"status": "not_applicable", "expected": "", "actual": ""}}
        result = parse_audit_response(json.dumps(data), self.context, False,
            position_type="Материал", expected_work="Зажим для троса DIN 3093 10мм")
        self.assertEqual(result["status"], "ОК")

    def test_report_separates_technology_problem_from_quantity_and_reference(self):
        from test_audit_reports import ReportTests
        report = ReportTests().make_report([("Ошибка", {
            "technology": {"status": "different", "expected": "обжимными втулками",
                           "actual": "талрепными стяжками"}})])
        row = report.iloc[0]
        self.assertIn("обжимными втулками", row["Что не так"])
        self.assertIn("талрепными стяжками", row["Что не так"])
        self.assertNotIn("не на тех страницах", row["Что не так"])
        self.assertNotIn("Объём", row["Что не так"])

    def test_found_page_is_derived_from_single_work_citation(self):
        data = {**self.data, "found_page": "5"}
        result = self.parse(data)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["status"], "ОК")

    def test_conflicting_work_pages_still_reject(self):
        data = {**self.data, "found_page": "9",
                "evidence": self.data["evidence"] + [proof(5, "Зажим 328 шт.", "work")]}
        with self.assertRaises(ValueError):
            self.parse(data)

    def test_confirmation_rebinds_work_inside_same_scope_package(self):
        plan = ("Укладка гибких бетонных плит. План площадки ЛОС. "
                "Площадь крепления - 1879 м2 (площадка ЛОС). "
                "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
        section = ("Сечение 2-2. Площадка ЛОС. Укрепление откосов насыпи "
                   "гибкими бетонными плитами толщиной 0,15 м.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            if len(calls) == 1:
                data = response("2", evidence=[proof(2, section)], details="")
            else:
                data = response("1", evidence=[proof(1, plan)], details="")
                data["volume_calculation"] = {
                    "area": {"value": "1879", "unit": "м2", "page": 1, "quote": plan},
                    "thickness": {"value": "0,15", "unit": "м", "page": 2, "quote": section}}
            page_no = next(iter(context))
            piece = context[page_no].split("\n[...]\n")[0]
            data["technology"] = {"status": "confirmed", "expected": "гибких бетонных плит",
                "actual": piece[:200]}
            data["evidence"].append(proof(page_no, piece, "technology"))
            return parse_audit_response(json.dumps(data), context, False,
                expected_volume="281.85", expected_unit="м3", formula="1879*0,15",
                position_type="Работа",
                expected_work="Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль",
                expected_scope="ЛОС")
        result = audit_text_pages(PageTextIndex([plan, section]), [0], ask,
            "Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")
        self.assertEqual(result["found_pages"], [1, 2])

    def test_confirmation_rebinds_to_anchor_naming_both_scopes(self):
        plan = ("Укладка гибких бетонных плит. План. "
                "Площадь крепления - 526 м2 (площадка КНС) и 1879 м2 (площадка ЛОС). "
                "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
        section = ("Сечение 2-2. Площадка ЛОС. Укрепление откосов насыпи "
                   "гибкими бетонными плитами толщиной 0,15 м.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            if len(calls) == 1:
                data = response("2", evidence=[proof(2, section)], details="")
            else:
                data = response("1", evidence=[proof(1, plan)], details="")
                data["volume_calculation"] = {
                    "area": {"value": "1879", "unit": "м2", "page": 1, "quote": plan},
                    "thickness": {"value": "0,15", "unit": "м", "page": 2, "quote": section}}
            page_no = next(iter(context))
            piece = context[page_no].split("\n[...]\n")[0]
            data["technology"] = {"status": "confirmed", "expected": "гибких бетонных плит",
                "actual": piece[:200]}
            data["evidence"].append(proof(page_no, piece, "technology"))
            return parse_audit_response(json.dumps(data), context, False,
                expected_volume="281.85", expected_unit="м3", formula="1879*0,15",
                position_type="Работа",
                expected_work="Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль",
                expected_scope="ЛОС")
        result = audit_text_pages(PageTextIndex([plan, section]), [0], ask,
            "Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")

    def test_confirmation_rejects_rebinding_to_foreign_scope(self):
        plan = ("Укладка гибких бетонных плит. План площадки ЛОС. "
                "Площадь крепления - 1879 м2 (площадка ЛОС).")
        foreign = ("Сечение 1-1. Площадка КНС. "
                   "Укладка гибких бетонных плит.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            page = "1" if len(calls) == 1 else "2"
            quote = plan if len(calls) == 1 else foreign
            data = response(page, evidence=[proof(int(page), quote)], details="")
            page_no = next(iter(context))
            piece = context[page_no].split("\n[...]\n")[0]
            data["technology"] = {"status": "confirmed", "expected": "гибких бетонных плит",
                "actual": piece[:200]}
            data["evidence"].append(proof(page_no, piece, "technology"))
            return parse_audit_response(json.dumps(data), context, False,
                position_type="Работа",
                expected_work="Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль",
                expected_scope="ЛОС")
        result = audit_text_pages(PageTextIndex([plan, foreign]), [0], ask,
            "Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertIn("Добор не подтвердил связь", result["discrepancy_details"])


class TruncationRecoveryTests(unittest.TestCase):
    def test_compact_changed_attempt_keeps_original_work_and_proof(self):
        from test_audit import AdapterTests, ChatAdapterTests
        adapter = ChatAdapterTests()
        result, calls, _ = adapter.run_chat([
            adapter.chat_output("", finish_reason="length"),
            adapter.chat_output(AdapterTests.valid())])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["requests"], 2)
        self.assertNotEqual(calls[0]["messages"], calls[1]["messages"])

    def test_second_truncation_does_not_keep_retrying_or_restart_location_search(self):
        from test_audit import ChatAdapterTests
        adapter = ChatAdapterTests()
        truncated = adapter.chat_output("", finish_reason="length")
        result, calls, _ = adapter.run_chat([truncated, truncated])
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["recoverable"])


class CalculationScopeTests(unittest.TestCase):
    def test_area_and_section_must_belong_to_the_target_scope(self):
        area = "Плиты. Площадь 526 м² (площадка КНС) и 1879 м ² (площадка ЛОС)."
        context = {1: area, 3: "Сечение 2-2. Площадка ЛОС. Плиты толщиной 0,15 м.",
                   4: "Сечение 1-1. Площадка КНС. Плиты толщиной 0,15 м."}
        def data(number, page):
            result = response("1", evidence=[proof(1, area)], details="")
            result["volume_calculation"] = {
                "area": {"value": number, "unit": "м2", "page": 1, "quote": area},
                "thickness": {"value": "0,15", "unit": "м", "page": page, "quote": context[page]}}
            return result
        valid = parse_audit_response(json.dumps(data("1879", 3)), context, False,
            expected_volume="281.85", expected_unit="м3", formula="1879*0,15", expected_scope="ЛОС")
        self.assertEqual(valid["found_volume"], "281.85")
        self.assertEqual(valid["quantity_basis"], "derived")
        for number, page, expected in (("526", 3, "78.9"), ("1879", 4, "281.85")):
            with self.subTest(number=number, page=page), self.assertRaises(ValueError):
                parse_audit_response(json.dumps(data(number, page)), context, False,
                    expected_volume=expected, expected_unit="м3",
                    formula=f"{number}*0,15", expected_scope="ЛОС")

    def test_operand_scope_prefers_its_own_citation_over_mixed_page(self):
        quote = "Площадь крепления - 1879м ² (площадка ЛОС)."
        context = {1: "Площадь 526 м² (площадка КНС) и " + quote,
                   2: "Сечение 2-2. Площадка ЛОС. Плиты толщиной 0,15 м."}
        data = response("1", evidence=[proof(1, quote)], details="")
        data["volume_calculation"] = {
            "area": {"value": "1879", "unit": "м2", "page": 1, "quote": quote},
            "thickness": {"value": "0,15", "unit": "м", "page": 2, "quote": context[2]}}
        result = parse_audit_response(json.dumps(data), context, False,
            expected_volume="281.85", expected_unit="м3", formula="1879*0,15",
            expected_scope="ЛОС")
        self.assertEqual(result["found_volume"], "281.85")

    def test_ocr_split_operation_and_split_technology_terms_are_accepted(self):
        area = ("к ре пление г и бкими бе тонными плитами. "
                "П лоща д ь крепления - 1879м ² (площадка ЛОС).")
        section = ("Укрепление откосов насыпи гибкими бетонными плитами толщиной 0,15 м. "
                   "Противосуффозионный экран геотекстиль Дорнит с прочностью не менее.")
        context = {1: area, 2: section}
        work = "Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль; 1879 м2, толщина плит"
        data = response("1", evidence=[proof(1, area), proof(2, section, "technology")], details="")
        data["technology"] = {"status": "confirmed", "expected": "на геотекстиль",
                              "actual": "Противосуффозионный экран геотекстиль Дорнит"}
        data["volume_calculation"] = {
            "area": {"value": "1879", "unit": "м2", "page": 1, "quote": area},
            "thickness": {"value": "0,15", "unit": "м", "page": 2, "quote": section}}
        result = parse_audit_response(json.dumps(data), context, False,
            expected_volume="281.85", expected_unit="м3", formula="1879*0,15",
            position_type="Работа", expected_work=work, expected_scope="ЛОС")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")
        self.assertEqual(result["quantity_basis"], "derived")

    def test_thickness_of_another_object_is_rejected(self):
        context = {1: "Площадь крепления - 1879 м2 (площадка ЛОС).",
                   2: "Засыпка зазоров между бетонными блоками вручную щебнем."}
        data = response("1", evidence=[proof(1, context[1])], details="")
        data["volume_calculation"] = {
            "area": {"value": "1879", "unit": "м2", "page": 1, "quote": context[1]},
            "thickness": {"value": "0,15", "unit": "м", "page": 1,
                          "quote": "Плиты толщиной 0,15 м. " + context[1]}}
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(data), context, False,
                expected_volume="281.85", expected_unit="м3", formula="1879*0,15",
                position_type="Работа",
                expected_work="Засыпка зазоров между бетонными блоками",
                expected_scope="ЛОС")


class CatalogChunkTests(unittest.TestCase):
    def test_measurement_is_not_split_across_catalog_chunks(self):
        pad = "x" * 940
        chunks = list(_catalog_chunks(pad + " площадь 1879 м2 конец " + "y" * 300))
        self.assertEqual(len(chunks), 2)
        self.assertTrue(any("1879 м2" in chunk for chunk in chunks))
        self.assertLessEqual(max(map(len, chunks)), 1000)
    def test_table_chunks_end_on_closed_rows(self):
        rows = "".join(
            f"<tr><td>1.{i}</td><td>Материал номер {i} с длинным названием</td>"
            f"<td>шт.</td><td>{100 + i}</td></tr>" for i in range(30))
        table = "<table>" + rows + "</table>"
        chunks = list(_catalog_chunks(table))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertEqual(chunk.count("<tr"), chunk.count("</tr>"))
        self.assertTrue(any("115" in chunk for chunk in chunks))

    def test_chunking_terminates_with_long_measurement_prefix(self):
        chunks = list(_catalog_chunks("z" * 1200 + " 1879 м2"))
        self.assertTrue(chunks)
        self.assertTrue(any("1879 м2" in chunk for chunk in chunks))

