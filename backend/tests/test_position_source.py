"""Stage 1: position identity is independent of numeric quantity.

Same work with the same number in different sections keeps different source
identity; mutating one quantity does not move the position across sections.
"""

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl

from vor_parse import load_vor_positions


def _workbook(cells):
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = "VOR"
    for addr, value in cells.items():
        ws[addr] = value
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


HEADER = {
    "A1": "Ведомость",
    "A2": "№ п.п.",
    "B2": "Наименование работ",
    "C2": "Тип позиции",
    "D2": "Объем работ / Количество",
    "E2": "Ед. изм.",
    "F2": "Формула текст",
    "G2": "Ссылка на чертежи",
}


def _corpus(quantity_kns="10", quantity_los="10"):
    cells = dict(HEADER)
    cells.update(
        {
            # Technical "1; 2" row keeps the pattern of the real corpus:
            # the parser must filter it without shifting source rows.
            "A3": "1",
            "B3": "2",
            "A5": "Раздел 1 (площадка КНС)",
            "A6": 1,
            "B6": "Укладка плит",
            "C6": "Работа",
            "D6": quantity_kns,
            "E6": "м2",
            "A8": "Раздел 2 (площадка ЛОС)",
            "A9": 2,
            "B9": "Укладка плит",
            "C9": "Работа",
            "D9": quantity_los,
            "E9": "м2",
        }
    )
    return _workbook(cells)


class PositionSourceTests(unittest.TestCase):
    def test_same_work_same_quantity_keeps_section_identity(self):
        positions, _ = load_vor_positions(
            io.BytesIO(_corpus()), "vor.xlsx"
        )
        self.assertEqual(len(positions), 2)
        kns, los = positions
        for position in positions:
            self.assertIsNotNone(position.position_source)
        self.assertEqual(kns.position_source.row, 6)
        self.assertEqual(los.position_source.row, 9)
        self.assertNotEqual(
            kns.position_source.position_id, los.position_source.position_id
        )
        self.assertEqual(kns.position_source.scope_key, "КНС")
        self.assertEqual(los.position_source.scope_key, "ЛОС")
        self.assertEqual(kns.position_source.scope_reason, "section_heading")
        self.assertIn("КНС", kns.position_source.sections[0].text)

    def test_mutated_quantity_does_not_move_scope(self):
        before, _ = load_vor_positions(io.BytesIO(_corpus()), "vor.xlsx")
        after, _ = load_vor_positions(
            io.BytesIO(_corpus(quantity_kns="2254.8")), "vor.xlsx"
        )
        self.assertEqual(before[0].position_source.row, 6)
        self.assertEqual(after[0].position_source.row, 6)
        self.assertEqual(
            before[0].position_source.sheet, after[0].position_source.sheet
        )
        self.assertEqual(after[0].position_source.scope_key, "КНС")
        self.assertEqual(after[0].work_name, before[0].work_name)
        self.assertNotEqual(after[0].volume, before[0].volume)

    def test_xlsx_cells_carry_plain_and_formula_addresses(self):
        raw = _workbook(
            {
                **HEADER,
                "A3": "1",
                "B3": "2",
                "A4": 1,
                "B4": "Укладка плит",
                "C4": "Работа",
                "D4": "=2*3",
                "E4": "м2",
            }
        )
        positions, _ = load_vor_positions(io.BytesIO(raw), "vor.xlsx")
        source = positions[0].position_source
        self.assertEqual(source.row, 4)
        self.assertTrue(source.position_id.endswith(":VOR:4"))
        self.assertEqual(source.cells.get("D4"), "=2*3")
        self.assertIn("B4", source.cells)

    def test_csv_uses_logical_records_without_excel_cells(self):
        csv = (
            "№ п.п.;Наименование работ;Объём работ / Количество;Ед. изм.;"
            "Тип позиции;Ссылка на чертежи\n"
            "1;2\n"
            "Раздел 1 (площадка КНС)\n"
            "\n"
            "1;Укладка плит;10;м2;Работа;doc.pdf\n"
            ";Лист 1;doc.pdf\n"
            "Раздел 2 (площадка ЛОС)\n"
            "2;Укладка плит;10;м2;Работа;doc.pdf\n"
        )
        positions, _ = load_vor_positions(
            io.BytesIO(csv.encode("utf-8-sig")), "vor.csv"
        )
        self.assertEqual(len(positions), 2)
        kns, los = positions
        self.assertEqual(kns.position_source.format, "csv")
        self.assertEqual(kns.position_source.row, 4)
        self.assertEqual(los.position_source.row, 7)
        self.assertNotEqual(
            kns.position_source.position_id, los.position_source.position_id
        )
        self.assertEqual(kns.position_source.sheet, "")
        for address in kns.position_source.cells:
            self.assertNotRegex(address, r"^[A-Z]+[0-9]+$")
        self.assertEqual(kns.position_source.scope_key, "КНС")
        self.assertEqual(los.position_source.scope_key, "ЛОС")
        self.assertEqual(kns.position_source.continuation_rows, [])

    def test_heading_scope_accepts_generic_site_keyword(self):
        raw = _workbook(
            {
                **HEADER,
                "A3": "Раздел (участок КНС)",
                "A4": 1,
                "B4": "Укладка плит",
                "C4": "Работа",
                "D4": "10",
                "E4": "м2",
            }
        )
        positions, _ = load_vor_positions(io.BytesIO(raw), "vor.xlsx")
        self.assertEqual(positions[0].position_source.scope_key, "КНС")
        self.assertEqual(
            positions[0].position_source.scope_reason, "section_heading"
        )

if __name__ == "__main__":
    unittest.main()
