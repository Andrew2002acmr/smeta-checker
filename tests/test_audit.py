"""Consumer-visible audit/search boundaries; no live credentials."""

import ast
import json
from decimal import Decimal
from pathlib import Path
import re
import random
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import (BudgetExceeded, INCOMPLETE, PageTextIndex, RequestBudget,
                        TECHNICAL_ERROR, audit_text_pages, empty_result,
                        parse_audit_response, parse_page_reference, technical_error,
                        _numbers, _terms)


def response(page="-", volume="-", unit="-", volume_ok=False, unit_ok=False,
             comment_ok=False, evidence=(), details="Не найдено", status="Ошибка"):
    return {"status": status, "work_found": page != "-", "found_page": page,
            "found_volume": volume, "found_unit": unit,
            "volume_confirmed": volume_ok, "unit_confirmed": unit_ok,
            "comment_confirmed": comment_ok, "evidence": list(evidence),
            "discrepancy_details": details}


def proof(page, quote, kind="work"):
    return {"page": page, "quote": quote, "kind": kind}


def found(context, page, volume="45", matching=True):
    quote = context[page]
    data = response(str(page), volume, "m", matching, True,
                    evidence=[proof(page, quote), proof(page, quote, "volume"),
                              proof(page, quote, "unit")], details="Volume mismatch" if not matching else "Confirmed")
    return parse_audit_response(json.dumps(data), context, comment_required=False)


def app_functions(**extra):
    """Load adapter/worker without starting Streamlit."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
    names = {"audit_position_hybrid", "worker_check_row"}
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"INCOMPLETE": INCOMPLETE, "TECHNICAL_ERROR": TECHNICAL_ERROR,
                 "BudgetExceeded": BudgetExceeded, "RequestBudget": RequestBudget,
                 "audit_text_pages": audit_text_pages, "parse_audit_response": parse_audit_response,
                 "empty_result": empty_result, "technical_error": technical_error,
                 "time": SimpleNamespace(sleep=lambda _: None), "re": re,
                 "YANDEX_API_KEY": "test", "YANDEX_FOLDER": "test", "YANDEX_MODEL_NAME": "test-model", **extra}
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
        self.assertEqual(pages, [9])
        self.assertIn("99", description)

    def test_rejects_bad_or_huge_ranges_without_guessing(self):
        for value in ("9.5", "текст 9", "12-9", "0", "1-9999999999999", "23242243", None, float("nan")):
            with self.subTest(value=value):
                self.assertEqual(parse_page_reference(value, 303)[0], [])


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.context = {3: "Copper cable 45 m"}
        self.data = response("3", "45", "m", True, True,
                             evidence=[proof(3, "Copper cable"), proof(3, "45 m", "volume"), proof(3, "m", "unit")])

    def parse(self, data=None, context=None):
        return parse_audit_response(json.dumps(self.data if data is None else data),
                                    self.context if context is None else context, comment_required=False)

    def test_multpage_evidence_and_whitespace(self):
        data = response("1", "45", "m", True, True,
                        evidence=[proof(1, "Copper cable"), proof(7, "45 m", "volume"), proof(7, "m", "unit")])
        result = self.parse(data, {1: "Copper\n cable", 7: "Quantity 45 m"})
        self.assertEqual(result["found_pages"], [1, 7])
        self.assertEqual(result["status"], "ОК")


    def test_unverified_ok_is_a_mismatch(self):
        result = self.parse({**self.data, "status": "ОК", "volume_confirmed": False})
        self.assertEqual(result["status"], "Ошибка")
        self.assertEqual(result["found_page"], "3")

    def test_forged_quote_or_unsent_page_rejected(self):
        for evidence in ([proof(99, "Copper cable")], [proof(3, "Steel cable")], [proof(3, "Copper cable ... 45")]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.parse({**self.data, "evidence": evidence})

    def test_quote_elsewhere_on_page_is_not_valid_in_fragment(self):
        with self.assertRaises(ValueError):
            self.parse(context={3: "Quantity 45 m"})

    def test_found_volume_must_appear_in_evidence(self):
        with self.assertRaises(ValueError):
            self.parse({**self.data, "found_volume": "46"})

    def test_volume_quote_can_prove_unit_with_worded_quantity(self):
        data = response("3", "10", "пункт", True, True, evidence=[
            {"page": "3", "quote": "Опорная сеть", "kind": "work"},
            {"page": "3", "quote": "из десяти определяемых пунктов", "kind": "volume"}])
        result = self.parse(data, {3: "Опорная сеть создана из десяти определяемых пунктов"})
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_pages"], [3])
        self.assertTrue(result["unit_confirmed"])

    def test_shared_volume_quote_must_contain_claimed_unit(self):
        with self.assertRaises(ValueError):
            self.parse({**self.data, "found_unit": "kg", "evidence": [
                proof(3, "Copper cable"), proof(3, "45 m", "volume")]})

    def test_work_location_requires_work_quote(self):
        with self.assertRaises(ValueError):
            self.parse({**self.data, "evidence": [proof(3, "45 m", "volume"), proof(3, "m", "unit")]})

    def test_comment_confirmation_requires_its_own_quote(self):
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps({**self.data, "comment_confirmed": True}), self.context)

    def test_thinking_and_markdown_are_not_source_evidence(self):
        raw = '<think>reason</think>```json\n' + json.dumps(self.data) + '\n```'
        result = parse_audit_response(raw, self.context, comment_required=False)
        self.assertEqual(result["found_page"], "3")

    def test_invalid_response_shapes(self):
        for raw in ("", "no json", '{"status":"Ошибка"}', json.dumps({**self.data, "work_found": "true"}),
                    json.dumps({**self.data, "found_page": True}),
                    json.dumps({**self.data, "evidence": {}}), json.dumps({**self.data, "found_volume": []})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_audit_response(raw, self.context)


class CalculatedVolumeTests(unittest.TestCase):
    def setUp(self):
        self.context = {84: "Крепление плитами. Площадь крепления - 526м² (площадка КНС).",
                        85: "Плиты толщиной 0,15 м на геотекстиле"}
        self.data = response("84", evidence=[proof(84, "Крепление плитами.")], details="")
        self.data["volume_calculation"] = {
            "area": {"value": "526", "unit": "м2", "page": 84,
                     "quote": "Площадь крепления - 526м² (площадка КНС)."},
            "thickness": {"value": "0,15", "unit": "м", "page": 85,
                          "quote": "Плиты толщиной 0,15 м на геотекстиле"}}

    def parse(self, expected="78.89999999999999", unit="м3", formula="526*0,15"):
        return parse_audit_response(json.dumps(self.data), self.context, False,
                                    expected_volume=expected, expected_unit=unit, formula=formula)

    def test_area_times_thickness_keeps_both_sources_without_fabricated_result_quote(self):
        result = self.parse()
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "78.9")
        self.assertEqual(result["found_unit"], "м3")
        self.assertEqual(result["found_pages"], [84, 85])
        self.assertEqual([e["quote"] for e in result["evidence"] if e["kind"] == "volume"],
                         [self.data["volume_calculation"][k]["quote"] for k in ("area", "thickness")])

    def test_millimetres_are_converted_before_comparison(self):
        self.context[85] = "Плиты толщиной 150 мм"
        self.data["volume_calculation"]["thickness"].update(value="150", unit="мм", quote=self.context[85])
        self.assertEqual(self.parse()["found_volume"], "78.9")

    def test_mismatching_volume_remains_a_finding(self):
        result = self.parse(expected="79")
        self.assertEqual(result["status"], "Ошибка")
        self.assertTrue(result["work_found"])
        self.assertFalse(result["volume_confirmed"])
        self.assertEqual(result["found_volume"], "78.9")

    def test_operand_missing_from_pdf_is_rejected_even_if_it_is_in_vor_formula(self):
        self.data["volume_calculation"]["area"]["value"] = "527"
        with self.assertRaises(ValueError):
            self.parse(formula="527*0,15")

    def test_forged_quote_unsent_page_and_wrong_dimension_are_rejected(self):
        for patch in ({"quote": "Плиты толщиной 0,20 м"}, {"page": 86},
                      {"unit": "м2"}, {"value": "0"}, {"value": True}):
            with self.subTest(patch=patch):
                original = self.data["volume_calculation"]["thickness"].copy()
                self.data["volume_calculation"]["thickness"].update(patch)
                with self.assertRaises(ValueError):
                    self.parse()
                self.data["volume_calculation"]["thickness"] = original

    def test_unsupported_formula_or_output_unit_is_not_silently_calculated(self):
        for unit, formula in (("м2", "526*0,15"), ("м3", ""), ("м3", "526*0,15*1,1")):
            with self.subTest(unit=unit, formula=formula), self.assertRaises(ValueError):
                self.parse(unit=unit, formula=formula)


class SearchTests(unittest.TestCase):
    def test_found_volume_mismatch_stops_location_search(self):
        calls = []
        def ask(context):
            calls.append(context)
            return found(context, 1, "40", matching=False)
        result = audit_text_pages(PageTextIndex(["Cable 40 m", "Cable 45 m"]), [0], ask, "Cable", "45")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_volume"], "40")
        self.assertEqual(result["status"], "Ошибка")
        self.assertTrue(result["search_complete"])

    def test_missing_coefficient_does_not_trigger_search(self):
        calls = []
        def ask(context):
            calls.append(context)
            result = found(context, 1)
            result.update(comment_confirmed=False, status="Ошибка")
            return result
        result = audit_text_pages(PageTextIndex(["Cable 45 m"] * 50), [0], ask, "Cable", "45", comment="Coefficient 1.2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["found_page"], "1")
        self.assertFalse(result["comment_confirmed"])

    def test_one_confirmation_joins_far_quantity_without_restarting_location_search(self):
        calls = []
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 8 + ["Cable quantity 45 m"])
        def ask(context):
            calls.append(context)
            data = response("1", evidence=[proof(1, "Cable description")])
            if len(calls) == 2:
                data.update(found_volume="45", found_unit="m", volume_confirmed=True, unit_confirmed=True)
                data["evidence"] += [proof(10, "45 m", "volume")]
            return parse_audit_response(json.dumps(data), context, comment_required=False)
        result = audit_text_pages(index, [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1, 10])
        self.assertEqual(result["found_volume"], "45")
        self.assertEqual(result["status"], "ОК")

    def test_confirmation_failure_keeps_location_and_technical_status_outside_reference(self):
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 2:
                return technical_error("API unavailable")
            return parse_audit_response(json.dumps(response("2", evidence=[proof(2, "Cable description")])),
                                        context, comment_required=False)
        result = audit_text_pages(PageTextIndex(["Title", "Cable description"] + ["Introduction"] * 7 + ["Cable 45 m"]),
                                  [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["found_page"], "2")
        self.assertEqual(result["found_pages"], [2])
        self.assertFalse(result["search_complete"])

    def test_confirmation_cannot_exceed_shared_budget(self):
        calls = []
        def ask(context):
            calls.append(context)
            return parse_audit_response(json.dumps(response("1", evidence=[proof(1, "Cable description")])),
                                        context, comment_required=False)
        result = audit_text_pages(PageTextIndex(["Cable description"] + ["Introduction"] * 8 + ["Cable 45 m"]),
                                  [0], ask, "Cable", "45", unit="m", max_contexts=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "1")

    def test_confirmation_can_complete_missing_quantity_on_same_long_page(self):
        calls = []
        def ask(context):
            calls.append(context)
            data = response("1", evidence=[proof(1, "Cable description")])
            if len(calls) == 2:
                data.update(found_volume="45", found_unit="m", volume_confirmed=True, unit_confirmed=True)
                data["evidence"] += [proof(1, "Quantity 45 m", "volume")]
            return parse_audit_response(json.dumps(data), context, comment_required=False)
        text = "Cable description " * 200 + "Unrelated appendix " * 4000 + "Quantity 45 m"
        result = audit_text_pages(PageTextIndex([text]), [0], ask, "Cable", "45", unit="m", max_context_chars=1000)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "45")
        self.assertTrue(all(sum(map(len, c.values())) <= 1000 for c in calls))

    def test_related_reference_pages_checked_together(self):
        calls = []
        def ask(context):
            calls.append(context)
            data = response("1", "45", "m", True, True,
                            evidence=[proof(1, "Copper cable"), proof(3, "45 m", "volume"), proof(3, "m", "unit")])
            return parse_audit_response(json.dumps(data), context, comment_required=False)
        result = audit_text_pages(PageTextIndex(["Copper cable", "Introduction", "Cable quantity 45 m"]), [0, 2], ask, "Copper cable", "45")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["found_pages"], [1, 3])
        self.assertEqual(result["status"], "ОК")

    def test_neighbor_table_volume_combines_with_description(self):
        def ask(context):
            data = response("1", "45", "m", True, True,
                            evidence=[proof(1, "Cable"), proof(2, "45 m", "volume"), proof(2, "m", "unit")])
            return parse_audit_response(json.dumps(data), context, comment_required=False)
        result = audit_text_pages(PageTextIndex(["Cable", "Quantity 45 m"]), [0], ask, "Cable", "45")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_pages"], [1, 2])

    def test_same_row_table_volume_ranks_first_for_confirmation(self):
        texts = ["Cable 45 m unrelated appendix", "Cable description and quantity 45 m"]
        plain = PageTextIndex(texts).support_candidates("Cable", "45", "m")
        rows = PageTextIndex(texts, table_rows=[
            ("Cable specification | different item", "Another position | 45 | m"),
            ("Cable description | 45 | m",)]).support_candidates("Cable", "45", "m")
        self.assertEqual(rows[0], 1)
        self.assertNotEqual(plain[0], rows[0])

    def test_far_location_preserved_on_wrong_reference(self):
        index = PageTextIndex(["Title", "Contents"] + ["Introduction"] * 20 + ["Copper cable 45 m"])
        def ask(context):
            return found(context, 23) if 23 in context else empty_result("Ошибка", "Not in these fragments")
        result = audit_text_pages(index, [0], ask, "Copper cable", "45")
        self.assertEqual(result["found_page"], "23")
        self.assertEqual(result["status"], "Ошибка")
        self.assertTrue(result["work_found"])

    def test_end_of_huge_page_is_locally_retrieved(self):
        text = "Introduction " * 10000 + "Copper cable 45 m"
        seen = []
        def ask(context):
            seen.append(context[1])
            return parse_audit_response(json.dumps(response("1", "45", "m", True, True,
                evidence=[proof(1, "Copper cable 45 m", kind) for kind in ("work", "volume", "unit")])), context, comment_required=False)
        result = audit_text_pages(PageTextIndex([text]), [0], ask, "Copper cable", "45", max_context_chars=1000)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(len(seen[0]), 1000)

    def test_no_location_does_not_claim_document_absence(self):
        result = audit_text_pages(PageTextIndex(["Unrelated"] * 303), [0], lambda _: empty_result("Ошибка", "Not found"), "Cable", "45")
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertFalse(result["search_complete"])
        self.assertEqual(result["found_page"], "-")

    def test_request_and_character_budgets_stop_negative_search(self):
        for limits in ({"max_contexts": 2}, {"max_total_chars": 300, "max_context_chars": 200}):
            calls = []
            def ask(context):
                calls.append(context)
                return empty_result("Ошибка", "Not in context")
            result = audit_text_pages(PageTextIndex(["Cable 40 m " * 100] * 30), [0], ask, "Cable", "45", **limits)
            self.assertEqual(result["status"], INCOMPLETE)
            self.assertLessEqual(len(calls), 2)
            self.assertLessEqual(sum(len(t) for c in calls for t in c.values()), limits.get("max_total_chars", 60000))

    def test_failed_or_pending_reference_retains_found_evidence(self):
        for index in (PageTextIndex(["", "Cable 45 m"], {0: "OCR unavailable"}),
                      PageTextIndex(["", "Cable 45 m"], pending_ocr=[0])):
            result = audit_text_pages(index, [0], lambda c: found(c, 2), "Cable", "45")
            self.assertEqual(result["status"], INCOMPLETE)
            self.assertEqual(result["found_page"], "2")
            self.assertFalse(result["search_complete"])

    def test_provider_failure_stops_without_more_search(self):
        calls = []
        def ask(context):
            calls.append(context)
            return technical_error("401")
        result = audit_text_pages(PageTextIndex(["Cable"] * 100), [0], ask, "Cable", "45")
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(len(calls), 1)


class TextRankingTests(unittest.TestCase):
    def test_nfkc_material_codes_and_units_remain_searchable(self):
        self.assertIn("lcf", _terms("𝛾𝑙𝑐𝐹≤(𝛾𝑐/𝛾𝑛)𝑅"))
        self.assertTrue({"гбп-1-150", "в30"} <= _terms("Плиты ГБП-1-150 бетон В30"))
        self.assertIn("3-20", _terms("фракции 3-20 мм"))
        self.assertIn("м/с", _terms("скорость 18,7 м/с"))
        for query, pages in (
                ("В30", ["В20", "В30"]),
                ("м2", ["м3", "м²"]),
                ("л/с", ["м/с", "л/с"])):
            with self.subTest(query=query):
                self.assertEqual(PageTextIndex(pages).candidates(query, "", "", ""), [1])

    def test_digit_codes_outweigh_generic_work_words(self):
        index = PageTextIndex(["бетон", "В30"])
        self.assertEqual(index.candidates("бетон В30", "", "", ""), [1, 0])

    def test_quantity_search_does_not_repeat_exact_material_location_ranking(self):
        query = "Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль"
        quantity = "Укладка гибких бетонных плит на геотекстиль. Площадь 526 м2"
        section = "Гибкие бетонные плиты ПБЗГУ-405 на геотекстиле. Толщина 0,15 м"
        index = PageTextIndex([quantity, section, section, section])
        self.assertEqual(index.candidates(query, "", "", "")[0], 1)
        self.assertEqual(index.support_candidates(query, "", "м3")[0], 0)
        codes = PageTextIndex(["В20 40", "В30 45"])
        self.assertEqual(codes.support_candidates("В30", "", ""), [1])

    def test_work_coverage_ranks_complete_description_first(self):
        index = PageTextIndex(["укладка труб", "укладка гибких бетонных плит"])
        self.assertEqual(index.candidates("укладка гибких бетонных плит", "", "", ""), [1, 0])

    def test_grouped_quantity_retrieves_and_validates_without_changing_source(self):
        for space in (" ", "\u00a0", "\u2009"):
            with self.subTest(space=repr(space)):
                text = f"Расход {1}{space}974,71 л/с"
                self.assertEqual(_numbers(text), {Decimal("1974.71")})
                index = PageTextIndex(["Расход 974,71 л/с", text])
                self.assertEqual(index.support_candidates("Расход", "1974.71", "л/с")[0], 1)
                data = response("2", "1974.71", "л/с", True, True, evidence=[
                    proof(2, "Расход"), proof(2, text, "volume"), proof(2, "л/с", "unit")])
                result = parse_audit_response(json.dumps(data), {2: text}, False)
                self.assertEqual(result["status"], "ОК")
                self.assertEqual(result["evidence"][1]["quote"], text)
        self.assertEqual(_numbers("45,00 46.25"), {Decimal("45.00"), Decimal("46.25")})

    def test_irrelevant_header_does_not_consume_fragment_budget(self):
        text = "Unrelated cover sheet\n" + "appendix\n" * 300 + "Copper cable 45 m\n"
        fragment = PageTextIndex([text]).fragment(0, "Copper cable", "", "", "", 200)
        self.assertIn("Copper cable 45 m", fragment)
        self.assertNotIn("Unrelated cover sheet", fragment)
        self.assertLessEqual(len(fragment), 200)

    def test_line_alignment_preserves_quote_without_rewriting_it(self):
        quote = "Copper cable, rated voltage 660 V, quantity 45 m."
        text = "Unrelated cover\n" + "appendix\n" * 200 + quote + "\n" + "appendix\n" * 200
        fragment = PageTextIndex([text]).fragment(0, "Copper cable", "", "", "45", 2000)
        self.assertIn(quote, fragment)
        self.assertLessEqual(len(fragment), 2000)
        data = response("1", evidence=[proof(1, quote)])
        self.assertTrue(parse_audit_response(json.dumps(data), {1: fragment}, False)["work_found"])


class EvidenceAndRetrievalTests(unittest.TestCase):
    def test_model_status_cannot_override_evidence_or_cause_retry(self):
        data = response("1", "45", "m", True, True, evidence=[
            proof(1, "Cable 45 m", kind) for kind in ("work", "volume", "unit")])
        data["status"] = "Частично верно"
        self.assertEqual(parse_audit_response(json.dumps(data), {1: "Cable 45 m"}, False)["status"], "ОК")
        del data["status"]
        data["volume_confirmed"] = False
        self.assertEqual(parse_audit_response(json.dumps(data), {1: "Cable 45 m"}, False)["status"], "Ошибка")

    def test_typographic_variants_but_not_omitted_or_rephrased_words(self):
        context = {1: 'Кабель «ВВГ» — 45 м'}
        data = response("1", evidence=[proof(1, 'Кабель "ВВГ" - 45 м')])
        self.assertTrue(parse_audit_response(json.dumps(data), context, False)["work_found"])
        for quote in ('Кабель ... 45 м', 'Провод «ВВГ» — 45 м'):
            data["evidence"][0]["quote"] = quote
            with self.assertRaises(ValueError):
                parse_audit_response(json.dumps(data), context, False)

    def test_quote_cannot_cross_artificial_fragment_gap(self):
        quote = "Cable\n[...]\n45 m"
        data = response("1", evidence=[proof(1, quote)])
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(data), {1: quote}, False)

    def test_decimal_equivalence_retrieves_and_validates_quantity(self):
        index = PageTextIndex(["Cable quantity 45,00 m", "Cable quantity 46 m"])
        self.assertEqual(index.support_candidates("Cable", "45.000", "m")[0], 0)
        data = response("1", "45.000", "m", True, True, evidence=[
            proof(1, "Cable"), proof(1, "45,00 m", "volume")])
        self.assertEqual(parse_audit_response(json.dumps(data), {1: index.texts[0]}, False)["status"], "ОК")
        data["found_volume"] = "45.01"
        with self.assertRaises(ValueError):
            parse_audit_response(json.dumps(data), {1: index.texts[0]}, False)

    def test_mismatching_table_is_available_to_confirmation(self):
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 5 + ["Cable quantity 40 m"])
        calls = []
        def ask(context):
            calls.append(context)
            data = response("1", evidence=[proof(1, "Cable description")])
            if len(calls) == 2:
                data.update(found_volume="40", found_unit="m", unit_confirmed=True)
                data["evidence"].append(proof(7, "40 m", "volume"))
            return parse_audit_response(json.dumps(data), context, False)
        result = audit_text_pages(index, [0], ask, "Cable", "45", unit="m")
        self.assertEqual(result["status"], "Ошибка")
        self.assertEqual(result["found_volume"], "40")
        self.assertEqual(result["found_pages"], [1, 7])

    def test_fragment_retains_table_header_and_whole_best_window(self):
        text = "Cable specification | Quantity | m\n" + "irrelevant appendix\n" * 600 + "Cable quantity 45 m"
        fragment = PageTextIndex([text]).fragment(0, "Cable", "", "", "45", 600, True)
        self.assertIn("Cable specification | Quantity | m", fragment)
        self.assertIn("Cable quantity 45 m", fragment)
        self.assertLessEqual(len(fragment), 600)
        for piece in fragment.split("\n[...]\n"):
            self.assertIn(piece, text)

    def test_recoverable_context_failure_does_not_stop_other_candidates(self):
        index = PageTextIndex(["Title", "Introduction", "Cable 45 m"])
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 1:
                result = technical_error("Malformed JSON after retries")
                result["recoverable"] = True
                return result
            return found(context, 3)
        result = audit_text_pages(index, [0], ask, "Cable", "45")
        self.assertEqual(result["found_page"], "3")
        self.assertEqual(result["contexts"], 2)
        self.assertEqual(result["status"], "Ошибка")

    def test_first_complete_find_stops_search_without_second_location(self):
        index = PageTextIndex(["Cable 40 m", "Cable 45 m"])
        calls = []
        def ask(context):
            calls.append(dict(context))
            page = next(iter(context))
            quote = context[page]
            volume = "40" if "40" in quote else "45"
            return found(context, page, volume)
        result = audit_text_pages(index, [], ask, "Cable", "45", max_pages=1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["work_found"])
        self.assertEqual(result["found_page"], str(next(iter(calls[0]))))
        self.assertEqual(result["found_pages"], [next(iter(calls[0]))])

    def test_unreadable_unreferenced_neighbor_does_not_invalidate_proven_reference(self):
        index = PageTextIndex(["Cable 45 m", ""], {1: "OCR unavailable"}, pending_ocr=[1])
        result = audit_text_pages(index, [0], lambda c: found(c, 1), "Cable", "45")
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(result["search_complete"])

    def test_long_description_cannot_consume_quantity_confirmation_context(self):
        description = "Cable description installation. " * 30
        index = PageTextIndex([description * 8 + "Unrelated appendix " * 1000 + "Quantity 45 m"])
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 1:
                data = response("1", evidence=[proof(1, description)])
            else:
                self.assertIn("Quantity 45 m", context[1])
                data = response("1", "45", "m", True, True, evidence=[
                    proof(1, "Cable description installation."),
                    proof(1, "Quantity 45 m", "volume")])
            return parse_audit_response(json.dumps(data), context, False)
        result = audit_text_pages(index, [0], ask, "Cable description installation", "45",
                                  unit="m", max_context_chars=2000)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "45")
        self.assertTrue(all(len(c[1]) <= 2000 for c in calls))

    def test_confirmation_keeps_work_away_from_page_header(self):
        text = ("Unrelated appendix.\n" * 300 + "Cable description installation. " * 100
                + "Unrelated details.\n" * 1000 + "Quantity 45 m")
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 1:
                piece = next(p for p in context[1].split("\n[...]\n")
                             if "Cable description installation." in p)
                start = max(0, piece.index("Cable description installation.") - 400)
                data = response("1", evidence=[proof(1, piece[start:start + 900])])
            else:
                self.assertIn("Cable description installation.", context[1])
                self.assertIn("Quantity 45 m", context[1])
                data = response("1", "45", "m", True, True, evidence=[
                    proof(1, "Cable description installation."),
                    proof(1, "Quantity 45 m", "volume")])
            return parse_audit_response(json.dumps(data), context, False)
        result = audit_text_pages(PageTextIndex([text]), [0], ask,
                                  "Cable description installation", "45", unit="m", max_context_chars=2000)
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(all(len(c[1]) <= 2000 for c in calls))

    def test_unlinked_confirmation_retains_original_without_revisiting_support(self):
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 5 + ["Cable 45 m"])
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 1:
                data = response("1", evidence=[proof(1, "Cable description")])
                return parse_audit_response(json.dumps(data), context, False)
            return found(context, 7)
        result = audit_text_pages(index, [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1])
        self.assertFalse(result["search_complete"])

    def test_retry_token_reserve_is_separate_from_attempt_limit(self):
        budget = RequestBudget(max_requests=6, max_total_tokens=100, max_output_tokens=10)
        budget.reserve("x" * 30)
        self.assertFalse(budget.can_retry("x" * 21))
        budget.reserve("x" * 21)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.tokens, 71)

    def test_retry_reserves_room_for_another_context(self):
        budget = RequestBudget(max_requests=3, max_total_tokens=100, max_output_tokens=10)
        budget.reserve("x")
        self.assertTrue(budget.can_retry("x"))
        budget.reserve("x")
        self.assertFalse(budget.can_retry("x"))
        budget.reserve("x")
        with self.assertRaises(BudgetExceeded):
            budget.reserve("x")


    def test_fragment_budget_includes_separators_after_trimming_a_bridge(self):
        rng = random.Random(0)
        text = "Header | quantity | unit\n" + "".join(
            (rng.choice(["Cable", "quantity", "appendix", "45", "specification"]) + " ")
            * rng.randrange(1, 30) + "\n" for _ in range(90))
        index = PageTextIndex([text])
        for prefer_volume in (False, True):
            for limit in (599, 1000, 1400, 2000, 2500):
                with self.subTest(prefer_volume=prefer_volume, limit=limit):
                    fragment = index.fragment(0, "Cable", "", "", "45", limit, prefer_volume)
                    self.assertLessEqual(len(fragment), limit)
                    for piece in fragment.split("\n[...]\n"):
                        self.assertIn(piece, text)


class BudgetTests(unittest.TestCase):
    def test_failed_attempts_are_reserved_and_capped(self):
        budget = RequestBudget(max_requests=2)
        budget.reserve("x")
        budget.reserve("x")
        with self.assertRaises(BudgetExceeded):
            budget.reserve("x")
        self.assertEqual(budget.requests, 2)

    def test_insufficient_token_budget_sends_nothing(self):
        budget = RequestBudget(max_total_tokens=2000)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("x")
        self.assertEqual(budget.requests, 0)

    def test_actual_usage_replaces_only_current_reservation(self):
        budget = RequestBudget(max_total_tokens=25, max_output_tokens=10)
        budget.reserve("x")  # failed request: its full reservation stays charged
        budget.reserve("x")
        budget.record(SimpleNamespace(total_tokens=5))
        self.assertEqual(budget.tokens, 16)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("x")


class FakeAPIError(Exception):
    status_code = 429


class AdapterTests(unittest.TestCase):
    def run_adapter(self, outputs, budget=None, texts=None):
        calls = []
        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = SimpleNamespace(create=self.create)
            def create(self, **kwargs):
                calls.append(kwargs)
                output = outputs.pop(0)
                if isinstance(output, Exception):
                    raise output
                if isinstance(output, SimpleNamespace):
                    return output
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output))],
                                       output_text=output, usage=SimpleNamespace(total_tokens=100))
            def close(self):
                pass
        namespace = app_functions(openai=SimpleNamespace(OpenAI=FakeClient, APIConnectionError=ConnectionError))
        result = namespace["audit_position_hybrid"](PageTextIndex(texts or ["Cable 45 m"]), [0],
            "Cable", "45", "m", "", "1", budget=budget)
        return result, calls

    @staticmethod
    def valid():
        return json.dumps(response("1", "45", "m", True, True,
            evidence=[{"source_id": "p1q1", "kind": kind} for kind in ("work", "volume", "unit")]))

    def test_invalid_evidence_retry_then_success(self):
        forged = json.dumps(response("1", evidence=[{"source_id": "p99q1", "kind": "work"}]))
        result, calls = self.run_adapter([forged, self.valid()])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["requests"], 2)

    def test_retry_consumes_position_budget(self):
        result, calls = self.run_adapter([FakeAPIError("quota"), self.valid()])
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["requests"], 2)

    def test_retry_budget_does_not_reset_for_next_context(self):
        negative = json.dumps(response())
        result, calls = self.run_adapter([FakeAPIError("quota"), negative, self.valid()],
            budget=RequestBudget(max_requests=2), texts=["Title"] + ["Cable 45 m"] * 10)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["found_page"], "-")

    def test_token_budget_prevents_http_attempt(self):
        result, calls = self.run_adapter([], budget=RequestBudget(max_total_tokens=1))
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(calls, [])

    def test_completed_empty_response_retains_provider_diagnostics(self):
        output = SimpleNamespace(output_text="", status="completed",
                                 usage=SimpleNamespace(total_tokens=100))
        result, calls = self.run_adapter([output] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["requests"], 3)
        self.assertIn("response_status=completed", result["discrepancy_details"])
        self.assertIn("ответ модели: <пусто>", result["discrepancy_details"])

    def test_failed_empty_response_is_not_output_budget_exhaustion(self):
        output = SimpleNamespace(output_text="", status="failed",
                                 usage=SimpleNamespace(total_tokens=0))
        result, calls = self.run_adapter([output] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertFalse(result["work_found"])
        self.assertEqual(result["requests"], 3)
        self.assertIn("response_status=failed", result["discrepancy_details"])
        self.assertIn("ответ модели: <пусто>", result["discrepancy_details"])

    def test_output_token_exhaustion_is_incomplete_not_technical_failure(self):
        output = SimpleNamespace(
            output_text="", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(total_tokens=6000))
        result, calls = self.run_adapter([output] * 3)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertFalse(result["work_found"])
        self.assertEqual(result["requests"], 3)
        self.assertIn("обрезан лимитом вывода", result["discrepancy_details"])

    def test_transport_failure_does_not_reuse_previous_truncation(self):
        truncated = SimpleNamespace(
            output_text="", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(total_tokens=6000))
        result, calls = self.run_adapter(
            [truncated, FakeAPIError("quota"), FakeAPIError("quota")])
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["requests"], 3)
        self.assertIn("FakeAPIError: quota", result["discrepancy_details"])
        self.assertIn("response_status=?", result["discrepancy_details"])

    def test_malformed_exhausted_response_is_technical_failure(self):
        result, calls = self.run_adapter(["broken"] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["requests"], 3)


if __name__ == "__main__":
    unittest.main()
