"""Quantity graph: consumption detection, schedule linkage, reconciliation."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import detect_consumption
from audit_measurements import format_ru
from quantity_graph import (
    classify_quantity,
    link_consumption_schedule,
    link_stock_chain,
    reconcile_consumption,
    reconcile_position_schedule,
    verify_stock_chain,
)


def _schedule():
    return [(1,
        "<table><tr><th>Наименование</th><th>Ед.</th><th>Кол-во</th></tr>"
        "<tr><td>Площадка Альфа</td><td></td><td></td></tr>"
        "<tr><td>Щебень М400</td><td>м3</td><td>86,79</td></tr>"
        "<tr><td>Площадка Бета</td><td></td><td></td></tr>"
        "<tr><td>Щебень М400</td><td>м3</td><td>310,035</td></tr></table>")]


class DetectConsumptionTests(unittest.TestCase):
    def test_rate_unit_detects_without_keywords(self):
        rate, unit = detect_consumption("Засыпка щебнем 0,15 м3/м2")
        self.assertEqual((rate, unit), (Decimal("0.15"), "м3/м2"))

    def test_thickness_never_detects_despite_same_number(self):
        self.assertEqual(detect_consumption("Плиты толщиной 0,15 м"), (None, ""))

    def test_keyword_fallback_for_unknown_unit_spelling(self):
        rate, unit = detect_consumption("Норма расхода 2,5 кгс/м2")
        self.assertEqual((rate, unit), (Decimal("2.5"), "кгс/м2"))


class FormatRuTests(unittest.TestCase):
    def test_float_tails_die_genuine_precision_survives(self):
        self.assertEqual(format_ru(Decimal("78.89999999999999")), "78,90")
        self.assertEqual(format_ru("310.03499999999997"), "310,035")
        self.assertEqual(format_ru("165"), "165")
        self.assertEqual(format_ru("0.165"), "0,165")
        self.assertEqual(format_ru("-"), "-")


class LinkScheduleTests(unittest.TestCase):
    def test_unique_scope_row_links(self):
        hit = link_consumption_schedule(
            "Засыпка зазоров щебнем", "Расход щебня 0,15 м3/м2",
            "Альфа", _schedule())
        self.assertEqual(
            (hit["page"], hit["volume"], hit["scope"]),
            (1, Decimal("86.79"), "АЛЬФА"))

    def test_wrong_scope_or_ambiguity_fails_closed(self):
        self.assertIsNone(link_consumption_schedule(
            "Засыпка зазоров щебнем", "Расход щебня", "", _schedule()))
        doubled = _schedule() + _schedule()
        self.assertIsNone(link_consumption_schedule(
            "Засыпка зазоров щебнем", "Расход щебня", "Альфа", doubled))

    def test_mark_code_equal_to_amount_does_not_bind(self):
        schedule = [(1,
            "<table><tr><th>Наименование</th><th>Ед.</th><th>Кол-во</th></tr>"
            "<tr><td>Площадка Альфа</td><td></td><td></td></tr>"
            "<tr><td>Марка 86,79</td><td>шт.</td><td>12</td></tr>"
            "<tr><td>Щебень М400</td><td>м3</td><td>86,79</td></tr></table>")]
        hit = link_consumption_schedule(
            "Засыпка зазоров щебнем", "Расход щебня 0,15 м3/м2",
            "Альфа", schedule)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["volume"], Decimal("86.79"))

class ReconcileTests(unittest.TestCase):
    def test_delta_percent_and_derived_rate(self):
        out = reconcile_consumption(
            Decimal("78.90"), Decimal("526"), Decimal("0.15"), Decimal("86.79"))
        self.assertEqual(out["delta"], Decimal("-7.89"))
        self.assertEqual(out["delta_percent"], Decimal("-9.09"))
        self.assertEqual(out["derived_rate"], Decimal("0.165"))
        self.assertFalse(out["matches"])
        self.assertIn("rate-differs-from-schedule", out["uncertainties"])

    def test_missing_schedule_is_explicit_uncertainty(self):
        out = reconcile_consumption(
            Decimal("78.90"), Decimal("526"), Decimal("0.15"), None)
        self.assertIsNone(out["matches"])
        self.assertIn("schedule-volume-missing", out["uncertainties"])


class WorkerPassTests(unittest.TestCase):
    def test_non_consumption_returns_none(self):
        self.assertIsNone(reconcile_position_schedule(
            {"w_name": "Укладка плит", "w_comment": "", "w_formula": "526*0,15"},
            {"evidence": []}))

    def test_consumption_without_index_reports_unavailable(self):
        out = reconcile_position_schedule(
            {"w_name": "Засыпка", "w_comment": "Расход щебня 0,15 м3/м2",
             "w_formula": "", "text_index": None},
            {"evidence": []})
        self.assertEqual(out["uncertainties"], ["schedule-text-unavailable"])


class ClassifyTests(unittest.TestCase):
    def test_consumption_needs_rate_unit_not_bare_product(self):
        calc = {"expression": "=D18*0.15", "dependencies": [
            {"origin": "vor_cell", "cell": "D18", "value": "526"},
            {"origin": "formula_literal", "value": "0.15"}]}
        kind, _ = classify_quantity("Засыпка", "Расход щебня 0,15 м3/м2", "м3", "526*0,15", calc)
        self.assertEqual(kind, "consumption")
        kind, _ = classify_quantity("Укладка плит", "526 м2, толщина 150 мм", "м3", "526*0,15", calc)
        self.assertEqual(kind, "geometry")

    def test_count_needs_round_and_discrete_unit(self):
        calc = {"expression": "=ROUND(D18*0.44,0)", "dependencies": []}
        kind, _ = classify_quantity("Анкеры", "", "шт.", "", calc)
        self.assertEqual(kind, "count")
        kind, _ = classify_quantity("Щебень", "", "м3", "", calc)
        self.assertNotEqual(kind, "count")

    def test_direct_covers_references_and_unknown_shapes(self):
        calc = {"expression": "=D18", "dependencies": []}
        kind, _ = classify_quantity("Укладка", "", "м2", "", calc)
        self.assertEqual(kind, "direct")
        kind, _ = classify_quantity("Работа", "", "м3", "", {"expression": "=СЛОЖНАЯ(1;2)", "dependencies": []})
        self.assertEqual(kind, "direct")


class StockChainTests(unittest.TestCase):
    def _pos(self, index, number, ptype, unit, volume, expr, deps,
             work="Засыпка", comment="Расход щебня 0,15 м3/м2"):
        from types import SimpleNamespace
        return SimpleNamespace(
            index=index, number=number, position_type=ptype, unit=unit,
            volume=volume, work_name=work, comment=comment, formula="",
            calculation={"expression": expr, "value": volume,
                         "status": "calculated", "dependencies": deps})

    def _chain(self):
        work_deps = [
            {"cell": "D18", "value": "526", "origin": "vor_cell"},
            {"cell": None, "value": "0.15", "origin": "formula_literal"}]
        mat_deps = work_deps + [
            {"cell": None, "value": "1.1", "origin": "formula_literal"}]
        work = self._pos(9, "10", "Работа", "м3", "78.90", "=D18*0.15", work_deps)
        mat = self._pos(10, "11", "Материал", "м3", "86.79", "=D18*0.15*1.1",
                         mat_deps, work="Щебень", comment="Кзапаса =1,1")
        return work, mat

    def test_work_links_stocked_material(self):
        work, mat = self._chain()
        link = link_stock_chain(work, [work, mat])
        self.assertEqual(link["material_number"], "11")
        self.assertEqual(link["factor"], "1.1")

    def test_chain_verifies_arithmetic(self):
        work, mat = self._chain()
        out = verify_stock_chain(work, [work, mat])
        self.assertTrue(out["matches"])
        self.assertEqual(out["uncertainties"], [])

    def test_non_consumption_work_has_no_chain(self):
        work, mat = self._chain()
        work.comment = "толщина 150 мм"
        self.assertIsNone(link_stock_chain(work, [work, mat]))

    def test_different_base_has_no_chain(self):
        work, mat = self._chain()
        mat.calculation["dependencies"][0] = {
            "cell": "D30", "value": "1879", "origin": "vor_cell"}
        self.assertIsNone(link_stock_chain(work, [work, mat]))

    def test_cross_section_material_never_links(self):
        work, mat = self._chain()
        work.position_source = {"scope_key": "КНС"}
        mat.position_source = {"scope_key": "ЛОС"}
        self.assertIsNone(link_stock_chain(work, [work, mat]))

    def test_ambiguous_structural_matches_fail_closed(self):
        import copy
        from types import SimpleNamespace
        work, mat = self._chain()
        work.position_source = {"scope_key": "КНС"}
        mat.position_source = {"scope_key": "КНС"}
        twin = SimpleNamespace(**{**mat.__dict__})
        twin.calculation = copy.deepcopy(mat.calculation)
        twin.index = 11
        twin.number = "12"
        self.assertIsNone(link_stock_chain(work, [work, mat, twin]))

    def test_same_section_material_still_links(self):
        work, mat = self._chain()
        work.position_source = {"scope_key": "КНС"}
        mat.position_source = {"scope_key": "КНС"}
        link = link_stock_chain(work, [work, mat])
        self.assertEqual(link["material_number"], "11")

if __name__ == "__main__":
    unittest.main()
