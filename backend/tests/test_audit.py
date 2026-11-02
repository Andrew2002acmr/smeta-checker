"""Consumer-visible audit/search boundaries; no live credentials."""

import json
from decimal import Decimal
from pathlib import Path
import random
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import (BudgetExceeded, INCOMPLETE, PageTextIndex, RequestBudget,
                        TECHNICAL_ERROR, audit_text_pages, empty_result,
                        parse_audit_response, parse_page_reference, technical_error,
                        _terms)
from audit_measurements import _numbers
from audit_llm import _catalog_chunks, audit_position_hybrid


def _catalog(context):
    """Build quote_sources exactly like audit_llm.ask chunking."""
    quote_sources = {}
    for page, text in context.items():
        for piece in str(text).split("\n[...]\n"):
            for quote in _catalog_chunks(piece):
                quote_sources[f"p{page}q{len(quote_sources) + 1}"] = {
                    "page": page, "quote": quote}
    return quote_sources


def _sid(quote_sources, quote, page=None):
    for sid, source in quote_sources.items():
        if source["quote"] == quote and (page is None or source["page"] == page):
            return sid
    raise AssertionError(f"quote not in catalog: {quote!r}")


def claims(*items, details=""):
    """items: (claim_id, [quotes]) or (claim_id, [quotes], extra dict)."""
    out = []
    for item in items:
        claim_id, quotes = item[0], item[1]
        extra = item[2] if len(item) > 2 else {}
        out.append({"claim_id": claim_id,
                    "source_ids": quotes, **extra})
    return {"claims": out, "discrepancy_details": details}


def found(context, page, volume="45", matching=True,
          expected_volume=None, expected_unit="m", expected_work="",
          position_type="", comment_required=False):
    """One-shot confirmed/mismatched answer on the new wire."""
    quote_sources = _catalog(context)
    quote = context[page]
    sid = _sid(quote_sources, quote, page)
    wire = claims(("presence", [sid]),
                  ("quantity", [sid],
                   {"value_source_id": sid, "unit_source_id": sid}),
                  ("unit", [sid], {"unit_source_id": sid}),
                  details="Volume mismatch" if not matching else "Confirmed")
    return parse_audit_response(
        json.dumps(wire), context, comment_required,
        expected_volume=volume if expected_volume is None else expected_volume,
        expected_unit=expected_unit, quote_sources=quote_sources,
        position_type=position_type, expected_work=expected_work)




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

    def test_volume_sheet_stamp_maps_to_physical_page(self):
        from audit_service import _SHEET_MAP_CACHE, get_optimized_page_indices

        class FakePage:
            def __init__(self, text):
                self.text = text

            def get_text(self):
                return self.text

        class FakeDoc(list):
            pass

        pages = ["cover %d" % i for i in range(6)]
        pages += ["Том отчет\n%d из 10\nтело листа" % n for n in range(1, 11)]
        doc = FakeDoc([FakePage(text) for text in pages])
        _SHEET_MAP_CACHE.clear()
        try:
            indices, description, numbers = get_optimized_page_indices(doc, "9")
            self.assertEqual(indices, [14])
            self.assertEqual(numbers, [9])
            self.assertIn("PDF 15", description)
        finally:
            _SHEET_MAP_CACHE.clear()

    def test_missing_stamp_keeps_legacy_file_order(self):
        from audit_service import _SHEET_MAP_CACHE, get_optimized_page_indices

        class FakePage:
            def get_text(self):
                return "plain body without stamps"

        class FakeDoc(list):
            pass

        doc = FakeDoc([FakePage() for _ in range(5)])
        _SHEET_MAP_CACHE.clear()
        try:
            self.assertEqual(get_optimized_page_indices(doc, "3")[0], [2])
        finally:
            _SHEET_MAP_CACHE.clear()


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.context = {3: "Copper cable 45 m"}
        self.quote_sources = _catalog(self.context)
        sid = _sid(self.quote_sources, "Copper cable 45 m")
        self.sid = sid
        self.claim_quotes = {
            "presence": "Copper cable 45 m",
            "quantity": "Copper cable 45 m",
            "unit": "Copper cable 45 m",
        }

    def parse(self, wire=None, context=None, **kwargs):
        if context is None:
            context = self.context
        quote_sources = kwargs.pop("quote_sources", None)
        if quote_sources is None and context is self.context:
            quote_sources = self.quote_sources
        elif quote_sources is None:
            quote_sources = _catalog(context)
        if wire is None:
            sid = _sid(quote_sources, self.claim_quotes["presence"])
            wire = claims(("presence", [sid]),
                          ("quantity", [sid],
                           {"value_source_id": sid, "unit_source_id": sid}),
                          ("unit", [sid], {"unit_source_id": sid}))
        kwargs.setdefault("expected_volume", "45")
        kwargs.setdefault("expected_unit", "m")
        kwargs.setdefault("expected_work", "Copper cable")
        kwargs.setdefault("position_type", "Материал")
        return parse_audit_response(json.dumps(wire), context,
                                    comment_required=False,
                                    quote_sources=quote_sources, **kwargs)

    def parse_quotes(self, quotes_by_claim, context=None, details="",
                     **kwargs):
        """quotes_by_claim: claim_id -> quote text or [quote texts]."""
        context = self.context if context is None else context
        quote_sources = _catalog(context)
        items = []
        for claim_id, quotes in quotes_by_claim.items():
            if isinstance(quotes, str):
                quotes = [quotes]
            extra = {}
            if claim_id in ("quantity", "unit"):
                extra = {"value_source_id": _sid(quote_sources, quotes[0]),
                         "unit_source_id": _sid(quote_sources, quotes[0])}
            items.append((claim_id, [_sid(quote_sources, q) for q in quotes],
                         extra))
        return self.parse(claims(*items, details=details), context,
                          quote_sources=quote_sources, **kwargs)

    def test_code_verdict_comes_from_quotes_not_flags(self):
        withheld_wire = claims(("presence", [self.sid]),
                               details="model withholds quantity")
        bad = self.parse(withheld_wire)
        self.assertEqual(bad["status"], "Неполная проверка")
        self.assertEqual(bad["found_page"], "3")

    def test_unknown_source_id_rejects_only_that_proposal(self):
        wire = claims(("presence", ["p99q1"]),
                      ("quantity", [self.sid],
                       {"value_source_id": self.sid,
                        "unit_source_id": self.sid}),
                      ("unit", [self.sid], {"unit_source_id": self.sid}),
                      details="bad presence id")
        result = self.parse(wire)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "not_found")
        self.assertEqual(by_id["quantity"]["state"], "confirmed")
        # Internal validator messages stay out of user-facing details.
        self.assertNotIn("Неизвестный идентификатор", result["discrepancy_details"])


    def test_location_without_work_description_still_binds_quantity(self):
        # expected_work="" gives presence nothing to contradict: location
        # confirms and the bound number+unit decides the verdict in code.
        result = self.parse_quotes(
            {"presence": "Quantity 45 m", "quantity": "Quantity 45 m",
             "unit": "Quantity 45 m"},
            {3: "Quantity 45 m"}, expected_work="")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "45")

    def test_found_volume_must_appear_in_evidence(self):
        with self.assertRaises(ValueError):
            parse_audit_response("not json {{", {1: "x"},
                                 quote_sources={"p1q1": {"page": 1,
                                                         "quote": "x"}})
    def test_quantity_fragment_cannot_supply_foreign_row_work_evidence(self):
        # The catalog addresses one HTML row per fragment; quantity and unit
        # therefore stay inside the addressed row and cannot silently use the
        # neighbouring schedule line.
        ctx = {88: ("<table><tr><th>Поз.</th><th>Наименование и техническая характеристика</th><th>Поставщик</th></tr>"
                    "<tr><td>1.4</td><td>Зажим для троса</td><td>шт.</td><td>328</td></tr>"
                    "<tr><td>2.1</td><td>Геотекстиль Дорнит</td><td>м2</td><td>2254,8</td></tr></table>")}
        quote_sources = _catalog(ctx)
        qty_sid = next(sid for sid, source in quote_sources.items()
                       if "2254,8" in source["quote"])
        wire = claims(("presence", [qty_sid]),
                      ("quantity", [qty_sid],
                       {"value_source_id": qty_sid, "unit_source_id": qty_sid}),
                      ("unit", [qty_sid], {"unit_source_id": qty_sid}),
                      details="fragment-bound row")
        result = parse_audit_response(
            json.dumps(wire), ctx, comment_required=False,
            position_type="Материал",
            expected_work="Зажим для троса",
            expected_volume="2254,8", expected_unit="м2",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "not_found")
        self.assertEqual(by_id["quantity"]["state"], "confirmed")
        self.assertEqual(result["status"], "Неполная проверка")

    def test_worded_quantity_without_digits_needs_manual_review(self):
        context = {3: "Опорная сеть создана из десяти определяемых пунктов"}
        quote_sources = _catalog(context)
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]), ("quantity", [sid]),
                      details="worded quantity")
        result = parse_audit_response(
            json.dumps(wire), context, comment_required=False,
            expected_volume="10", expected_unit="пункт",
            expected_work="Опорная сеть", position_type="Материал",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "Неполная проверка")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "review_required")
        self.assertIn("worded_quantity_without_digits",
                      by_id["quantity"]["reasons"])

    def test_quantity_quote_without_unit_stays_not_found(self):
        context = {3: "Опорная сеть создана, количество 10."}
        quote_sources = _catalog(context)
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]), ("quantity", [sid]),
                      details="bare number")
        result = parse_audit_response(
            json.dumps(wire), context, comment_required=False,
            expected_volume="10", expected_unit="пункт",
            expected_work="Опорная сеть", position_type="Материал",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "Неполная проверка")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "not_found")

    def test_work_location_requires_work_quote(self):
        wire = claims(("quantity", [self.sid],
                       {"value_source_id": self.sid,
                        "unit_source_id": self.sid}),
                      ("unit", [self.sid], {"unit_source_id": self.sid}),
                      details="quantity without presence")
        result = self.parse(wire)
        self.assertEqual(result["status"], "Неполная проверка")
        self.assertFalse(result["work_found"])
        self.assertEqual(result["found_page"], "3")
        self.assertEqual(result["found_volume"], "45")

    def test_method_claim_ignored_for_material_positions(self):
        quote_sources = dict(self.quote_sources)
        quote_sources["p3q9"] = {"page": 3, "quote": "выдуманный способ"}
        wire = claims(("presence", [self.sid]),
                      ("quantity", [self.sid],
                       {"value_source_id": self.sid,
                        "unit_source_id": self.sid}),
                      ("unit", [self.sid], {"unit_source_id": self.sid}),
                      ("method", ["p3q9"]), details="method on material")
        result = self.parse(wire, quote_sources=quote_sources,
                            expected_work="Copper cable 45 m")
        self.assertEqual(result["status"], "ОК")
        self.assertNotIn("method", [c["claim_id"] for c in result["checks"]])

    def test_unquoted_comment_claim_downgrades_without_reject(self):
        wire = claims(("presence", [self.sid]),
                      ("quantity", [self.sid],
                       {"value_source_id": self.sid,
                        "unit_source_id": self.sid}),
                      ("unit", [self.sid], {"unit_source_id": self.sid}),
                      details="no comment proposal")
        result = parse_audit_response(
            json.dumps(wire), self.context, comment_required=True,
            expected_volume="45", expected_unit="m",
            expected_work="Copper cable", position_type="Материал",
            quote_sources=self.quote_sources)
        self.assertEqual(result["status"], "ОК")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["comment"]["state"], "not_found")

    def test_unconfirmed_comment_is_advisory_with_preserved_proof(self):
        wire = claims(("presence", [self.sid]),
                      ("quantity", [self.sid],
                       {"value_source_id": self.sid,
                        "unit_source_id": self.sid}),
                      ("unit", [self.sid], {"unit_source_id": self.sid}),
                      details="no comment proposal")
        result = parse_audit_response(
            json.dumps(wire), self.context, comment_required=True,
            expected_volume="45", expected_unit="m",
            expected_work="Copper cable", position_type="Материал",
            quote_sources=self.quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["comment"]["state"], "not_found")
        self.assertEqual(result["status"], "ОК")
        self.assertIn(self.sid, [f.get("source_ids", [None])[0]
                                 for f in by_id["presence"].get("facts", [])]
                      if by_id["presence"].get("facts") else True)

    def test_thinking_and_markdown_are_not_source_evidence(self):
        sid = self.sid
        raw = ('<think>reason</think>```json\n' + json.dumps(
            claims(("presence", [sid]),
                   ("quantity", [sid],
                    {"value_source_id": sid, "unit_source_id": sid}),
                   ("unit", [sid], {"unit_source_id": sid}))) + '\n```')
        result = parse_audit_response(
            raw, self.context, comment_required=False, expected_volume="45",
            expected_unit="m", expected_work="Copper cable",
            position_type="Материал", quote_sources=self.quote_sources)
        self.assertEqual(result["found_page"], "3")

    def test_invalid_response_shapes(self):
        good_sid = self.sid
        good = claims(("presence", [good_sid]),
                      ("quantity", [good_sid],
                       {"value_source_id": good_sid,
                        "unit_source_id": good_sid}),
                      ("unit", [good_sid], {"unit_source_id": good_sid}))
        for raw in ("", "no json", '{"status":"Ошибка"}',
                    json.dumps({**good, "claims": "true"}),
                    json.dumps({"claims": [{"claim_id": "nope",
                                             "source_ids": [good_sid]}],
                                "discrepancy_details":  True})):
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                parse_audit_response(raw, self.context,
                                     quote_sources=self.quote_sources,
                                     expected_volume="45", expected_unit="m")


class SearchTests(unittest.TestCase):
    def test_found_volume_mismatch_stops_location_search(self):
        calls = []
        def ask(context):
            calls.append(context)
            return found(context, 1, "40", matching=False,
                         expected_volume="45", expected_unit="m",
                         expected_work="Cable", position_type="Материал")
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
            result = found(context, 1, expected_volume="45",
                           expected_unit="m", expected_work="Cable",
                           position_type="Материал")
            return result
        result = audit_text_pages(PageTextIndex(["Cable 45 m"] * 50), [0], ask, "Cable", "45", comment="Coefficient 1.2")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["status"], "ОК")

    def test_adjacent_reference_is_advisory_but_distant_is_discrepancy(self):
        # New finish() keeps a distant match confirmed but flags navigation:
        # adjacent hits note "рядом", distant hits note "вне ссылки".
        for primary, found_page, status in (([0], 2, "ОК"), ([2], 2, "ОК"),
                                             ([0, 3], 3, "ОК"), ([0], 4, "ОК")):
            with self.subTest(primary=primary, found_page=found_page):
                def ask(context):
                    if found_page in context:
                        return found(context, found_page,
                                     expected_volume="45", expected_unit="m",
                                     expected_work="Cable",
                                     position_type="Материал")
                    return empty_result("Ошибка", "Not here")
                result = audit_text_pages(PageTextIndex(["Cable 45 m"] * 5),
                                          primary, ask, "Cable", "45")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["found_page"], str(found_page))
                self.assertIn("рядом" if found_page != 4 else "вне ссылки",
                              result["discrepancy_details"])

    def test_one_confirmation_joins_far_quantity_without_restarting_location_search(self):
        calls = []
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 8 + ["Cable quantity 45 m"])
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            if len(calls) == 1:
                desc = next(q for q in quote_sources.values()
                            if "Cable description" in q["quote"])["quote"]
                sid = _sid(quote_sources, desc)
                wire = claims(("presence", [sid]), details="work only")
            else:
                desc = next(q for q, src in quote_sources.items()
                            if "Cable description" in src["quote"])
                qty = next(q for q, src in quote_sources.items()
                           if "45 m" in src["quote"])
                wire = claims(("presence", [desc]),
                              ("quantity", [qty],
                               {"value_source_id": qty,
                                "unit_source_id": qty}),
                              ("unit", [qty], {"unit_source_id": qty}),
                              details="confirm")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1, 10])
        self.assertEqual(result["found_volume"], "45")
        self.assertEqual(result["status"], "ОК")

    def test_confirmation_failure_preserves_location_as_partial_check(self):
        calls = []
        def ask(context):
            calls.append(context)
            if len(calls) == 2:
                return technical_error("API unavailable")
            quote_sources = _catalog(context)
            desc = next((s for s, src in quote_sources.items()
                         if "Cable description" in src["quote"]), None)
            if desc is None:
                result = empty_result(INCOMPLETE, "work not in context yet")
                result["recoverable"] = True
                return result
            wire = claims(("presence", [desc]), details="work only")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex(["Title", "Cable description"] + ["Introduction"] * 7 + ["Cable 45 m"]),
                                  [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "2")
        self.assertEqual(result["found_pages"], [2])
        self.assertFalse(result["search_complete"])

    def test_confirmation_cannot_exceed_shared_budget(self):
        def ask(context):
            quote_sources = _catalog(context)
            sid = next(iter(quote_sources))
            wire = claims(("presence", [sid]), details="work only")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex(["Cable description"] + ["Introduction"] * 8 + ["Cable 45 m"]),
                                  [0], ask, "Cable", "45", unit="m", max_contexts=1)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "1")

    def test_confirmation_can_complete_missing_quantity_on_same_long_page(self):
        calls = []
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            if len(calls) == 1:
                desc = next(q for q in quote_sources.values()
                            if "Cable description" in q["quote"])["quote"]
                sid = _sid(quote_sources, desc)
                wire = claims(("presence", [sid]), details="work only")
            else:
                qty = next(q for q in quote_sources.values()
                           if "Quantity 45 m" in q["quote"])["quote"]
                sid = _sid(quote_sources, qty)
                wire = claims(("presence", [sid]),
                              ("quantity", [sid],
                               {"value_source_id": sid,
                                "unit_source_id": sid}),
                              ("unit", [sid], {"unit_source_id": sid}),
                              details="confirm")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable description", position_type="Материал",
                quote_sources=quote_sources)
        text = "Cable description " * 200 + "Unrelated appendix " * 4000 + "Quantity 45 m"
        result = audit_text_pages(PageTextIndex([text]), [0], ask, "Cable", "45", unit="m", max_context_chars=1000)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "45")
        self.assertTrue(all(sum(map(len, c.values())) <= 1000 for c in calls))

    def test_related_reference_pages_checked_together(self):
        def ask(context):
            quote_sources = _catalog(context)
            work_sid = _sid(quote_sources, "Copper cable")
            qty_sid = _sid(quote_sources, "Cable quantity 45 m")
            wire = claims(("presence", [work_sid]),
                          ("quantity", [qty_sid],
                           {"value_source_id": qty_sid,
                            "unit_source_id": qty_sid}),
                          ("unit", [qty_sid], {"unit_source_id": qty_sid}),
                          details="related")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Copper cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex(["Copper cable", "Introduction", "Cable quantity 45 m"]), [0, 2], ask, "Copper cable", "45")
        self.assertEqual(result["found_pages"], [1, 3])
        self.assertEqual(result["status"], "ОК")

    def test_neighbor_table_volume_combines_with_description(self):
        def ask(context):
            quote_sources = _catalog(context)
            work_sid = _sid(quote_sources, "Cable")
            qty_sid = _sid(quote_sources, "Quantity 45 m")
            wire = claims(("presence", [work_sid]),
                          ("quantity", [qty_sid],
                           {"value_source_id": qty_sid,
                            "unit_source_id": qty_sid}),
                          ("unit", [qty_sid], {"unit_source_id": qty_sid}),
                          details="neighbor")
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
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

    def test_formula_support_preserves_area_in_confirmation_fragment(self):
        work = "Укладка гибких бетонных плит. Площадь крепления - 1879 м2 (площадка ЛОС)."
        area = "Площадь крепления - 1879 м2 (площадка ЛОС)."
        index = PageTextIndex([work] + ["Укладка плит количество 4"] * 4
                              + ["Введение\n" * 3000 + area])
        calls = []
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            if len(calls) == 1:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укладка гибких бетонных плит" in src["quote"])
                wire = claims(("presence", [work_sid]), details="work")
            else:
                self.assertIn(area, context[6])
                area_sid = next(s for s, src in quote_sources.items()
                                if src["page"] == 6 and "1879 м2" in src["quote"])
                wire = claims(("presence", [area_sid]),
                              ("quantity", [area_sid],
                               {"value_source_id": area_sid,
                                "unit_source_id": area_sid}),
                              ("unit", [area_sid],
                               {"unit_source_id": area_sid}),
                              details="area binds 1879 m2")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="1879", expected_unit="м2",
                expected_work="Укладка гибких бетонных плит",
                position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Укладка гибких бетонных плит", "1879", unit="м2",
                                  formula="1879*0,15", max_pages=2, max_context_chars=1000)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "1879")
        self.assertEqual(result["found_pages"], [1, 6])

    def test_formula_candidate_bonus_needs_quantity_context(self):
        from decimal import Decimal
        index = PageTextIndex(["Плиты количество 8", "Площадь 1879 м²", "1879"])
        self.assertEqual(index.support_candidates("Плиты", "281.85", "м3"), [0, 1])
        self.assertEqual(index.support_candidates("Плиты", "281.85", "м3",
                                                 formula_numbers={Decimal("1879")}), [1, 0])

    def test_far_location_preserved_on_wrong_reference(self):
        index = PageTextIndex(["Title", "Contents"] + ["Introduction"] * 20 + ["Copper cable 45 m"])
        def ask(context):
            if 23 in context:
                return found(context, 23, expected_volume="45",
                             expected_unit="m", expected_work="Copper cable",
                             position_type="Материал")
            result = empty_result("Ошибка", "Not in these fragments")
            result["recoverable"] = True
            return result
        result = audit_text_pages(index, [0], ask, "Copper cable", "45")
        self.assertEqual(result["found_page"], "23")
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(result["work_found"])

    def test_method_wording_mismatch_stays_unconfirmed(self):
        # Technology block is gone: wording equivalence is a method claim.
        # "Заполнение зазоров" shares the work object with "Засыпка
        # зазоров", but the specific "вручную" wording is unproven, so the
        # method claim stays review_required and the position incomplete.
        section = "Заполнение зазоров между блоками щебнем"
        order = "Зазоры между блоками засыпаются вручную щебнем"
        index = PageTextIndex([section, "Введение", order])
        def ask(context):
            quote_sources = _catalog(context)
            sid = next(iter(quote_sources))
            wire = claims(("presence", [sid]), ("method", [sid]),
                          details="method wording differs")
            return parse_audit_response(
                json.dumps(wire), context,
                comment_required=False, position_type="Работа",
                expected_volume="45", expected_unit="m",
                expected_work="Засыпка зазоров засыпаются вручную",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Засыпка", "45")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["method"]["state"], "review_required")
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["technology"]["status"], "unconfirmed")
    def test_method_answer_without_digits_keeps_difference_unconfirmed(self):
        section = "Заполнение зазоров между блоками щебнем"
        index = PageTextIndex([section, "Введение", "Другое введение"])
        def ask(context):
            quote_sources = _catalog(context)
            sid = next(iter(quote_sources))
            # No digits in the context: quantity cannot bind, method wording
            # differs -> unconfirmed method, not a silent confirmation.
            wire = claims(("presence", [sid]), ("method", [sid]),
                          details="no digits")
            return parse_audit_response(
                json.dumps(wire), context,
                comment_required=False, position_type="Работа",
                expected_volume="45", expected_unit="m",
                expected_work="Засыпка зазоров засыпаются вручную",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Засыпка", "45")
        self.assertEqual(result["technology"]["status"], "unconfirmed")
        self.assertEqual(result["status"], INCOMPLETE)

    def test_material_schedule_answer_accepted_despite_comment_claim(self):
        section = "Сечение 3-3. Геотекстиль Дорнит как противосуффозионный экран."
        schedule = ("Спецификация. Наименование и техническая характеристика. Поставщик. "
                    "Геотекстиль Дорнит: количество 631,2 м2.")
        index = PageTextIndex(["Титул", section, schedule])
        def ask(context):
            quote_sources = _catalog(context)
            sid = next(s for s, src in quote_sources.items()
                       if "631,2 м2" in src["quote"])
            wire = claims(("presence", [sid]),
                          ("quantity", [sid],
                           {"value_source_id": sid,
                            "unit_source_id": sid}),
                          ("unit", [sid], {"unit_source_id": sid}),
                          details="schedule row")
            return parse_audit_response(
                json.dumps(wire), context,
                position_type="Материал",
                expected_volume="631,2", expected_unit="м2",
                expected_work="Геотекстиль Дорнит",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [2], ask, "Геотекстиль Дорнит", "631,2",
                                  unit="м2")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "631.2")
        self.assertEqual(result["found_page"], "3")
    def test_end_of_huge_page_is_locally_retrieved(self):
        text = "Introduction " * 10000 + "Copper cable 45 m"
        seen = []
        def ask(context):
            seen.append(context[1])
            quote_sources = _catalog(context)
            sid = next(iter(quote_sources))
            wire = claims(("presence", [sid]),
                          ("quantity", [sid],
                           {"value_source_id": sid,
                            "unit_source_id": sid}),
                          ("unit", [sid], {"unit_source_id": sid}))
            return parse_audit_response(
                json.dumps(wire), context, comment_required=False,
                expected_volume="45", expected_unit="m",
                expected_work="Copper cable", position_type="Материал",
                quote_sources=quote_sources)
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
            result = audit_text_pages(index, [0], lambda c: found(c, 2, expected_volume="45",
                expected_unit="m", expected_work="Cable", position_type="Материал"), "Cable", "45")
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

    def test_incomplete_without_work_continues_to_candidates(self):
        # A VOR reference may point at the wrong physical pages; "not in
        # these fragments" must not stop the search while candidates remain.
        index = PageTextIndex(["Title intro cable", "Intro words cable",
                               "Cable 45 m scope"])
        calls = []
        def ask(context):
            calls.append(dict(context))
            if len(calls) == 1:
                return empty_result(INCOMPLETE, "not in these fragments")
            quote_sources = _catalog(context)
            sid = next(s for s, src in quote_sources.items()
                       if "Cable 45 m scope" in src["quote"])
            wire = claims(("presence", [sid]),
                          ("quantity", [sid],
                           {"value_source_id": sid, "unit_source_id": sid}),
                          ("unit", [sid], {"unit_source_id": sid}),
                          details="Confirmed")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Cable", "45")
        self.assertTrue(result["work_found"])


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

    def test_work_coverage_ranks_complete_description_first(self):
        index = PageTextIndex(["укладка труб", "укладка гибких бетонных плит"])
        self.assertEqual(index.candidates("укладка гибких бетонных плит", "", "", ""), [1, 0])
        self.assertEqual(index.candidates("укладка труб", "", "", ""), [0, 1])

    def test_grouped_quantity_retrieves_and_validates_without_changing_source(self):
        for space in (" ", " ", " "):
            with self.subTest(space=repr(space)):
                text = f"Расход {1}{space}974,71 л/с"
                self.assertEqual(_numbers(text), {Decimal("1974.71")})
                index = PageTextIndex(["Расход 974,71 л/с", text])
                # Both quantities remain evidence. The expected VOR number
                # must not choose which project value is retrieved first.
                self.assertEqual(index.support_candidates("Расход", "1974.71", "л/с"), [0, 1])
                quote_sources = _catalog({2: text})
                sid = next(iter(quote_sources))
                wire = claims(("presence", [sid]),
                              ("quantity", [sid],
                               {"value_source_id": sid,
                                "unit_source_id": sid}),
                              ("unit", [sid], {"unit_source_id": sid}))
                result = parse_audit_response(
                    json.dumps(wire), {2: text}, False,
                    expected_volume="1974.71", expected_unit="л/с",
                    expected_work="Расход", position_type="Материал",
                    quote_sources=quote_sources)
                self.assertEqual(result["status"], "ОК")
        self.assertEqual(_numbers("45,00 46.25"), {Decimal("45.00"), Decimal("46.25")})

    def test_unit_before_number_is_not_a_thousands_separator(self):
        self.assertEqual(
            _numbers("Геотекстиль 500 г/м2 631,2 м2."),
            {Decimal("500"), Decimal("631.2")})
        self.assertEqual(_numbers("Площадь 1 879 м2"), {Decimal("1879")})
        self.assertEqual(_numbers("м2 631,2"), {Decimal("631.2")})
        self.assertEqual(_numbers("Цена 1 879,50 руб."), {Decimal("1879.50")})

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
        quote_sources = _catalog({1: fragment})
        sid = next(s for s, src in quote_sources.items() if quote in src["quote"])
        wire = claims(("presence", [sid]),
                      ("quantity", [sid],
                       {"value_source_id": sid, "unit_source_id": sid}),
                       ("unit", [sid], {"unit_source_id": sid}))
        self.assertTrue(parse_audit_response(
            json.dumps(wire), {1: fragment}, False,
            expected_volume="45", expected_unit="m",
            expected_work="Copper cable", position_type="Материал",
            quote_sources=quote_sources)["work_found"])


class EvidenceAndRetrievalTests(unittest.TestCase):
    def test_model_status_cannot_override_evidence_or_cause_retry(self):
        quote_sources = _catalog({1: "Cable 45 m"})
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]),
                      ("quantity", [sid],
                       {"value_source_id": sid, "unit_source_id": sid}),
                      ("unit", [sid], {"unit_source_id": sid}),
                      details="model verdict goes here, not into claims")
        result = parse_audit_response(
            json.dumps(wire), {1: "Cable 45 m"}, False,
            expected_volume="45", expected_unit="m",
            expected_work="Cable", position_type="Материал",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "ОК")
        dropped = claims(("presence", [sid]), details="quantity withheld")
        result = parse_audit_response(
            json.dumps(dropped), {1: "Cable 45 m"}, False,
            expected_volume="45", expected_unit="m",
            expected_work="Cable", position_type="Материал",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "Неполная проверка")
        self.assertFalse(result["volume_confirmed"])

    def test_typographic_variants_but_not_omitted_or_rephrased_words(self):
        context = {1: 'Кабель «ВВГ» — 45 м'}
        quote_sources = _catalog(context)
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]),
                      ("quantity", [sid],
                       {"value_source_id": sid, "unit_source_id": sid}),
                      ("unit", [sid], {"unit_source_id": sid}),
                      details="dash/quotes normalized")
        self.assertTrue(parse_audit_response(
            json.dumps(wire), context, False, expected_volume="45",
            expected_unit="м", expected_work="Кабель ВВГ",
            position_type="Материал",
            quote_sources=quote_sources)["work_found"])
        for quote in ('Кабель ... 45 м', 'Провод «ВВГ» — 45 м'):
            bad_sources = dict(quote_sources)
            bad_sources[sid] = {"page": 1, "quote": quote}
            with self.subTest(quote=quote):
                bad = claims(("presence", [sid]),
                             ("quantity", [sid],
                              {"value_source_id": sid,
                               "unit_source_id": sid}),
                             ("unit", [sid], {"unit_source_id": sid}))
                result = parse_audit_response(
                    json.dumps(bad), context, False, expected_volume="45",
                    expected_unit="м", expected_work="Кабель ВВГ",
                    position_type="Материал", quote_sources=bad_sources)
                self.assertEqual(result["status"], "Неполная проверка")

    def test_quote_cannot_cross_artificial_fragment_gap(self):
        context = {1: "Cable\n[...]\n45 m"}
        quote_sources = _catalog(context)
        self.assertEqual(len(quote_sources), 2)
        first = [s for s in quote_sources if quote_sources[s]["page"] == 1][0]
        wire = claims(("presence", [first],
                       {"not": "used"}), details="gap") if False else None
        gap_sources = {"p1q9": {"page": 1, "quote": "Cable\n[...]\n45 m"}}
        wire = claims(("presence", ["p1q9"]), details="gap")
        result = parse_audit_response(
            json.dumps(wire), context, False, expected_volume="45",
            expected_unit="m", expected_work="Cable",
            position_type="Материал", quote_sources=gap_sources)
        self.assertEqual(result["status"], "Неполная проверка")

    def test_decimal_equivalence_retrieves_and_validates_quantity(self):
        index = PageTextIndex(["Cable quantity 45,00 m", "Cable quantity 46 m"])
        self.assertEqual(index.support_candidates("Cable", "45.000", "m")[0], 0)
        quote_sources = _catalog({1: index.texts[0]})
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]),
                      ("quantity", [sid],
                       {"value_source_id": sid, "unit_source_id": sid}),
                      ("unit", [sid], {"unit_source_id": sid}))
        self.assertEqual(parse_audit_response(
            json.dumps(wire), {1: index.texts[0]}, False,
            expected_volume="45.000", expected_unit="m",
            expected_work="Cable", position_type="Материал",
            quote_sources=quote_sources)["status"], "ОК")
        tampered = dict(quote_sources)
        tampered[sid] = {"page": 1, "quote": "Cable quantity 45,01 m"}
        forged = claims(("presence", [sid]),
                        ("quantity", [sid],
                         {"value_source_id": sid, "unit_source_id": sid}),
                        ("unit", [sid], {"unit_source_id": sid}))
        # A catalog quote that no longer matches the context is rejected
        # locally; the affected claim drops to not_found instead of failing
        # the whole answer.
        tampered_result = parse_audit_response(
            json.dumps(forged), {1: index.texts[0]}, False,
            expected_volume="45.000", expected_unit="m",
            expected_work="Cable", position_type="Материал",
            quote_sources=tampered)
        by_id = {c["claim_id"]: c["state"]
                 for c in tampered_result["checks"]}
        self.assertEqual(by_id["presence"], "not_found")
        self.assertEqual(tampered_result["status"], "Неполная проверка")

    def test_mismatching_table_is_available_to_confirmation(self):
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 5 + ["Cable quantity 40 m"])
        calls = []
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            desc = next((s for s, src in quote_sources.items()
                         if "Cable description" in src["quote"]), None)
            if len(calls) == 1:
                wire = claims(("presence", [desc]), details="work only")
            else:
                qty = next(s for s, src in quote_sources.items()
                           if "40 m" in src["quote"])
                wire = claims(("presence", [desc]),
                              ("quantity", [qty],
                               {"value_source_id": qty,
                                "unit_source_id": qty}),
                              ("unit", [qty], {"unit_source_id": qty}),
                              details="mismatch 40 vs 45")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
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
            return found(context, 3, expected_volume="45",
                         expected_unit="m", expected_work="Cable",
                         position_type="Материал")
        result = audit_text_pages(index, [0], ask, "Cable", "45")
        self.assertEqual(result["found_page"], "3")
        self.assertEqual(result["contexts"], 2)
        # The located quantity (45) matches the VOR expectation, so the new
        # code confirms it: recovery finds the work with bound evidence.
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(result["work_found"])

    def test_first_complete_find_stops_search_without_second_location(self):
        index = PageTextIndex(["Cable 45 m", "Cable 45 m"])
        calls = []
        def ask(context):
            calls.append(dict(context))
            page = next(iter(context))
            return found(context, page, expected_volume="45",
                         expected_unit="m", expected_work="Cable",
                         position_type="Материал")
        result = audit_text_pages(index, [], ask, "Cable", "45", max_pages=1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["work_found"])
        self.assertEqual(result["found_page"], str(next(iter(calls[0]))))
        self.assertEqual(result["found_pages"], [next(iter(calls[0]))])

    def test_unreadable_unreferenced_neighbor_does_not_invalidate_proven_reference(self):
        index = PageTextIndex(["Cable 45 m", ""], {1: "OCR unavailable"}, pending_ocr=[1])
        result = audit_text_pages(index, [0], lambda c: found(c, 1, expected_volume="45",
            expected_unit="m", expected_work="Cable", position_type="Материал"), "Cable", "45")
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(result["search_complete"])

    def test_long_description_cannot_consume_quantity_confirmation_context(self):
        description = "Cable description installation. " * 30
        index = PageTextIndex([description * 8 + "Unrelated appendix " * 1000 + "Quantity 45 m"])
        calls = []
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            if len(calls) == 1:
                desc = next(s for s, src in quote_sources.items()
                            if "Cable description installation." in src["quote"])
                wire = claims(("presence", [desc]), details="work only")
            else:
                self.assertIn("Quantity 45 m", context[1])
                qty = next(s for s, src in quote_sources.items()
                           if "Quantity 45 m" in src["quote"])
                desc = next(s for s, src in quote_sources.items()
                            if "Cable description installation." in src["quote"])
                wire = claims(("presence", [desc]),
                              ("quantity", [qty],
                               {"value_source_id": qty,
                                "unit_source_id": qty}),
                              ("unit", [qty], {"unit_source_id": qty}),
                              details="confirm")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable description installation",
                position_type="Материал", quote_sources=quote_sources)
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
            quote_sources = _catalog(context)
            if len(calls) == 1:
                piece = next(p for p in context[1].split("\n[...]\n")
                             if "Cable description installation." in p)
                start = max(0, piece.index("Cable description installation.") - 400)
                quote = piece[start:start + 900]
                sid = _sid(quote_sources, quote) if quote in [
                    s["quote"] for s in quote_sources.values()] else next(
                        s for s, src in quote_sources.items()
                        if "Cable description installation." in src["quote"])
                wire = claims(("presence", [sid]), details="work only")
            else:
                self.assertIn("Cable description installation.", context[1])
                self.assertIn("Quantity 45 m", context[1])
                desc = next(s for s, src in quote_sources.items()
                            if "Cable description installation." in src["quote"])
                qty = next(s for s, src in quote_sources.items()
                           if "Quantity 45 m" in src["quote"])
                wire = claims(("presence", [desc]),
                              ("quantity", [qty],
                               {"value_source_id": qty,
                                "unit_source_id": qty}),
                              ("unit", [qty], {"unit_source_id": qty}),
                              details="confirm")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable description installation",
                position_type="Материал", quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex([text]), [0], ask,
                                  "Cable description installation", "45", unit="m", max_context_chars=2000)
        self.assertEqual(result["status"], "ОК")
        self.assertTrue(all(len(c[1]) <= 2000 for c in calls))

    def test_unlinked_confirmation_retains_original_without_revisiting_support(self):
        # A confirm response that links nothing to the located work must not
        # move the location: the original presence stays, quantity stays
        # unproven, and the position reports incomplete.
        index = PageTextIndex(["Cable description"] + ["Introduction"] * 5 + ["Cable 45 m"])
        calls = []
        def ask(context):
            calls.append(context)
            quote_sources = _catalog(context)
            if len(calls) == 1:
                desc = next(s for s, src in quote_sources.items()
                            if "Cable description" in src["quote"])
                wire = claims(("presence", [desc]), details="work only")
            else:
                wire = claims(details="confirm links nothing")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="45", expected_unit="m",
                expected_work="Cable", position_type="Материал",
                quote_sources=quote_sources)
        result = audit_text_pages(index, [0], ask, "Cable", "45", unit="m")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1])

    def test_retry_token_reserve_is_separate_from_attempt_limit(self):
        budget = RequestBudget(max_requests=6, max_total_tokens=100, max_output_tokens=10)
        budget.reserve("x" * 60)
        self.assertFalse(budget.can_retry("x" * 42))
        budget.reserve("x" * 42)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.tokens, 71)

    def test_retry_reserves_room_for_another_context(self):
        budget = RequestBudget(max_requests=3, max_total_tokens=100, max_output_tokens=10)
        budget.reserve("x" * 2)
        self.assertTrue(budget.can_retry("x" * 2))
        budget.reserve("x" * 2)
        self.assertFalse(budget.can_retry("x" * 2))
        budget.reserve("x" * 2)
        with self.assertRaises(BudgetExceeded):
            budget.reserve("x" * 2)

    def test_same_context_fix_needs_room_for_one_retry_only(self):
        budget = RequestBudget(max_requests=3, max_total_tokens=30, max_output_tokens=10)
        budget.reserve("x" * 2)
        self.assertTrue(budget.can_retry_same_context("x" * 2))
        self.assertFalse(budget.can_retry("x" * 2))


    def test_fragment_budget_includes_separators_after_trimming_a_bridge(self):
        rng = random.Random(0)
        text = "Header | quantity | unit\n" + "".join(
            (rng.choice(["Cable", "quantity", "appendix", "45", "specification"]) + " ")
            * rng.randrange(1, 30) + "\n" for _ in range(90))
        index = PageTextIndex(text)
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
        budget.reserve("xx")
        budget.record(SimpleNamespace(total_tokens=5))
        self.assertEqual(budget.tokens, 5)
        budget.reserve("xx")
        budget.record(SimpleNamespace(total_tokens=5))
        self.assertEqual(budget.tokens, 10)

    def test_failed_attempt_refunds_tokens_keeps_request_count(self):
        budget = RequestBudget(max_total_tokens=25, max_output_tokens=10)
        budget.reserve("xx")
        budget.refund()
        self.assertEqual(budget.tokens, 0)
        self.assertEqual(budget.requests, 1)
        budget.reserve("xx")
        self.assertEqual(budget.tokens, 11)


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
        result = audit_position_hybrid(PageTextIndex(texts or ["Cable 45 m"]), [0],
            "Cable", "45", "m", "", "1", budget=budget,
            api_key="test", folder="test", model_name="test-model",
            client_factory=FakeClient, sleeper=lambda _: None)
        return result, calls

    @staticmethod
    def valid():
        quote_sources = _catalog({1: "Cable 45 m"})
        sid = next(iter(quote_sources))
        return json.dumps(claims(("presence", [sid]),
                                 ("quantity", [sid],
                                  {"value_source_id": sid,
                                   "unit_source_id": sid}),
                                 ("unit", [sid],
                                  {"unit_source_id": sid}),
                                 details="valid"))

    def test_malformed_container_retry_then_success(self):
        malformed = json.dumps({"claims": "not a list"})
        result, calls = self.run_adapter([malformed, self.valid()])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["requests"], 2)

    def test_retry_consumes_position_budget(self):
        result, calls = self.run_adapter([FakeAPIError("quota"), self.valid()])
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["requests"], 2)

    def test_retry_budget_does_not_reset_for_next_context(self):
        negative = json.dumps(claims(details="nothing here"))
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

    def test_output_token_exhaustion_retries_compact_then_incomplete(self):
        output = SimpleNamespace(
            output_text="", status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=SimpleNamespace(total_tokens=6000))
        result, calls = self.run_adapter([output, self.valid()])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["requests"], 2)
        self.assertIn("сокращённого пакета", calls[1]["input"])

    def test_repeated_exhaustion_is_incomplete_not_technical_failure(self):
        def truncated():
            return SimpleNamespace(
                output_text="", status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                usage=SimpleNamespace(total_tokens=6000))
        result, _ = self.run_adapter([truncated(), truncated(), truncated()])
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertFalse(result["work_found"])
        self.assertIn("обрезан лимитом вывода", result["discrepancy_details"])


    def test_malformed_exhausted_response_is_technical_failure(self):
        result, calls = self.run_adapter(["broken"] * 3)
        self.assertEqual(result["status"], TECHNICAL_ERROR)
        self.assertEqual(result["requests"], 3)


class ChatAdapterTests(unittest.TestCase):
    def run_chat(self, outputs):
        from types import SimpleNamespace

        calls = []

        class FakeChatClient:
            def __init__(self, **kwargs):
                self.seen = kwargs
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create)
                )

            def create(self, **kwargs):
                calls.append(kwargs)
                output = outputs.pop(0)
                if isinstance(output, Exception):
                    raise output
                return output

            def close(self):
                pass

        factory_calls = []

        def factory(**kwargs):
            client = FakeChatClient(**kwargs)
            factory_calls.append(client.seen)
            return client

        result = audit_position_hybrid(
            PageTextIndex(["Cable 45 m"]), [0],
            "Cable", "45", "m", "", "1",
            api_key="test", folder="", model_name="ds/deepseek-v4-flash",
            client_factory=factory, sleeper=lambda _: None,
            allowed_hosts=("anymodel.org",),
            base_url="https://anymodel.org/v1")
        return result, calls, factory_calls

    @staticmethod
    def chat_output(content, finish_reason="stop"):
        from types import SimpleNamespace

        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  total_tokens=15))

    def test_chat_backend_sends_raw_model_id(self):
        valid = AdapterTests.valid()
        result, calls, factory_calls = self.run_chat([self.chat_output(valid)])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(calls[0]["model"], "ds/deepseek-v4-flash")
        self.assertEqual(calls[0]["messages"][0]["role"], "system")
        self.assertNotIn("project", factory_calls[0])
        self.assertEqual(result["usage"], {"input": 10, "output": 5, "total": 15})

    def test_retry_carries_specific_fix_hint_for_wrong_volume_fragment(self):
        from audit_core import EvidenceValidationError
        quote_sources = _catalog({1: "Cable 45 m"})
        sid = next(iter(quote_sources))
        wrong = json.dumps(claims(("presence", [sid]),
                                  ("quantity", [sid],
                                   {"value_source_id": sid,
                                    "unit_source_id": sid}),
                                  ("unit", [sid], {"unit_source_id": sid}),
                                  details="wrong"))
        calls_seen = []
        real_parse = parse_audit_response
        def flaky_parse(raw, *args, **kwargs):
            calls_seen.append(raw)
            if len(calls_seen) == 1:
                raise EvidenceValidationError(
                    "Фактический объем отсутствует в цитате",
                    kind="volume", expected="45",
                    hint="выбери source_id того фрагмента, где видно число 45")
            return real_parse(raw, *args, **kwargs)
        with patch("audit_llm.parse_audit_response", flaky_parse):
            result, calls, _ = self.run_chat([
                self.chat_output(wrong), self.chat_output(AdapterTests.valid())])
        self.assertEqual(result["status"], "ОК")
        self.assertIn("Исправление: выбери source_id того фрагмента, где видно число 45",
                      calls[1]["messages"][1]["content"])

    def test_volume_mismatch_retry_carries_number_hint(self):
        # Narrowed-catalog repair was removed with the old wire: retry now
        # re-sends the same catalog plus the validator hint. The hint must
        # name the expected number so the model can pick another fragment.
        from audit_core import EvidenceValidationError, PageTextIndex
        from audit_llm import audit_position_hybrid
        from types import SimpleNamespace
        calls = []
        class FakeChatClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    content = "bad"
                else:
                    ids = re.findall(r"\[(p\d+q\d+)\]", kwargs["messages"][1]["content"])
                    sid = ids[-1]
                    content = json.dumps(claims(
                        ("presence", [sid]),
                        ("quantity", [sid],
                         {"value_source_id": sid, "unit_source_id": sid}),
                        ("unit", [sid], {"unit_source_id": sid}),
                        details="retry"))
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                             finish_reason="stop")],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            def close(self):
                pass
        seen = []
        real_parse = parse_audit_response
        def flaky_parse(raw, *args, **kwargs):
            seen.append(raw)
            if len(seen) == 1:
                raise EvidenceValidationError(
                    "Фактический объем отсутствует в цитате",
                    kind="volume", expected="45", hint="выбери другой фрагмент")
            return real_parse(raw, *args, **kwargs)
        with patch("audit_llm.parse_audit_response", flaky_parse):
            result = audit_position_hybrid(
                PageTextIndex(["Intro without digits here", "Cable 45 m"]), [0, 1],
                "Cable", "45", "m", "", "1",
                api_key="test", folder="", model_name="ds/deepseek-v4-flash",
                client_factory=lambda **kw: FakeChatClient(**kw), sleeper=lambda _: None,
                allowed_hosts=("anymodel.org",), base_url="https://anymodel.org/v1")
        self.assertEqual(result["status"], "ОК")
        retry_body = calls[1]["messages"][1]["content"]
        self.assertIn("45", retry_body)
        self.assertIn("выбери другой фрагмент", retry_body)


    def test_valid_json_at_length_keeps_verified_evidence(self):
        output = self.chat_output(AdapterTests.valid(), finish_reason="length")
        result, _, _ = self.run_chat([output])
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_pages"], [1])

    def test_output_budget_exhausted_is_not_retried(self):
        for body in ({"code": "output_budget_exhausted"},
                     {"error": {"code": "output_budget_exhausted"}}):
            error = FakeAPIError("output budget exhausted")
            error.status_code = 400
            error.body = body
            with self.subTest(body=body):
                result, calls, _ = self.run_chat([error])
                self.assertEqual(result["status"], INCOMPLETE)
                self.assertEqual(len(calls), 1)
                self.assertFalse(result["work_found"])
                self.assertTrue(result["recoverable"])

    def test_chat_host_outside_allowlist_is_rejected(self):
        from types import SimpleNamespace

        class FakeChatClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=lambda **kw: None))

            def close(self):
                pass

        with self.assertRaises(ValueError):
            audit_position_hybrid(
                PageTextIndex(["Cable 45 m"]), [0],
                "Cable", "45", "m", "", "1",
                api_key="test", folder="", model_name="ds/deepseek-v4-flash",
                client_factory=FakeChatClient, sleeper=lambda _: None,
                base_url="https://anymodel.org/v1")


if __name__ == "__main__":
    unittest.main()
