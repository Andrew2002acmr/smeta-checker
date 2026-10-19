"""Workbook self-consistency: value / formula / display-text checks."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vor_consistency import (
    check_display_consistency,
    check_length_operands,
    check_scheme_divisors,
)


class DisplayConsistencyTests(unittest.TestCase):
    def test_matching_display_passes(self):
        calc = {
            "cell": "D21", "expression": "=D18*0.15", "value": "78.90",
            "status": "calculated",
            "dependencies": [
                {"cell": "D18", "value": "526", "origin": "vor_cell"},
                {"cell": None, "expression": "0.15", "value": "0.15",
                 "origin": "formula_literal"},
            ],
        }
        cells = {"E21": {"formula": '=D18&"*0,15"', "cached": "526*0,15"}}
        self.assertEqual(
            check_display_consistency(
                calc, "526*0,15",
                {"display_cell": "E21", "display_formula": '=D18&"*0,15"'},
                cells),
            [])

    def test_foreign_number_flagged_once_with_cell(self):
        calc = {
            "cell": "D20", "expression": "=ROUND(D18*0.44,0)",
            "value": "231", "status": "calculated",
            "dependencies": [
                {"cell": "D18", "value": "526", "origin": "vor_cell"},
                {"cell": None, "expression": "0.44", "value": "0.44",
                 "origin": "formula_literal"},
                {"cell": None, "expression": "0", "value": "0",
                 "origin": "formula_literal"},
            ],
        }
        cells = {
            "E20": {"formula": '=D19&"*0,44"', "cached": "631,2*0,44"},
            "D19": {"formula": "=D18*1.2", "cached": 631.2},
        }
        hits = check_display_consistency(
            calc, "631,2*0,44",
            {"display_cell": "E20", "display_formula": '=D19&"*0,44"'},
            cells)
        kinds = {h["kind"] for h in hits}
        self.assertEqual(
            kinds, {"display-operand-mismatch", "display-cell-not-operand"})
        self.assertTrue(all(h["display_cell"] == "E20" for h in hits))
        self.assertTrue(any("D19" in h["message"] for h in hits))

    def test_no_display_text_passes(self):
        calc = {"cell": "D18", "expression": "=L18", "value": "526",
                "status": "calculated", "dependencies": []}
        self.assertEqual(
            check_display_consistency(calc, "", {"display_cell": "E18"}, {}),
            [])

    def test_failed_calculation_skipped(self):
        calc = {"cell": "D20", "expression": "=ROUND(D18*0.44,0)",
                "value": None, "status": "error", "dependencies": []}
        cells = {"E20": {"formula": '=D19&"*0,44"', "cached": "631,2*0,44"}}
        self.assertEqual(
            check_display_consistency(
                calc, "631,2*0,44", {"display_cell": "E20"}, cells),
            [])


class SchemeDivisorTests(unittest.TestCase):
    def _work(self, index, number, expr, deps):
        from types import SimpleNamespace
        return SimpleNamespace(
            index=index, number=number, calculation={
                "expression": expr, "status": "calculated",
                "dependencies": deps})

    def test_matching_divisors_pass(self):
        work = self._work(5, "6", "=ROUND(2*(M23*(D22/M23-1)+(M23-1)),0)", [
            {"cell": "D22", "value": "165", "origin": "vor_cell"},
            {"cell": "M23", "value": "5", "origin": "vor_cell"}])
        mat = self._work(8, "9", "=ROUND(2*D22/5+2,0)", [
            {"cell": "D22", "value": "165", "origin": "vor_cell"}])
        self.assertEqual(check_scheme_divisors([work, mat]), [])

    def test_mismatching_divisors_flagged(self):
        work = self._work(16, "17", "=ROUND(2*(M35*(D34/M35-1)+(M35-1)),0)", [
            {"cell": "D34", "value": "590", "origin": "vor_cell"},
            {"cell": "M35", "value": "4", "origin": "vor_cell"}])
        mat = self._work(19, "20", "=ROUND(2*D34/5+2,0)", [
            {"cell": "D34", "value": "590", "origin": "vor_cell"}])
        hits = check_scheme_divisors([work, mat])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "scheme-divisor-mismatch")
        self.assertIn("M35 = 4", hits[0]["message"])
        self.assertIn("№20", hits[0]["message"])


class LengthOperandTests(unittest.TestCase):
    def test_present_value_passes(self):
        calc = {"status": "calculated", "dependencies": [
            {"cell": "L35", "value": "172", "origin": "vor_cell"}]}
        pages = {34: "участок ЛОС: 172 м."}
        self.assertEqual(check_length_operands(calc, pages), [])

    def test_close_length_with_unit_flagged(self):
        calc = {"status": "calculated", "dependencies": [
            {"cell": "L35", "value": "172", "origin": "vor_cell"}]}
        pages = {84: "Длина по верхней бровке 171м; S=1879м²"}
        hits = check_length_operands(calc, pages)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "length-operand-conflicts")
        self.assertIn("171м", hits[0]["message"])

    def test_close_number_without_unit_ignored(self):
        calc = {"status": "calculated", "dependencies": [
            {"cell": "L35", "value": "172", "origin": "vor_cell"}]}
        pages = {84: "КК-5-168 (сущ.)"}
        self.assertEqual(check_length_operands(calc, pages), [])

    def test_snippet_has_no_html_and_breaks_on_words(self):
        calc = {"status": "calculated", "dependencies": [
            {"cell": "L35", "value": "172", "origin": "vor_cell"}]}
        pages = {84: ("обережная часть берегоукрепления<br>Длина по верхней "
                      "бровке 171м; S=1879м²<br>123,88 124,02<br>117,20 К")}
        hits = check_length_operands(calc, pages)
        self.assertEqual(len(hits), 1)
        message = hits[0]["message"]
        self.assertNotIn("<", message)
        self.assertNotIn(">", message)
        self.assertIn("171м", message)
if __name__ == "__main__":
    unittest.main()
