"""Compare main and the working tree using identical existing fixtures and live Qwen."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docx
import pandas as pd
from dotenv import dotenv_values
from openai.resources.responses import Responses
from streamlit.testing.v1 import AppTest
from export_checks import exports_match

INPUTS = ROOT / "test_documents" / "yandex_qwen" / "input"


def technical_failure(case):
    details = case["details"].lower()
    return (case["status"] == "Технический сбой" or
            any(marker in details for marker in ("не удалось извлечь json", "сбой api", "лимит api", "сбой обработки позиции")))


def hashes():
    return {name: hashlib.sha256((INPUTS / name).read_bytes()).hexdigest()
            for name in ("project.pdf", "vor.xlsx", "expected.json")}


def run_branch(label, app_path, output):
    output.mkdir(parents=True, exist_ok=True)
    settings = dotenv_values(ROOT / ".env", encoding="utf-8-sig")
    # Identical credentials/model, including main's environment-first loader.
    for key in ("YANDEX_CLOUD_API_KEY", "YANDEX_CLOUD_FOLDER", "YANDEX_CLOUD_MODEL"):
        if settings.get(key):
            os.environ[key] = settings[key]
    model = settings.get("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest"
    cases = json.loads((INPUTS / "expected.json").read_text(encoding="utf-8"))
    before = hashes()
    records, lock = [], threading.Lock()
    original = Responses.create

    def traced(client, *args, **kwargs):
        started = time.monotonic()
        prompt = str(kwargs.get("input", ""))
        page = re.search(r"(?:СТРАНИЦА|стр\.)\s+(\d+)", prompt)
        record = {"page": int(page.group(1)) if page else None, "model": model,
                  "input_characters": len(prompt)}
        print(f"[{label}] Запрос Qwen: страница {record['page']}", flush=True)
        try:
            result = original(client, *args, **kwargs)
            record.update(http_status=200, response_text=getattr(result, "output_text", ""))
            usage = getattr(result, "usage", None)
            if usage:
                record["usage"] = usage.model_dump()
            return result
        except Exception as exc:
            record.update(http_status=getattr(exc, "status_code", None), error_type=type(exc).__name__)
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            with lock:
                records.append(record)
                (output / "api_calls.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[{label}] HTTP {record.get('http_status')}, {record['duration_seconds']} с", flush=True)

    started = time.monotonic()
    app = AppTest.from_file(str(app_path), default_timeout=600)
    with patch.object(Responses, "create", traced):
        app.run()
        if app.exception:
            raise RuntimeError(f"{label}: исключение при запуске приложения")
        next(item for item in app.text_input if item.label == "Путь к папке проекта на диске").set_value(str(INPUTS))
        app.run()
        if app.exception:
            raise RuntimeError(f"{label}: исключение при загрузке документов")
        next(item for item in app.button if item.label == "🚀 Запустить аудит").click().run()
    elapsed = time.monotonic() - started
    if app.exception:
        raise RuntimeError(f"{label}: исключение при аудите")
    saved = app.session_state["audit_results"]["vor.xlsx"]
    (output / "audit.xlsx").write_bytes(saved["excel_bytes"])
    (output / "audit.docx").write_bytes(saved["word_bytes"])
    excel = pd.read_excel(io.BytesIO(saved["excel_bytes"]))
    word = docx.Document(io.BytesIO(saved["word_bytes"]))
    checks = []
    for idx, (case, (_, row)) in enumerate(zip(cases, saved["df"].iterrows())):
        status, page = str(row["Статус аудита"]), str(row["[ПД] Фактическая страница"])
        volume = str(row["[ПД] Найденный объем (нейросеть)"])
        expected_volume = "45" if idx < 2 else "2" if idx == 2 else "-"
        volume_ok = (expected_volume in re.findall(r"\d+", volume)) if expected_volume != "-" else volume == "-"
        exports_ok = exports_match(excel, word, row, saved["tracking"][idx])
        check = {"case": case["name"], "status": status, "page": page, "found_volume": volume,
                       "details": str(row["Детали расхождений"]), "expected_status": case["expected_status"],
                       "expected_page": case["expected_page"], "expected_volume": expected_volume,
                       "passed": status == case["expected_status"] and page == case["expected_page"] and volume_ok,
                       "exports_match": exports_ok}
        check["technical_failure"] = technical_failure(check)
        check["passed"] = check["passed"] and not check["technical_failure"] and exports_ok
        checks.append(check)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=app_path.parent, text=True).strip()
    summary = {"label": label, "commit": commit, "model": model, "input_hashes": before,
               "inputs_unchanged": before == hashes(), "elapsed_seconds": round(elapsed, 3),
               "api_calls": len(records), "total_tokens": sum(item.get("usage", {}).get("total_tokens", 0) for item in records),
               "cases": checks, "passed_count": sum(item["passed"] for item in checks),
               "working_tree_changes": label == "current"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    for pdf in app.session_state["pdf_pool"].values():
        if not pdf.is_closed:
            pdf.close()


def compare(output):
    main = json.loads((output / "main" / "summary.json").read_text(encoding="utf-8"))
    current = json.loads((output / "current" / "summary.json").read_text(encoding="utf-8"))
    # A parsing/API failure is not a successful negative audit, even if its
    # displayed status and page happen to match the expected negative result.
    for summary in (main, current):
        for case in summary["cases"]:
            case["technical_failure"] = technical_failure(case)
            case["passed"] = case["passed"] and not case["technical_failure"] and case["exports_match"]
        summary["passed_count"] = sum(case["passed"] for case in summary["cases"])
        (output / summary["label"] / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    same_inputs = main["input_hashes"] == current["input_hashes"] and main["inputs_unchanged"] and current["inputs_unchanged"]
    rows = []
    for old, new in zip(main["cases"], current["cases"]):
        rows.append({"Сценарий": old["case"], "Ожидаемый статус": old["expected_status"],
                     "Ожидаемая страница": old["expected_page"], "main: статус": old["status"],
                     "main: страница": old["page"], "main: объем": old["found_volume"],
                     "main: пройден": old["passed"], "Текущая: статус": new["status"],
                     "Текущая: страница": new["page"], "Текущая: объем": new["found_volume"],
                     "Текущая: пройден": new["passed"], "main: детали": old["details"], "Текущая: детали": new["details"]})
    pd.DataFrame(rows).to_csv(output / "comparison.csv", index=False, encoding="utf-8-sig")
    report = {"identical_inputs": same_inputs, "main": main, "current": current}
    (output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = (f"# Сравнение main и текущей рабочей версии\n\n"
            f"main: `{main['commit'][:7]}`. Текущая: `{current['commit'][:7]}` с незакоммиченными исправлениями.\n\n"
            f"Модель: `{main['model']}`. Одинаковые входные файлы (SHA-256): {same_inputs}.\n\n"
            "| Сценарий | main: статус / страница / объем | Текущая: статус / страница / объем |\n"
            "|---|---|---|\n")
    for old, new in zip(main["cases"], current["cases"]):
        text += f"| {old['case']} | {old['status']} / {old['page']} / {old['found_volume']} | {new['status']} / {new['page']} / {new['found_volume']} |\n"
    text += (f"\nПройдено: main {main['passed_count']}/4; текущая {current['passed_count']}/4.\n\n"
             f"Запросов API: main {main['api_calls']}; текущая {current['api_calls']}. "
             f"Токенов: main {main['total_tokens']}; текущая {current['total_tokens']}.\n\n"
             "Один реальный прогон на синтетическом наборе; ответы модели могут различаться при повторных запусках. "
             "Проверялись статусы, страницы, числовые значения фактических объемов и сохранение страниц в Excel/Word. "
             "Сбой API или разбора ответа не засчитывается как успешная проверка.\n\n"
             "Неполная проверка не является доказательством отсутствия работы во всем PDF. "
             "Текущая версия сохраняет найденную работу с замечанием и ограничивает запросы; "
             "фактические единицы и цитаты доступны в расширенном отчете.\n")
    (output / "comparison.md").write_text(text, encoding="utf-8")
    print(text, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", choices=("main", "current"))
    parser.add_argument("--app", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.run:
        run_branch(args.run, args.app.resolve(), args.output.resolve())
    else:
        compare(args.output.resolve())
