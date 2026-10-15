"""Explicit, opt-in live Qwen audit of synthetic documents (uses API quota).

Run: python tests/run_live_yandex.py
Inputs and outputs are retained under test_documents/yandex_qwen/.
"""

import io
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docx
import pandas as pd
import pymupdf
from dotenv import dotenv_values
from openai.resources.responses import Responses
from streamlit.testing.v1 import AppTest


def main():
    settings = {**os.environ, **dotenv_values(ROOT / ".env", encoding="utf-8-sig")}
    if not settings.get("YANDEX_CLOUD_API_KEY") or not settings.get("YANDEX_CLOUD_FOLDER"):
        raise RuntimeError("Заполните YANDEX_CLOUD_API_KEY и YANDEX_CLOUD_FOLDER в .env")
    folder = ROOT / "test_documents" / "yandex_qwen"
    inputs = folder / "input"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outputs = folder / "results" / stamp
    inputs.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)

    # Deliberately different PDF page numbers and footer sheet numbers.
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
    if not font_path.exists():
        raise RuntimeError("Для русскоязычного тестового PDF нужен шрифт Arial")
    page_texts = [
        "Общие сведения о проекте. Испытательный комплект для аудита ВОР. "
        "Настоящий документ содержит сведения об объеме работ по монтажу инженерных сетей. "
        "Колонтитул содержит номер листа проекта, а ссылка ВОР использует номер страницы PDF. "
        "Объемы работ приведены в спецификациях на следующих страницах.",
        "Спецификация кабельных линий. Работа: Прокладка медного кабеля ВВГнг 3х2,5. "
        "Объем работ: 45 м. Единица измерения: м (метр). "
        "Кабель прокладывается в защитной трубе в помещении технического обслуживания. "
        "Заявленный объем подтверждается спецификацией кабельных линий. "
        "Никакие другие работы на этой странице не приведены.",
        "Спецификация насосного оборудования. Работа: Монтаж циркуляционного насоса. "
        "Количество: 2 шт. Единица измерения: шт (штука). "
        "Предусмотрены два одинаковых насоса для системы отопления. "
        "Монтаж включает установку оборудования на подготовленные основания. "
        "Другие объемы и количества насосов в данном комплекте не предусмотрены.",
    ]
    with pymupdf.open() as pdf:
        for idx, text in enumerate(page_texts):
            page = pdf.new_page()
            page.insert_font(fontname="testarial", fontfile=str(font_path))
            remaining = page.insert_textbox(pymupdf.Rect(50, 50, 545, 650), text,
                                            fontname="testarial", fontsize=12)
            if remaining < 0:
                raise RuntimeError("Текст тестового PDF не помещается на странице")
            page.insert_text((50, 780), f"Номер листа в колонтитуле: {101 + idx}",
                             fontname="testarial", fontsize=10)
        pdf.save(inputs / "project.pdf")

    cases = [
        {"name": "Верная ссылка", "work": "Прокладка медного кабеля ВВГнг 3х2,5", "volume": 45, "unit": "м", "page": 2, "expected_status": "ОК", "expected_page": "Стр. 2"},
        {"name": "Неверная ссылка — автоматический поиск", "work": "Прокладка медного кабеля ВВГнг 3х2,5", "volume": 45, "unit": "м", "page": 1, "expected_status": "Ошибка", "expected_page": "Стр. 2"},
        {"name": "Несовпадение объема", "work": "Монтаж циркуляционного насоса", "volume": 3, "unit": "шт", "page": 3, "expected_status": "Ошибка", "expected_page": "Стр. 3"},
        {"name": "Работа не найдена в ограниченном контексте", "work": "Установка металлического ограждения", "volume": 10, "unit": "м", "page": 1, "expected_status": "Неполная проверка", "expected_page": "-"},
    ]
    rows = [{"№ п.п.": idx + 1, "Наименование работ, ресурсов, затрат по проекту": case["work"],
             "Объем работ / Количество": case["volume"], "Ед. изм.": case["unit"],
             "Ссылка на ПД": "Спецификация проектной документации", "Наименование файла": "project.pdf",
             "Номер страниц (через пробел)": case["page"]} for idx, case in enumerate(cases)]
    pd.DataFrame(rows).to_excel(inputs / "vor.xlsx", index=False)
    (inputs / "expected.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    records = []
    records_lock = threading.Lock()
    original_create = Responses.create

    def traced_create(client, *args, **kwargs):
        start = time.monotonic()
        pages = re.findall(r"\[СТРАНИЦА (\d+)", str(kwargs.get("input", "")))
        record = {"pages": list(dict.fromkeys(map(int, pages))),
                  "model": settings.get("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest"}
        print(f"Запрос Qwen: страницы {record['pages']}", flush=True)
        try:
            result = original_create(client, *args, **kwargs)
            record.update(http_status=200, response_text=getattr(result, "output_text", ""))
            usage = getattr(result, "usage", None)
            if usage:
                record["usage"] = usage.model_dump()
            return result
        except Exception as exc:
            record.update(http_status=getattr(exc, "status_code", None), error_type=type(exc).__name__)
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - start, 3)
            with records_lock:
                records.append(record)
                (outputs / "api_calls.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Ответ Qwen: HTTP {record.get('http_status')}, {record['duration_seconds']} с", flush=True)

    print(f"Тестовые документы: {inputs}", flush=True)
    print(f"Результаты: {outputs}", flush=True)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=600)
    with patch.object(Responses, "create", traced_create):
        app.run()
        if app.exception:
            raise RuntimeError("Приложение не запустилось")
        next(item for item in app.text_input if item.label == "Путь к папке проекта на диске").set_value(str(inputs))
        app.run()
        if app.exception:
            raise RuntimeError("Не удалось загрузить тестовые документы")
        next(item for item in app.button if item.label == "🚀 Запустить аудит").click().run()
    if app.exception:
        raise RuntimeError("Аудит завершился исключением приложения")
    saved = app.session_state["audit_results"]["vor.xlsx"]
    (outputs / "audit.xlsx").write_bytes(saved["excel_bytes"])
    (outputs / "audit.docx").write_bytes(saved["word_bytes"])
    checks = []
    table = saved["df"]
    excel = pd.read_excel(io.BytesIO(saved["excel_bytes"]))
    word = docx.Document(io.BytesIO(saved["word_bytes"]))
    for idx, (case, (_, row)) in enumerate(zip(cases, table.iterrows())):
        status = str(row["Статус аудита"])
        page = str(row["[ПД] Фактическая страница"])
        actual = {"case": case["name"], "status": status, "page": page,
                  "found_volume": str(row["[ПД] Найденный объем (нейросеть)"]),
                  "details": str(row["Детали расхождений"]),
                  "passed": status == case["expected_status"] and page == case["expected_page"],
                  "exports_match": str(excel.iloc[idx]["[ПД] Фактическая страница"]) == page
                                   and word.tables[0].rows[idx + 1].cells[5].text == page}
        checks.append(actual)
    summary = {"model": settings.get("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest",
               "api_calls": len(records), "cases": checks,
               "all_passed": all(case["passed"] and case["exports_match"] for case in checks)}
    (outputs / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    for pdf in app.session_state["pdf_pool"].values():
        pdf.close()
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
