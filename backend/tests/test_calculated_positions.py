"""Operation, technology and material quantity must not be conflated."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import INCOMPLETE, PageTextIndex, audit_text_pages, parse_audit_response
from test_audit import _catalog, _sid, claims
from audit_llm import _catalog_chunks


def _parse(context, wire, expected_work, expected_volume="328",
           expected_unit="шт.", position_type="Работа", **kwargs):
    quote_sources = _catalog(context)
    return parse_audit_response(
        json.dumps(wire), context, False,
        position_type=position_type, expected_work=expected_work,
        expected_volume=expected_volume, expected_unit=expected_unit,
        quote_sources=quote_sources, **kwargs)


class OperationEvidenceTests(unittest.TestCase):
    POSITION = ("Укладка гибких бетонных плит ПБЗГУ-405 на геотекстиль\n1879 м2, толщина плит 150 мм")
    WORK = "Скрепление плит соединением талрепными"

    def setUp(self):
        self.context = {1: "Монтаж плит с соединением отдельных плит талрепными стяжками.",
                        5: "Спецификация. Наименование и техническая характеристика. Поставщик. Количество. Зажим 328 шт."}
        quote_sources = _catalog(self.context)
        self.work_sid = _sid(quote_sources, self.context[1], 1)
        self.qty_sid = next(s for s, src in quote_sources.items()
                            if "328 шт" in src["quote"])
        self.quote_sources = quote_sources

    def wire(self, method=True, work_sid=None, qty_sid=None, details=""):
        work_sid = work_sid or self.work_sid
        qty_sid = qty_sid or self.qty_sid
        items = [("presence", [work_sid]),
                 ("quantity", [qty_sid],
                  {"value_source_id": qty_sid, "unit_source_id": qty_sid}),
                 ("unit", [qty_sid], {"unit_source_id": qty_sid})]
        if method:
            items.append(("method", [work_sid]))
        return claims(*items, details=details)

    def parse(self, wire=None, context=None, **kwargs):
        context = self.context if context is None else context
        quote_sources = _catalog(context)
        kwargs.setdefault("expected_work", self.WORK)
        kwargs.setdefault("expected_volume", "328")
        kwargs.setdefault("expected_unit", "шт.")
        kwargs.setdefault("position_type", "Работа")
        return parse_audit_response(json.dumps(wire or self.wire()),
                                    context, False,
                                    quote_sources=quote_sources, **kwargs)

    def test_operation_location_is_not_the_material_quantity_page(self):
        result = self.parse(expected_work="Монтаж плит с соединением отдельных плит талрепными стяжками")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["found_pages"], [1, 5])
        self.assertEqual(result["quantity_basis"], "direct")

    def test_material_specification_cannot_be_the_only_work_proof(self):
        wire = self.wire(work_sid=self.qty_sid, method=False)
        result = self.parse(wire, expected_work="Скрепление плит",
                            position_type="Материал")
        self.assertEqual(result["status"], "Неполная проверка")
        by_id = {c["claim_id"]: c["state"] for c in result["checks"]}
        self.assertEqual(by_id["presence"], "not_found")

    def test_different_technology_preserves_work_and_quantity(self):
        # Method wording "талрепными стяжками" shares the work object but the
        # exact VOR wording is unproven: quantity stays confirmed, method is
        # the open question, the position is incomplete (not an error).
        result = self.parse()
        self.assertTrue(result["work_found"])
        self.assertTrue(result["volume_confirmed"])
        self.assertEqual(result["status"], INCOMPLETE)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["method"]["state"], "review_required")
        self.assertEqual(result["technology"]["status"], "unconfirmed")

    def test_different_technology_with_broken_quantity_stays_error(self):
        wire = self.wire()
        result = self.parse(wire, expected_volume="999", expected_unit="шт.")
        self.assertEqual(result["status"], "Ошибка")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "contradiction")
        self.assertEqual(by_id["method"]["state"], "review_required")

    def test_method_claim_omitted_for_works_leaves_question_open(self):
        wire = self.wire(method=False)
        result = self.parse(wire)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["method"]["state"], "not_found")
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertEqual(result["technology"]["status"], "unconfirmed")

    def test_material_method_does_not_gate_status(self):
        wire = self.wire(work_sid=self.qty_sid)
        result = self.parse(wire, expected_work="Зажим для троса",
                            position_type="Материал")
        self.assertEqual(result["status"], "ОК")

    def test_confirmed_method_needs_matching_vor_terms(self):
        wire = self.wire()
        result = self.parse(wire, expected_work="Сварка стыков аргоном")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "not_found")
        self.assertEqual(result["status"], "Неполная проверка")
    def test_method_cannot_be_invented_or_claimed_without_its_quote(self):
        quote_sources = _catalog({1: self.context[1]})
        sid = next(iter(quote_sources))
        bad_sources = dict(quote_sources)
        bad_sources[sid] = {"page": 1, "quote": "иной пресс"}
        wire = claims(("presence", [sid]), ("method", [sid]),
                      details="forged method quote")
        result = parse_audit_response(
            json.dumps(wire), {1: self.context[1]}, False,
            position_type="Работа", expected_work=self.WORK,
            expected_volume="328", expected_unit="шт.",
            quote_sources=bad_sources)
        self.assertEqual(result["status"], "Неполная проверка")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["method"]["state"], "not_found")

    def test_unknown_method_is_not_an_absent_operation(self):
        result = self.parse()
        self.assertEqual(result["status"], INCOMPLETE)
        self.assertTrue(result["work_found"])
        self.assertEqual(result["found_volume"], "328")

    def test_unrelated_table_row_cannot_prove_the_requested_operation(self):
        context = {5: self.context[5]}
        quote_sources = _catalog(context)
        sid = next(iter(quote_sources))
        wire = claims(("presence", [sid]),
                      ("quantity", [sid],
                       {"value_source_id": sid, "unit_source_id": sid}),
                      ("unit", [sid], {"unit_source_id": sid}),
                      details="foreign row")
        result = parse_audit_response(
            json.dumps(wire), context, False,
            position_type="Материал",
            expected_work="Геотекстиль Дорнит плотностью 500",
            expected_volume="2254,8", expected_unit="м2",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "not_found")
        self.assertEqual(by_id["quantity"]["state"], "contradiction")

    def test_operation_term_check_does_not_reject_applicable_material_row(self):
        quote_sources = _catalog(self.context)
        qty_sid = self.qty_sid
        wire = claims(("presence", [qty_sid]),
                      ("quantity", [qty_sid],
                       {"value_source_id": qty_sid,
                        "unit_source_id": qty_sid}),
                      ("unit", [qty_sid], {"unit_source_id": qty_sid}),
                      details="material row")
        result = parse_audit_response(
            json.dumps(wire), self.context, False,
            position_type="Материал",
            expected_work="Зажим для троса DIN 3093 10мм",
            expected_volume="328", expected_unit="шт.",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "ОК")

    def test_report_separates_method_problem_from_quantity_and_reference(self):
        from test_audit_reports import ReportTests
        report = ReportTests().make_report([("Ошибка", {
            "technology": {"status": "different", "expected": "обжимными втулками",
                           "actual": "талрепными стяжками"}})])
        row = report[0]
        self.assertIn("обжимными втулками", row["Что не так"])
        self.assertIn("талрепными стяжками", row["Что не так"])
        self.assertNotIn("не на тех страницах", row["Что не так"])
        self.assertNotIn("Объём", row["Что не так"])

    def test_found_page_is_derived_from_single_work_citation(self):
        wire = self.wire()
        result = self.parse(wire, expected_work="Монтаж плит с соединением отдельных плит талрепными стяжками")
        self.assertEqual(result["found_page"], "1")
        self.assertEqual(result["status"], "ОК")

    def test_conflicting_work_pages_merge_evidence(self):
        quote_sources = _catalog(self.context)
        wire = {"claims": [{"claim_id": "presence",
                             "source_ids": [self.work_sid, self.qty_sid]}],
                "discrepancy_details": "two pages"}
        result = parse_audit_response(
            json.dumps(wire), self.context, False,
            position_type="Работа", expected_work=self.WORK,
            expected_volume="328", expected_unit="шт.",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "confirmed")
        self.assertEqual(result["found_page"], "1")

    def test_confirmation_joins_direct_quantity_inside_same_scope_package(self):
        plan = ("Укладка гибких бетонных плит. План площадки ЛОС. "
                "Объём работ 281,85 м3 (площадка ЛОС). "
                "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
        section = ("Сечение 2-2. Площадка ЛОС. Укрепление откосов насыпи "
                   "гибкими бетонными плитами.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            quote_sources = _catalog(context)
            if len(calls) == 1:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укрепление откосов" in src["quote"])
                wire = claims(("presence", [work_sid]), details="work")
            else:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укрепление откосов" in src["quote"])
                qty_sid = next(s for s, src in quote_sources.items()
                               if "281,85 м3" in src["quote"])
                wire = claims(("presence", [work_sid]),
                              ("quantity", [qty_sid],
                               {"value_source_id": qty_sid,
                                "unit_source_id": qty_sid}),
                              ("unit", [qty_sid],
                               {"unit_source_id": qty_sid}),
                              details="direct volume on plan")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="281.85", expected_unit="м3",
                position_type="Материал",
                expected_work="Укладка гибких бетонных плит",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex([plan, section]), [0], ask,
            "Укладка гибких бетонных плит", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")
        self.assertEqual(result["found_pages"], [1, 2])

    def test_confirmation_keeps_anchor_naming_both_scopes(self):
        plan = ("Укладка гибких бетонных плит. План. "
                "Объём работ 281,85 м3 (площадка ЛОС). "
                "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
        section = ("Сечение 2-2. Площадка ЛОС. Укрепление откосов насыпи "
                   "гибкими бетонными плитами.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            quote_sources = _catalog(context)
            if len(calls) == 1:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укрепление откосов" in src["quote"])
                wire = claims(("presence", [work_sid]), details="work")
            else:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укрепление откосов" in src["quote"])
                qty_sid = next(s for s, src in quote_sources.items()
                               if "281,85 м3" in src["quote"])
                wire = claims(("presence", [work_sid]),
                              ("quantity", [qty_sid],
                               {"value_source_id": qty_sid,
                                "unit_source_id": qty_sid}),
                              ("unit", [qty_sid],
                               {"unit_source_id": qty_sid}),
                              details="direct volume on plan")
            return parse_audit_response(
                json.dumps(wire), context, False,
                expected_volume="281.85", expected_unit="м3",
                position_type="Материал",
                expected_work="Укладка гибких бетонных плит",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex([plan, section]), [0], ask,
            "Укладка гибких бетонных плит", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")

    def test_confirmation_without_linked_quantity_stays_incomplete(self):
        plan = ("Укладка гибких бетонных плит. План площадки ЛОС. "
                "Площадь крепления - 1879 м2 (площадка ЛОС).")
        foreign = ("Сечение 1-1. Площадка КНС. "
                   "Укладка гибких бетонных плит.")
        calls = []
        def ask(context):
            calls.append(sorted(context))
            quote_sources = _catalog(context)
            if len(calls) == 1:
                work_sid = next(s for s, src in quote_sources.items()
                                if "Укладка гибких бетонных плит" in src["quote"])
                wire = claims(("presence", [work_sid]), details="work only")
            else:
                wire = claims(details="confirm links nothing")
            return parse_audit_response(
                json.dumps(wire), context, False,
                position_type="Материал",
                expected_work="Укладка гибких бетонных плит",
                expected_volume="281.85", expected_unit="м3",
                quote_sources=quote_sources)
        result = audit_text_pages(PageTextIndex([plan, foreign]), [0], ask,
            "Укладка гибких бетонных плит", "281.85",
            unit="м3", formula="1879*0,15")
        self.assertEqual(result["status"], INCOMPLETE)


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
    """S×t operand mechanics were removed from the wire: quantity proposals
    carrying an area fragment plus a thickness fragment validate as a direct
    quantity binding (two different measurements = ambiguous), never as a
    computed 281.85. These tests pin that direct-measurement behavior."""

    def test_area_plus_thickness_proposals_stay_ambiguous(self):
        work = "Укладка плит на подготовленное основание."
        area = "Плиты. Площадь 526 м2 (площадка КНС) и 1879 м2 (площадка ЛОС)."
        context = {1: work,
                   2: area,
                   3: "Сечение 2-2. Площадка ЛОС. Плиты толщиной 0,15 м."}
        quote_sources = _catalog(context)
        work_sid = next(s for s, src in quote_sources.items()
                        if "Укладка плит" in src["quote"])
        area_sid = next(s for s, src in quote_sources.items()
                        if "526 м2" in src["quote"])
        thick_sid = next(s for s, src in quote_sources.items()
                         if "0,15 м" in src["quote"])
        wire = claims(("presence", [work_sid]),
                      ("quantity", [area_sid, thick_sid],
                       {"value_source_id": area_sid,
                        "unit_source_id": thick_sid}),
                      details="S×t as two fragments")
        result = parse_audit_response(
            json.dumps(wire), context, False,
            expected_volume="281.85", expected_unit="м3",
            expected_work="Укладка плит", position_type="Материал",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "review_required")
        self.assertIn("ambiguous_measurements", by_id["quantity"]["reasons"])
        self.assertEqual(result["status"], INCOMPLETE)

    def test_direct_volume_quote_confirms_without_computation(self):
        context = {1: "Укладка плит. Объём работ 281,85 м3 (площадка ЛОС).",
                   2: "Сечение 2-2. Площадка ЛОС. Плиты толщиной 0,15 м."}
        quote_sources = _catalog(context)
        vol_sid = next(s for s, src in quote_sources.items()
                       if "281,85 м3" in src["quote"])
        wire = claims(("presence", [vol_sid]),
                      ("quantity", [vol_sid],
                       {"value_source_id": vol_sid,
                        "unit_source_id": vol_sid}),
                      ("unit", [vol_sid], {"unit_source_id": vol_sid}),
                      details="direct volume")
        result = parse_audit_response(
            json.dumps(wire), context, False,
            expected_volume="281.85", expected_unit="м3",
            expected_work="Укладка плит", position_type="Материал",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "281.85")

    def test_split_terms_still_share_the_work_object(self):
        area = ("к ре пление г и бкими бе тонными плитами. "
                "П лоща д ь крепления - 1879 м2 (площадка ЛОС).")
        section = ("Укрепление откосов насыпи гибкими бетонными плитами. "
                   "Противосуффозионный экран геотекстиль Дорнит с прочностью не менее.")
        context = {1: area, 2: section}
        quote_sources = _catalog(context)
        work_sid = _sid(quote_sources, section, 2)
        wire = claims(("presence", [work_sid]),
                      ("method", [work_sid]), details="split terms")
        result = parse_audit_response(
            json.dumps(wire), context, False,
            expected_volume="281.85", expected_unit="м3",
            position_type="Работа",
            expected_work="Укрепление откосов насыпи гибкими бетонными плитами",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["presence"]["state"], "confirmed")

    def test_thickness_of_another_object_is_not_a_volume(self):
        context = {1: "Засыпка зазоров между бетонными блоками. Объём работ 45 м3.",
                   2: "Засыпка зазоров между бетонными блоками вручную щебнем. Толщина слоя 0,15 м."}
        quote_sources = _catalog(context)
        work_sid = next(s for s, src in quote_sources.items()
                        if "Объём работ 45 м3" in src["quote"])
        thick_sid = next(s for s, src in quote_sources.items()
                         if "0,15 м" in src["quote"])
        wire = claims(("presence", [work_sid]),
                      ("quantity", [thick_sid],
                       {"value_source_id": thick_sid,
                        "unit_source_id": thick_sid}),
                      ("unit", [thick_sid], {"unit_source_id": thick_sid}),
                      details="foreign thickness as volume")
        result = parse_audit_response(
            json.dumps(wire), context, False,
            expected_volume="281.85", expected_unit="м3",
            position_type="Материал", expected_work="Засыпка зазоров",
            quote_sources=quote_sources)
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "contradiction")
        self.assertEqual(result["status"], "Ошибка")


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
