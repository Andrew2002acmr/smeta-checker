"""Stage 2: code-owned measurement checks, not model verdict flags."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audit_measurements import (
    canonical_unit,
    compare_measurements,
    iter_measurements,
    parse_measurement,
)


class MeasurementParsingTests(unittest.TestCase):
    def test_exact_operator_and_range_states(self):
        self.assertEqual(
            compare_measurements(
                parse_measurement("631,2", unit="м2"),
                parse_measurement("631.20", unit="м2")).get("state"),
            "confirmed")
        self.assertEqual(
            compare_measurements(
                parse_measurement("не менее 25", unit="кН/м"),
                parse_measurement("25/25", unit="кН/м")).get("state"),
            "review_required")
        self.assertEqual(
            compare_measurements(
                parse_measurement("10-12", unit="мм"),
                parse_measurement("11", unit="мм")).get("state"),
            "confirmed")
        self.assertEqual(
            compare_measurements(
                parse_measurement("10-12", unit="мм"),
                parse_measurement("11-13", unit="мм")).get("state"),
            "review_required")

    def test_directed_pair_and_variant_are_not_arithmetic(self):
        expected = parse_measurement("11/12")
        self.assertEqual(expected.status, "directed_pair")
        self.assertEqual(
            compare_measurements(
                expected, parse_measurement("25/25")).get("state"),
            "contradiction")
        variant = parse_measurement("Плиты 5 (3)–20")
        self.assertEqual(variant.status, "variant")
        self.assertEqual(
            compare_measurements(variant, parse_measurement("20")).get("state"),
            "review_required")

    def test_closed_units_and_quantity_kinds(self):
        self.assertEqual(canonical_unit("м 2"), ("м2", "area"))
        self.assertIsNone(canonical_unit("ведро"))
        for spelling in ("точка", "точки", "пункт", "пункта",
                         "наблюдение", "наблюдения", "наблюдений",
                         "точка наблюдения", "точка наблюдений"):
            with self.subTest(spelling=spelling):
                self.assertEqual(canonical_unit(spelling), ("точка", "observation"))
        self.assertEqual(
            compare_measurements(
                parse_measurement("8", unit="наблюдение"),
                parse_measurement("8", unit="точка")).get("state"),
            "confirmed")
        self.assertEqual(
            compare_measurements(
                parse_measurement("10", unit="м3", kind="material_volume"),
                parse_measurement("10", unit="м3",
                                  kind="geometric_envelope")).get("state"),
            "contradiction")
        self.assertEqual(
            compare_measurements(
                parse_measurement("10", unit="м3", kind="material_volume"),
                parse_measurement("10", unit="шт", kind="item_count")).get("state"),
            "contradiction")

    def test_atomic_measurements_preserve_order_and_roles(self):
        measurements = iter_measurements("Марка 86,79; щебень 86,79 м3")
        self.assertEqual(
            [(str(item.value), item.unit) for item in measurements],
            [("86.79", "м3")])
        self.assertEqual(
            [(str(item.value), item.unit)
             for item in iter_measurements("Щебень М400 86,79 м3")],
            [("86.79", "м3")])
        self.assertEqual(
            [(str(item.value), item.unit)
             for item in iter_measurements("м2 631,2")],
            [("631.2", "м2")])
        self.assertEqual(
            [(str(item.value), item.unit)
             for item in iter_measurements("Объём 1 879 500,25 м3 и 45 м")],
            [("1879500.25", "м3"), ("45", "м")])
        self.assertEqual(
            compare_measurements(
                parse_measurement("86,79", unit="м3"),
                parse_measurement("310,035", unit="м3")).get("state"),
            "contradiction")
        self.assertEqual(
            compare_measurements(
                parse_measurement("631.1999999999999", unit="м2"),
                parse_measurement("631,2", unit="м2")).get("state"),
            "confirmed")


class ClaimConflictTests(unittest.TestCase):
    def test_two_applicable_facts_are_conflict_not_missing(self):
        import json
        from audit_core import parse_audit_response
        context = {34: "Длина участка 172 м по проекту.",
                   84: "План: длина 171 м."}
        sources = {"p34q1": {"page": 34, "quote": context[34]},
                   "p84q1": {"page": 84, "quote": context[84]}}
        wire = {"claims": [
            {"claim_id": "presence", "source_ids": ["p34q1"]},
            {"claim_id": "quantity", "source_ids": ["p34q1", "p84q1"]},
            {"claim_id": "unit", "source_ids": ["p34q1"]}],
            "discrepancy_details": ""}
        result = parse_audit_response(
            json.dumps(wire), context, comment_required=False,
            position_type="Материал", expected_volume="172",
            expected_unit="м", expected_work="Длина участка",
            quote_sources=sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "source_conflict")
        self.assertEqual(len(by_id["quantity"]["facts"]), 2)
        self.assertEqual(result["status"], "Неполная проверка")

    def test_bad_record_does_not_erase_valid_quantity(self):
        import json
        from audit_core import parse_audit_response
        text = "Кабель 45 м"
        result = parse_audit_response(json.dumps({"claims": [
            {"claim_id": "presence", "source_ids": ["s"]},
            {"claim_id": "quantity", "source_ids": ["s"]},
            {"claim_id": "unit", "source_ids": ["s"]},
            {"claim_id": "attribute", "source_ids": ["s"], "status": "confirmed"}
        ]}), {1: text}, False, expected_volume="45", expected_unit="м",
            expected_work="Кабель", position_type="Материал",
            quote_sources={"s": {"page": 1, "quote": text}})
        self.assertTrue(result["volume_confirmed"])
        self.assertEqual(result["found_volume"], "45")

    def test_quantity_alone_cannot_close_required_strength(self):
        import json
        from types import SimpleNamespace
        from audit_core import build_claim_specs, parse_audit_response
        position = SimpleNamespace(work_name="Геотекстиль", comment="Прочность 11/12",
                                   volume="631.2", unit="м2", position_type="Материал")
        text = "Геотекстиль. Количество 631,2 м2"
        result = parse_audit_response(json.dumps({"claims": [
            {"claim_id": "presence", "source_ids": ["s"]},
            {"claim_id": "quantity", "source_ids": ["s"]},
            {"claim_id": "unit", "source_ids": ["s"]}
        ]}), {1: text}, False, expected_volume="631.2", expected_unit="м2",
            expected_work="Геотекстиль", position_type="Материал",
            claim_specs=build_claim_specs(position),
            quote_sources={"s": {"page": 1, "quote": text}})
        self.assertTrue(result["volume_confirmed"])
        self.assertEqual(result["status"], "Неполная проверка")
        self.assertTrue(any(check["kind"] == "attribute" and check["required"]
                            and check["state"] != "confirmed"
                            for check in result["checks"]))

    def test_density_predicate_compares_value_and_keeps_quantity(self):
        import json
        from types import SimpleNamespace
        from audit_core import build_claim_specs, parse_audit_response
        position = SimpleNamespace(work_name="Геотекстиль", comment="Плотность 500 г/м2",
                                   volume="631.2", unit="м2", position_type="Материал")
        specs = build_claim_specs(position)
        attribute = next(spec for spec in specs if spec.kind == "attribute")
        for actual, state in (("500", "confirmed"), ("400", "contradiction")):
            with self.subTest(actual=actual):
                context = {1: "Геотекстиль 631,2 м2", 2: f"Плотность {actual} г/м2"}
                sources = {"qty": {"page": 1, "quote": context[1]},
                           "attr": {"page": 2, "quote": context[2]}}
                wire = {"claims": [
                    {"claim_id": "presence", "source_ids": ["qty"]},
                    {"claim_id": "quantity", "source_ids": ["qty"]},
                    {"claim_id": "unit", "source_ids": ["qty"]},
                    {"claim_id": attribute.claim_id, "source_ids": ["attr"]}]}
                result = parse_audit_response(
                    json.dumps(wire), context, False, expected_volume="631.2",
                    expected_unit="м2", expected_work="Геотекстиль",
                    position_type="Материал", claim_specs=specs, quote_sources=sources)
                checks = {check["claim_id"]: check for check in result["checks"]}
                self.assertEqual(checks[attribute.claim_id]["state"], state)
                self.assertTrue(result["volume_confirmed"])


class WorkerReferenceTests(unittest.TestCase):
    def test_invalid_reference_is_independent_of_matching_pdf_quantity(self):
        import json
        import pandas as pd
        from audit_core import PageTextIndex, parse_page_reference, parse_audit_response
        from audit_service import worker_check_row
        from audit_reports import build_user_report

        text = "Кабель 45 м"
        index = PageTextIndex([text])
        for reference, expected in (("2", "Ошибка"), ("1 2", "Ошибка"),
                                    ("не страница", "Ошибка"), ("", "ОК")):
            with self.subTest(reference=reference):
                indices, description, pages = parse_page_reference(reference, 1)
                task = {"idx": 0, "w_name": "Кабель", "w_vol": "45", "w_unit": "м",
                        "w_ref": "", "w_page": reference, "w_comment": "", "w_formula": "",
                        "position_type": "Материал", "text_index": index,
                        "target_indices": indices, "loc_desc": description,
                        "primary_phys_pages": pages, "target_pdf_name": "pd.pdf"}
                def audit(**kwargs):
                    return parse_audit_response(json.dumps({"claims": [
                        {"claim_id": kind, "source_ids": ["s"]}
                        for kind in ("presence", "quantity", "unit")
                    ]}), {1: text}, False, expected_volume="45", expected_unit="м",
                        expected_work="Кабель", position_type="Материал",
                        quote_sources={"s": {"page": 1, "quote": text}})
                _, row, tracking = worker_check_row(task, audit)
                self.assertEqual(row["Статус аудита"], expected)
                self.assertTrue(tracking["volume_confirmed"])
                report = build_user_report(
                    pd.DataFrame([{"№": 1, "Статус аудита": row["Статус аудита"]}]),
                    [tracking], [task])
                self.assertEqual(report[0]["Статус аудита"],
                                 "Обнаружено расхождение" if expected == "Ошибка" else "ОК")

    def test_truncated_answer_with_only_workbook_checks_is_not_ok(self):
        from audit_core import PageTextIndex, empty_result, INCOMPLETE, TECHNICAL_ERROR
        from audit_service import worker_check_row

        index = PageTextIndex(["Кабель 45 м"])
        task = {"idx": 0, "w_name": "Кабель", "w_vol": "45", "w_unit": "м",
                "w_ref": "", "w_page": "1", "w_comment": "", "w_formula": "",
                "position_type": "Материал", "text_index": index,
                "target_indices": [0], "loc_desc": "Стр. 1",
                "primary_phys_pages": [1], "target_pdf_name": "pd.pdf",
                "corpus_id": "x" * 64,
                "calculation": {"cell": "D1", "status": "calculated"}}
        def audit(**kwargs):
            failure = empty_result(
                INCOMPLETE, "Неполная проверка: ответ модели обрезан "
                "лимитом вывода (length).")
            failure["recoverable"] = False
            failure["execution_errors"] = [{
                "kind": "provider",
                "message": "Ответ модели обрезан лимитом вывода; PDF не оценён.",
                "recoverable": False}]
            return failure
        _, row, tracking = worker_check_row(task, audit)
        self.assertEqual(row["Статус аудита"], TECHNICAL_ERROR)
        self.assertTrue(tracking["execution_errors"])


if __name__ == "__main__":
    unittest.main()
