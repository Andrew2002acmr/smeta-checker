"""Linked evidence by named scope/section, not proximity or bare numbers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_core import (PageTextIndex, measurement_scopes, section_scopes)


def fixture():
    plan = ("План площадки ЛОС. Площадь крепления - 1879м2 (площадка ЛОС). "
            "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
    kns_section = ("Сечение 1-1\nПлощадка КНС\nУкладка плит")
    los_section = ("Сечение 2-2\nПлощадка ЛОС\n"
                   "Укладка плит толщиной 0,15 м")
    los_spec = ("<table><tr><th>Наименование и техническая характеристика</th>"
                "<th>Поставщик</th><th>Единица измерения</th><th>Количество</th></tr>"
                "<tr><td>Берегоукрепление площадки ЛОС</td><td></td><td></td><td></td></tr>"
                "<tr><td>Плиты ПБЗГУ</td><td></td><td>шт.</td><td>165</td></tr>"
                "<tr><td>Зажимы для троса</td><td></td><td>шт.</td><td>328</td></tr></table>")
    kns_spec = ("<table><tr><th>Наименование и техническая характеристика</th>"
                "<th>Поставщик</th><th>Единица измерения</th><th>Количество</th></tr>"
                "<tr><td>Берегоукрепление площадки КНС</td><td></td><td></td><td></td></tr>"
                "<tr><td>Плиты ПБЗГУ</td><td></td><td>шт.</td><td>165</td></tr>"
                "<tr><td>Зажимы для каната</td><td></td><td>шт.</td><td>328</td></tr></table>")
    return ["Титул", plan, kns_section, los_section, los_spec, kns_spec]


class RelatedContextTests(unittest.TestCase):
    def test_plan_links_los_section_before_unrelated_kns_section(self):
        texts = fixture()
        scope, related = PageTextIndex(texts).related_context(
            [1], "Укладка плит", "281.85", "1879*0.15", "")
        self.assertEqual(scope, "ЛОС")
        # Unrelated nearby 1-1 (index 2) must lose to caption 2-2 (index 3).
        self.assertEqual(related[0], 3)
        self.assertNotIn(2, related)

    def test_material_spec_row_supports_matching_scope_quantity(self):
        texts = fixture()
        texts[1] = ("План площадки ЛОС. Площадь крепления - 165м2 (площадка ЛОС). "
                    "Сечение 2-2 выполнено по берегоукреплению площадки ЛОС.")
        scope, related = PageTextIndex(texts).related_context(
            [1], "Укладка плит зажимами для каната", "165", "165*3", "")
        self.assertEqual(scope, "ЛОС")
        self.assertEqual(related[0], 3)
        self.assertIn(4, related)
        self.assertNotIn(5, related)
    def test_same_number_does_not_transfer_across_scopes(self):
        scope, related = PageTextIndex(fixture()).related_context(
            [1], "Укладка плит", "999", "", "")
        self.assertEqual((scope, related), ("", []))

    def test_operation_resource_row_anchors_unique_scope(self):
        # Shared clamp row: same row carries the operand count plus task terms.
        texts = fixture()
        texts[4] = texts[4].replace("Плиты ПБЗГУ</td><td></td><td>шт.</td><td>165</td>",
                                    "Плиты ПБЗГУ, зажимы для каната</td><td></td><td>шт.</td><td>165</td>")
        texts[5] = texts[5].replace("Зажимы для каната", "Зажимы для шланга")
        scope, related = PageTextIndex(texts).related_context(
            [1], "Скрепление плит зажимами для каната", "999", "165", "")
        self.assertEqual(scope, "ЛОС")
        self.assertIn(4, related)

    def test_primary_schedule_row_anchors_scope_without_leaving_page(self):
        texts = fixture()
        texts[4] = texts[4].replace("Зажимы для троса</td><td></td><td>шт.</td><td>328</td>",
                                    "Зажимы для каната</td><td></td><td>шт.</td><td>328</td>")
        texts[5] = texts[5].replace("Зажимы для каната", "Зажимы для шланга")
        scope, related = PageTextIndex(texts).related_context(
            [4], "Зажим для каната", "328", "", "")
        self.assertEqual(scope, "ЛОС")
        self.assertNotIn(5, related)

    def test_operation_term_mismatch_does_not_anchor_scope(self):
        scope, related = PageTextIndex(fixture()).related_context(
            [1], "Скрепление плит зажимами для шланга", "999", "165", "")
        self.assertEqual((scope, related), ("", []))

    def test_missing_relation_yields_no_fabricated_link(self):
        scope, related = PageTextIndex(
            ["Титул", "Площадь крепления 1879 м2", "Разное 1879 шт"]
        ).related_context([1], "Укладка плит", "281.85", "1879*0.15", "")
        self.assertEqual((scope, related), ("", []))

    def test_scoped_measurement_rejects_foreign_binding(self):
        quote = ("Крепление плитами. Площадь крепления - 526м2 "
                 "(площадка КНС) и 1879м2 (площадка ЛОС).")
        self.assertEqual(measurement_scopes(quote, "526", "м2"), {"КНС"})
        self.assertEqual(measurement_scopes(quote, "1879", "м2"), {"ЛОС"})

    def test_section_proof_in_foreign_scope_stays_foreign(self):
        scopes = section_scopes("Сечение 2-2 площадки КНС. Толщина 0,15 м")
        self.assertEqual(scopes, {"2-2": {"КНС"}})

    def test_ocr_split_label_still_anchors_scope(self):
        scope, related = PageTextIndex(
            ["Титул", "План площадки<br>ЛОС. Площадь 1879м2 (площадка ЛОС). Сечение 2-2.",
             "<b>Сечение 2-2</b>\nПлощадка ЛОС"]
        ).related_context([1], "Укладка плит", "281.85", "1879*0.15", "")
        self.assertEqual((scope, related), ("ЛОС", [2]))

    def test_float_tail_volume_still_anchors_schedule_scope(self):
        spec = ("<table><tr><th>Наименование и техническая характеристика</th>"
                "<th>Поставщик</th></tr>"
                "<tr><td>Берегоукрепление площадки КНС</td><td></td></tr>"
                "<tr><td>Геотекстиль Дорнит плотностью 500 г/м2</td>"
                "<td>м2</td><td>631,2</td></tr></table>")
        scope, _ = PageTextIndex(["Титул", spec]).related_context(
            [1], "Геотекстиль Дорнит, плотностью 500 г/м2",
            "631.1999999999999", "526*1,2", "")
        self.assertEqual(scope, "КНС")

    def test_adjacent_cells_do_not_merge_into_thousands(self):
        spec = ("<table><tr><th>Наименование и техническая характеристика</th>"
                "<th>Поставщик</th></tr>"
                "<tr><td>Берегоукрепление площадки КНС</td><td></td></tr>"
                "<tr><td>Гибкие бетонные плиты ГБП</td><td></td><td></td>"
                "<td></td><td>шт.</td><td>165</td><td>831</td></tr></table>")
        scope, _ = PageTextIndex(["Титул", spec]).related_context(
            [1], "Гибкие бетонные плиты ГБП", "165", "", "")
        self.assertEqual(scope, "КНС")

    def test_explicit_scope_pins_subject_despite_foreign_quantity(self):
        texts = fixture()
        scope, related = PageTextIndex(texts).related_context(
            [1], "Геотекстиль Дорнит", "2254,8", "1879*1,2", "",
            expected_scope="КНС")
        self.assertEqual(scope, "КНС")
        self.assertNotIn(5, related)

    def test_explicit_empty_scope_never_derives_group_from_quantity(self):
        texts = fixture()
        scope, related = PageTextIndex(texts).related_context(
            [1], "Укладка плит зажимами для каната", "165", "165*3", "",
            expected_scope="")
        self.assertEqual((scope, related), ("", []))

    def test_unknown_scope_hint_fails_closed(self):
        texts = fixture()
        scope, related = PageTextIndex(texts).related_context(
            [1], "Укладка плит", "165", "165*3", "",
            expected_scope="НЕИЗВЕСТНЫЙ")
        self.assertEqual((scope, related), ("", []))


if __name__ == "__main__":
    unittest.main()
