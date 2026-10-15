"""Consumer-visible audit/search boundaries; no live credentials."""

import ast
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import (BudgetExceeded, INCOMPLETE, PageTextIndex, RequestBudget,
                        TECHNICAL_ERROR, audit_text_pages, empty_result,
                        parse_audit_response, parse_page_reference, technical_error)


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
                 "time": SimpleNamespace(sleep=lambda _: None),
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

    def test_latin_and_cyrillic_statuses(self):
        for status in ("OK", "ОК", "OК", "ОK", " ok "):
            self.assertEqual(self.parse({**self.data, "status": status})["status"], "ОК")

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
                    json.dumps({**self.data, "found_page": True}), json.dumps({**self.data, "status": "success"}),
                    json.dumps({**self.data, "evidence": {}}), json.dumps({**self.data, "found_volume": []})):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_audit_response(raw, self.context)


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
                                  [0], ask, "Cable", "45", unit="m", max_requests=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "1")

    def test_same_page_confirmation_retrieves_quantity_outside_initial_fragment(self):
        calls = []
        def ask(context):
            calls.append(context)
            data = response("1", evidence=[proof(1, "Cable description")])
            if len(calls) == 1:
                self.assertNotIn("Quantity 45 m", context[1])
            else:
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
        for limits in ({"max_requests": 2}, {"max_total_chars": 300, "max_context_chars": 200}):
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
            evidence=[proof(1, "Cable 45 m", kind) for kind in ("work", "volume", "unit")]))

    def test_invalid_evidence_retry_then_success(self):
        forged = json.dumps(response("1", evidence=[proof(1, "Steel cable")]))
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

    def test_malformed_exhausted_response_is_technical_failure(self):
        result, calls = self.run_adapter(["broken"] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["requests"], 3)


if __name__ == "__main__":
    unittest.main()
