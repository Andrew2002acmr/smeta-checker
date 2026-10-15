"""Exercise the Streamlit audit and exports with a synthetic project and mocked API."""

import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fitz
import pandas as pd
import docx
from streamlit.testing.v1 import AppTest


class FakeClient:
    def __init__(self, **kwargs):
        self.responses = SimpleNamespace(create=self.create)

    def create(self, *, model, instructions, input, max_output_tokens, **_):
        prompt = input
        if "Проверяемая работа: Pump station" in prompt:
            raise RuntimeError("Simulated provider failure")
        data = {
            "status": "Ошибка",
            "work_found": True,
            "volume_confirmed": True,
            "unit_confirmed": True,
            "comment_confirmed": True,
            "found_volume": "45 m",
            "found_unit": "m",
            "found_page": "1",
            "evidence": [
                {"page": 1, "quote": "Copper cable specification", "kind": "work"},
                {"page": 2, "quote": "Copper cable 45 m", "kind": "volume"},
                {"page": 2, "quote": "Copper cable 45 m", "kind": "unit"},
            ],
            "discrepancy_details": "Confirmed with multi-page evidence",
        }
        return SimpleNamespace(output_text=json.dumps(data), usage=SimpleNamespace(total_tokens=100))

    def close(self):
        pass


class AppFlowTests(unittest.TestCase):
    def _run_audit(self, folder, header, rows):
        with fitz.open() as pdf:
            for content in ("Copper cable specification. " * 20, "Copper cable 45 m. " * 20):
                page = pdf.new_page()
                page.insert_textbox(fitz.Rect(50, 50, 500, 600), content)
            pdf.save(folder / "project.pdf")
        with (folder / "vor.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(header)
            writer.writerows(rows)
        with patch.dict(os.environ, {"YANDEX_CLOUD_API_KEY": "test-only", "YANDEX_CLOUD_FOLDER": "test-folder"}), \
                patch("openai.OpenAI", FakeClient), \
                patch("audit_tracing._configured_client", return_value=None):
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=30)
            at.run()
            self.assertEqual(len(at.exception), 0, str(at.exception))
            self.assertEqual([i for i in at.selectbox if i.label in ("Провайдер", "Модель")], [])
            folder_input = next(item for item in at.text_input if item.label == "Путь к папке проекта на диске")
            folder_input.set_value(str(folder)).run()
            self.assertEqual(len(at.exception), 0, str(at.exception))
            next(item for item in at.button if item.label == "🚀 Запустить аудит").click().run()
            self.assertEqual(len(at.exception), 0, str(at.exception))
            return at

    def test_audit_continues_after_failed_row_and_exports_actual_page(self):
        with tempfile.TemporaryDirectory(prefix="smeta-audit-test-") as folder:
            folder = Path(folder)
            at = self._run_audit(
                folder,
                ["№ п.п.", "Наименование работ, ресурсов, затрат по проекту",
                 "Объём работ / Количество", "Ед. изм.", "Ссылка на ПД",
                 "Наименование файла", "Номер страниц (через пробел)"],
                [[1, "Copper cable", 45, "m", "", "project.pdf", 1],
                 [2, "Pump station", 1, "шт", "", "project.pdf", 2],
                 [3, "Copper cable", 45, "m", "", "project.pdf", 2]])
            saved = at.session_state["audit_results"]["vor.csv"]
            table = saved["df"]
            self.assertEqual(table["Статус аудита"].tolist(), ["ОК", "Технический сбой", "Ошибка"])
            self.assertEqual(table["[ПД] Фактическая страница"].tolist(), ["Стр. 1, 2", "-", "Стр. 1, 2"])
            self.assertEqual(table["[ПД] Найденная единица"].tolist(), ["m", "-", "m"])
            self.assertEqual(table["[ПД] Найденные страницы"].tolist(), ["1, 2", "-", "1, 2"])
            self.assertTrue(all("Работа: да" in v for v in table["[ПД] Подтверждения (работа/объём/ед./прим.)"].tolist()[:1] + table["[ПД] Подтверждения (работа/объём/ед./прим.)"].tolist()[2:]))
            self.assertIn("Copper cable 45 m", table["[ПД] Доказательства (цитаты со страницами)"].iloc[0])
            self.assertIn("Copper cable 45 m", table["[ПД] Доказательства (цитаты со страницами)"].iloc[2])
            self.assertEqual(table["[ПД] Поиск завершён"].tolist(), ["да", "нет", "да"])
            tracking = saved["tracking"]
            self.assertEqual(tracking[0]["found_pages"], [1, 2])
            self.assertEqual(tracking[2]["found_pages"], [1, 2])
            self.assertEqual(tracking[0]["found_page"], 1)
            self.assertEqual(len(tracking[0]["evidence"]), 3)
            self.assertEqual(len(tracking[2]["evidence"]), 3)
            excel = pd.read_excel(io.BytesIO(saved["excel_bytes"]))
            self.assertEqual(excel["[ПД] Фактическая страница"].tolist(), ["Стр. 1, 2", "-", "Стр. 1, 2"])
            self.assertEqual(excel["[ПД] Найденные страницы"].tolist(), ["1, 2", "-", "1, 2"])
            self.assertIn("Copper cable 45 m", str(excel["[ПД] Доказательства (цитаты со страницами)"].iloc[0]))
            word = docx.Document(io.BytesIO(saved["word_bytes"]))
            self.assertEqual(word.tables[0].rows[1].cells[5].text, "Стр. 1, 2")
            self.assertEqual(word.tables[0].rows[2].cells[6].text, "Технический сбой")
            self.assertIn("1, 2", word.tables[0].rows[3].cells[7].text)
            self.assertIn("Copper cable 45 m", word.tables[0].rows[3].cells[7].text)
            for pdf in at.session_state["pdf_pool"].values():
                pdf.close()

    def test_audit_reads_vor_page_link_column(self):
        with tempfile.TemporaryDirectory(prefix="smeta-audit-test-") as folder:
            folder = Path(folder)
            at = self._run_audit(
                folder,
                ["№ п.п.", "Наименование работ, ресурсов, затрат по проекту",
                 "Объём работ / Количество", "Ед. изм.", "Ссылка на чертежи",
                 "Наименование файла", "[ВОР] Ссылка на страницу"],
                [[1, "Copper cable", 45, "m", "", "project.pdf", 1]])
            saved = at.session_state["audit_results"]["vor.csv"]
            table = saved["df"]
            self.assertEqual(table["Статус аудита"].tolist(), ["ОК"])
            self.assertNotIn("Страница не указана", table["Детали расхождений"].iloc[0])
            self.assertEqual(table["[ВОР] Ссылка на страницу"].tolist(), [1])
            for pdf in at.session_state["pdf_pool"].values():
                pdf.close()

    def test_audit_rerun_overwrites_stale_results(self):
        with tempfile.TemporaryDirectory(prefix="smeta-audit-test-") as folder:
            folder = Path(folder)
            at = self._run_audit(
                folder,
                ["№ п.п.", "Наименование работ, ресурсов, затрат по проекту",
                 "Объём работ / Количество", "Ед. изм.", "Ссылка на чертежи",
                 "Наименование файла", "[ВОР] Ссылка на страницу",
                 "Статус аудита", "Детали расхождений",
                 "[ПД] Найденный объем (нейросеть)", "[ПД] Фактическая страница"],
                [[1, "Copper cable", 45, "m", "", "project.pdf", 1,
                  "Технический сбой", "stale", "-", "-"]])
            saved = at.session_state["audit_results"]["vor.csv"]
            table = saved["df"]
            self.assertEqual(list(table.columns).count("Статус аудита"), 1)
            self.assertEqual(table["Статус аудита"].tolist(), ["ОК"])
            for pdf in at.session_state["pdf_pool"].values():
                pdf.close()



if __name__ == "__main__":
    unittest.main()
