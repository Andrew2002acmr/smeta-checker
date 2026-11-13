"""Golden expectations for the 22 real VOR positions (manual review verdicts).

Source of truth: the user's hand-checked reviews of audit.docx/audit(1).docx/
audit(2).docx against the original Excel and PDF. Each case pins the
workbook-layer verdict (no PDF, no LLM): display consistency, stock
chains, and scheme checks. Live-model verdicts are NOT pinned here.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from audit_reports import DISCREPANCY, MANUAL, build_user_report
from quantity_graph import link_stock_chain, verify_stock_chain
from vor_consistency import (
    check_display_consistency,
    check_scheme_divisors,
)

import json

from audit_core import parse_audit_response


def _dep(cell, value, origin="vor_cell"):
    return {"cell": cell, "expression": value if cell is None else f"={cell}",
            "value": value, "cached_value": None, "origin": origin,
            "status": "calculated"}


def _calc(cell, expression, value, deps):
    return {"cell": cell, "expression": expression, "value": value,
            "cached_value": None, "status": "calculated", "error": None,
            "cached_mismatch": False, "dependencies": deps}


def _pos(index, number, ptype, unit, volume, work, comment, calc,
         display_cell="", display_formula=None):
    refs = {"display_cell": display_cell} if display_cell else {}
    if display_formula is not None:
        refs["display_formula"] = display_formula
    return SimpleNamespace(
        index=index, number=number, position_type=ptype, unit=unit,
        volume=volume, work_name=work, comment=comment, formula="",
        calculation=calc, formula_refs=refs)


# Real workbook cells (vor.xlsx): D19=631.2 stocked area, D31=2254.8.
_CELLS = {
    "E20": {"formula": '=D19&"*0,44"', "cached": "631,2*0,44"},
    "E32": {"formula": '=D31&"*0,44"', "cached": "2254,8*0,44"},
    "D19": {"formula": "=D18*1.2", "cached": 631.2},
    "D31": {"formula": "=D30*1.2", "cached": 2254.8},
}


def _anchor_calc(expr, base, base_cell):
    return _calc("D?", expr, "0", [
        _dep(base_cell, base), _dep(None, "0.44", "formula_literal"),
        _dep(None, "0", "formula_literal")])


class GoldenWorkbookTests(unittest.TestCase):
    """User verdict: №3/№14 are genuine D↔E contradictions (278 vs 231,
    992 vs 827); quantities themselves match the spec."""

    def test_no3_display_contradiction(self):
        calc = _anchor_calc("=ROUND(D18*0.44,0)", "526", "D18")
        calc.update(cell="D20", value="231")
        hits = check_display_consistency(
            calc, "631,2*0,44",
            {"display_cell": "E20", "display_formula": '=D19&"*0,44"'},
            _CELLS)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "display-text-diverges")
        self.assertIn("631,2", hits[0]["message"])
        self.assertIn("D19", hits[0]["message"])
        self.assertIn("231", hits[0]["message"])

    def test_no14_display_contradiction(self):
        calc = _anchor_calc("=ROUND(D30*0.44,0)", "1879", "D30")
        calc.update(cell="D32", value="827")
        hits = check_display_consistency(
            calc, "2254,8*0,44",
            {"display_cell": "E32", "display_formula": '=D31&"*0,44"'},
            _CELLS)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "display-text-diverges")
        self.assertIn("2254,8", hits[0]["message"])
        self.assertIn("D31", hits[0]["message"])
        self.assertIn("827", hits[0]["message"])


class GoldenStockChainTests(unittest.TestCase):
    """User verdict: work × 1.1 = material = spec (78.90→86.79;
    281.85→310.035). Comparing bare work against the stocked schedule
    row is a false positive."""

    def _work(self, index, number, base_cell, base):
        return _pos(
            index, number, "Работа", "м3", "78.90" if base == "526" else "281.85",
            "Засыпка зазоров", "Расход щебня 0,15 м3/м2",
            _calc("D?", f"={base_cell}*0.15", "0", [
                _dep(base_cell, base),
                _dep(None, "0.15", "formula_literal")]),
            display_cell="E?")

    def _material(self, index, number, base_cell, base, volume):
        return _pos(
            index, number, "Материал", "м3", volume, "Щебень",
            "Кзапаса =1,1",
            _calc("D?", f"={base_cell}*0.15*1.1", volume, [
                _dep(base_cell, base),
                _dep(None, "0.15", "formula_literal"),
                _dep(None, "1.1", "formula_literal")]),
            display_cell="E?")

    def test_no10_links_no11_factor_1_1(self):
        work = self._work(9, "10", "D18", "526")
        mat = self._material(10, "11", "D18", "526", "86.79")
        link = link_stock_chain(work, [work, mat])
        self.assertIsNotNone(link)
        self.assertEqual(link["material_number"], "11")
        self.assertEqual(link["factor"], "1.1")
        out = verify_stock_chain(work, [work, mat])
        self.assertTrue(out["matches"])
        self.assertEqual(out["uncertainties"], [])

    def test_no21_links_no22_factor_1_1(self):
        work = self._work(20, "21", "D30", "1879")
        mat = self._material(21, "22", "D30", "1879", "310.035")
        link = link_stock_chain(work, [work, mat])
        self.assertIsNotNone(link)
        self.assertEqual(link["material_number"], "22")
        self.assertEqual(link["factor"], "1.1")
        out = verify_stock_chain(work, [work, mat])
        self.assertTrue(out["matches"])
        self.assertEqual(out["uncertainties"], [])


class GoldenSchemeTests(unittest.TestCase):
    """User verdict: №17 uses M35=4 while №20 divides by 5 — the basis
    deserves review. КНС pair (M23=5 vs /5) agrees and stays silent."""

    def _conn(self, index, number, m_cell, m_value, base_cell, base_value):
        return _pos(
            index, number, "Работа", "шт.", "0", "Скрепление плит", "",
            _calc("D?", f"=ROUND(2*({m_cell}*({base_cell}/{m_cell}-1)+({m_cell}-1)),0)",
                  "0", [_dep(m_cell, m_value), _dep(base_cell, base_value)]),
            display_cell="E?")

    def _driven(self, index, number, base_cell, base_value):
        return _pos(
            index, number, "Материал", "шт.", "0", "Анкер забивной", "",
            _calc("D?", f"=ROUND(2*{base_cell}/5+2,0)", "0",
                  [_dep(base_cell, base_value)]),
            display_cell="E?")

    def test_los_scheme_mismatch_flagged(self):
        work = self._conn(16, "17", "M35", "4", "D34", "590")
        mat = self._driven(19, "20", "D34", "590")
        hits = check_scheme_divisors([work, mat])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "scheme-divisor-mismatch")
        self.assertIn("M35 = 4", hits[0]["message"])
        self.assertIn("№20", hits[0]["message"])

    def test_kns_scheme_agreement_silent(self):
        work = self._conn(5, "6", "M23", "5", "D22", "165")
        mat = self._driven(8, "9", "D22", "165")
        self.assertEqual(check_scheme_divisors([work, mat]), [])


class GoldenReportTests(unittest.TestCase):
    """User verdict: №3/№14 wording must say the contradiction is
    inside the workbook (quantities match the spec), not a quantity
    mismatch against ПД."""

    def _row(self, status, source_overrides):
        frame = pd.DataFrame([{"№ п.п.": 3, "Статус аудита": status,
                               "Детали расхождений": ""}])
        source = {"pdf_name": "пд.pdf", "found_page": 88, "found_pages": [88],
                  "vor_pages": [88], "work_found": True,
                  "volume_confirmed": True, "unit_confirmed": True,
                  "comment_confirmed": True, "found_volume": "231",
                  "found_unit": "шт.", "evidence": []}
        source.update(source_overrides)
        task = {"w_name": "Анкеры", "w_vol": "231", "w_unit": "шт.",
                "w_comment": "", "w_page": "88", "target_indices": [87],
                "loc_desc": "Стр. 88", "file_refs": "пд.pdf"}
        return build_user_report(frame, [source], [task])[0]

    def test_no3_wording_names_workbook_contradiction(self):
        row = self._row("Ошибка", {"workbook_notes": [
            {"kind": "display-text-diverges", "display_cell": "E20",
             "message": "расчёт E20 не сходится с количеством D20"}]})
        self.assertEqual(row["Статус аудита"], DISCREPANCY)
        self.assertIn("В самой ведомости противоречие", row["Что не так"])
        self.assertIn("E20", row["Что не так"])
        self.assertNotIn("а в спецификации", row["Что не так"])

    def test_chain_convergence_is_manual_not_error(self):
        frame = pd.DataFrame([{"№ п.п.": 10,
                               "Статус аудита": "Неполная проверка",
                               "Детали расхождений": ""}])
        source = {"pdf_name": "пд.pdf", "found_page": 84, "found_pages": [84],
                  "vor_pages": [84], "work_found": True,
                  "volume_confirmed": False, "unit_confirmed": False,
                  "comment_confirmed": False, "found_volume": "-",
                  "found_unit": "-", "evidence": [],
                  "quantity_basis": "consumption",
                  "consumption": {
                      "matches": True, "schedule_volume": "86,79",
                      "schedule_page": 88, "schedule_item": "Щебень",
                      "rate": "0,15", "unit": "м3/м2",
                      "uncertainties": ["rate-justification-open"],
                      "stock_chain": {"material_number": "11", "factor": "1.1",
                                      "work_volume": "78,90",
                                      "material_volume": "86,79"}}}
        task = {"w_name": "Засыпка", "w_vol": "78,90", "w_unit": "м3",
                "w_comment": "Расход щебня 0,15 м3/м2", "w_page": "84",
                "target_indices": [83], "loc_desc": "Стр. 84",
                "file_refs": "пд.pdf"}
        row = build_user_report(frame, [source], [task])[0]
        self.assertEqual(row["Статус аудита"], MANUAL)
        self.assertIn("Потребность в материале сходится", row["Что не так"])
        self.assertIn("Обоснованность нормы", row["Что не так"])


class GoldenEvidenceChainTests(unittest.TestCase):
    """№2 diagnostic chain: verbatim spec row 1.1 must validate to ОК.
    Live-model verdicts are NOT pinned; only that correct evidence
    survives validation."""

    ROW_11 = ("<tr><td>1.1</td><td>Геотекстиль Дорнит, плотностью 500 г/м2</td>"
              "<td></td><td></td><td></td><td>м2</td><td>631,2</td>"
              "<td></td><td></td></tr>")
    HEADER = ("<table><tr><th>Поз.</th><th>Наименование и техническая "
              "характеристика</th><th>Поставщик</th></tr>")

    def test_spec_row_validates_to_ok(self):
        quote_sources = {"p88q1": {"page": 88, "quote": self.ROW_11}}
        data = {
            "claims": [
                {"claim_id": "presence", "source_ids": ["p88q1"]},
                {"claim_id": "quantity", "source_ids": ["p88q1"],
                 "value_source_id": "p88q1", "unit_source_id": "p88q1"},
                {"claim_id": "unit", "source_ids": ["p88q1"]},
            ],
            "discrepancy_details": "Найдено в спецификации",
        }
        result = parse_audit_response(
            json.dumps(data), {88: self.ROW_11}, position_type="Материал",
            expected_volume="631.1999999999999", expected_unit="м2",
            expected_work="Геотекстиль Дорнит, плотностью 500 г/м2",
            quote_sources=quote_sources)
        self.assertEqual(result["status"], "ОК")
        self.assertEqual(result["found_volume"], "631.2")
        self.assertEqual(result["found_page"], "88")
        self.assertFalse(result["comment_confirmed"])
        self.assertEqual(result["technology"]["status"], "not_applicable")
        by_id = {c["claim_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["quantity"]["state"], "confirmed")
if __name__ == "__main__":
    unittest.main()
