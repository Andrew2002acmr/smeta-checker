"""Unit tests for the shared preflight/worker PDF binding helper."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pdf_binding import (  # noqa: E402
    clean_pdf_name,
    index_designations,
    normalize_designation,
    resolve_position_pdfs,
    split_designation_refs,
    split_pdf_refs,
)


class PdfBindingTests(unittest.TestCase):
    def test_explicit_copy_wins_over_an_exact_filename(self):
        result = resolve_position_pdfs(["original.pdf"], ["original.pdf", "copy.pdf"], bindings={"original.pdf": "copy.pdf"})
        self.assertEqual(result[0], ["copy.pdf"])
        self.assertIsNone(result[3])
        unavailable = resolve_position_pdfs(["original.pdf"], ["original.pdf"], bindings={"original.pdf": "copy.pdf"})
        self.assertIsNotNone(unavailable[3])

    def test_split_refs_newline_semicolon_and_quotes(self):
        self.assertEqual(
            split_pdf_refs('"a.pdf";\n b.PDF \r\n"a.pdf"'),
            ["a.pdf", "b.PDF"])

    def test_clean_name_normalizes_slash_and_case(self):
        self.assertEqual(clean_pdf_name('C:\\Docs\\Proj.PDF '), "proj.pdf")
        self.assertEqual(clean_pdf_name("nan"), "")

    def test_exact_match_case_insensitive_key(self):
        pdfs, _, _, err = resolve_position_pdfs(["PROJ.pdf"], ["proj.pdf"])
        self.assertIsNone(err)
        self.assertEqual(pdfs, ["proj.pdf"])

    def test_missing_reference_is_explicit_error(self):
        pdfs, missing, _, err = resolve_position_pdfs(["ghost.pdf"], ["proj.pdf"])
        self.assertEqual(pdfs, [])
        self.assertEqual(missing, ["ghost.pdf"])
        self.assertIn("ghost.pdf", err)

    def test_ambiguous_fuzzy_is_explicit_error(self):
        pool = ["tom-1.pdf", "tom-10.pdf"]
        pdfs, _, ambiguous, err = resolve_position_pdfs(["tom-1"], pool)
        self.assertEqual(pdfs, [])
        self.assertTrue(ambiguous)
        self.assertIn("Неоднозначно", err)

    def test_multiple_distinct_pdfs_is_explicit_error(self):
        pdfs, _, _, err = resolve_position_pdfs(["a.pdf", "b.pdf"], ["a.pdf", "b.pdf"])
        self.assertEqual(pdfs, ["a.pdf", "b.pdf"])
        self.assertIn("несколько PDF", err)

    def test_same_pdf_twice_folds_to_one(self):
        pdfs, _, _, err = resolve_position_pdfs(["a.pdf", '"a.pdf"'], ["a.pdf"])
        self.assertIsNone(err)
        self.assertEqual(pdfs, ["a.pdf"])

    def test_no_explicit_reference_uses_only_unambiguous_fallback(self):
        self.assertEqual(resolve_position_pdfs([], ["only.pdf"])[0], ["only.pdf"])
        pool = ["section-a.pdf", "section-b.pdf"]
        self.assertEqual(resolve_position_pdfs([], pool, "section-a")[0], ["section-a.pdf"])
        pdfs, _, _, error = resolve_position_pdfs([], pool, "section")
        self.assertEqual(pdfs, [])
        self.assertIn("Неоднозначная", error)
        self.assertIsNotNone(resolve_position_pdfs([], pool)[3])

    def test_designation_refs_skip_sheet_numbers(self):
        self.assertEqual(
            split_designation_refs("30/09-ТКР16.ГЧ л. 1,"),
            ["30/09-ТКР16.ГЧ"])
        self.assertEqual(
            split_designation_refs("30/09-ТКР16.ГЧ л.1-3; 30/09-ТКР16.СО л. 2"),
            ["30/09-ТКР16.ГЧ", "30/09-ТКР16.СО"])

    def test_designation_refs_empty_without_designations(self):
        for value in (None, "", "nan", "см. чертежи"):
            self.assertEqual(split_designation_refs(value), [])

    def test_normalize_designation_folds_case_and_spacing(self):
        self.assertEqual(
            normalize_designation("30/09 - ТКР16.гч"),
            normalize_designation("30/09-ТКР16.ГЧ"))
        self.assertNotEqual(
            normalize_designation("30/09-ТКР16.ГЧ"),
            normalize_designation("30/09-ТКР16.СО"))

    def test_index_designations_maps_full_and_bare_forms(self):
        index = index_designations([
            (1, "ПРОЕКТНАЯ ДОКУМЕНТАЦИЯ 30/09-ТКР16 Том 3.16"),
            (5, "Спецификация 30/09-ТКР16.СО"),
        ])
        self.assertEqual(index[normalize_designation("30/09-ТКР16")], [1, 5])
        self.assertEqual(index[normalize_designation("30/09-ТКР16.СО")], [5])
        self.assertNotIn(normalize_designation("30/09-ТКР16.ГЧ"), index)


if __name__ == "__main__":
    unittest.main()
