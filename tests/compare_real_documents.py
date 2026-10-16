"""Run real documents through an unchanged app, with real Yandex Qwen requests."""

import argparse
from collections import Counter
from concurrent.futures import Future
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
import pymupdf
from dotenv import dotenv_values
from openai.resources.responses import Responses
from streamlit.testing.v1 import AppTest
from export_checks import exports_match


def file_hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.iterdir()) if p.suffix.lower() in {".xlsx", ".pdf"}}


def is_technical(status, details):
    return status == "Технический сбой" or any(marker in details.lower() for marker in
        ("не удалось извлечь json", "сбой api", "лимит api", "сбой обработки позиции"))


def run(label, app_path, inputs, output):
    output.mkdir(parents=True, exist_ok=True)
    settings = dotenv_values(ROOT / ".env", encoding="utf-8-sig")
    for key in ("YANDEX_CLOUD_API_KEY", "YANDEX_CLOUD_FOLDER", "YANDEX_CLOUD_MODEL"):
        if settings.get(key):
            os.environ[key] = settings[key]
    model = settings.get("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest"
    hashes_before = file_hashes(inputs)
    records, partial = [], {}
    lock = threading.Lock()
    original_create, original_result = Responses.create, Future.result
    original_ocr = pymupdf.Page.get_textpage_ocr
    ocr_count = 0

    def trace_ocr(page, *args, **kwargs):
        nonlocal ocr_count
        result = original_ocr(page, *args, **kwargs)
        ocr_count += 1
        if ocr_count % 10 == 0:
            print(f"[{label}] OCR: {ocr_count} страниц обработано", flush=True)
        return result

    def trace_result(future, *args, **kwargs):
        result = original_result(future, *args, **kwargs)
        if (isinstance(result, tuple) and len(result) == 3 and isinstance(result[0], int)
                and isinstance(result[1], dict) and "Статус аудита" in result[1]):
            idx, values, tracking = result
            with lock:
                if idx not in partial:
                    partial[idx] = {"index": idx, "values": values, "tracking": tracking}
                    (output / "partial_rows.json").write_text(
                        json.dumps(list(partial.values()), ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"[{label}] Позиция {idx + 1}: {values['Статус аудита']}; "
                          f"{values['Фактическая страница в ПД (найдено моделью)']} (завершено {len(partial)})", flush=True)
        return result

    def trace_create(client, *args, **kwargs):
        started = time.monotonic()
        prompt = str(kwargs.get("input", ""))
        pages = re.findall(r"(?:\[СТРАНИЦА|СТРАНИЦА)\s+(\d+)", prompt)
        work = re.search(r"Проверяемая работа:\s*([^\n]+)", prompt)
        record = {"pages_in_request": list(dict.fromkeys(int(p) for p in pages)),
                  "work": work.group(1).strip('"') if work else None,
                  "model": model, "input_characters": len(prompt)}
        try:
            result = original_create(client, *args, **kwargs)
            record.update(http_status=200, response_text=getattr(result, "output_text", ""),
                          response_status=getattr(result, "status", None))
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
                with (output / "api_calls.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                if len(records) <= 5 or len(records) % 10 == 0 or record.get("http_status") != 200:
                    print(f"[{label}] API: {len(records)} запросов; HTTP {record.get('http_status')}; "
                          f"страницы {record['pages_in_request']}", flush=True)

    started = time.monotonic()
    (output / "run_metadata.json").write_text(json.dumps({
        "label": label, "model": model, "input_hashes": hashes_before,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=app_path.parent, text=True).strip(),
        "started_at": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
    app = AppTest.from_file(str(app_path), default_timeout=3600)
    error = None
    try:
        with patch.object(Responses, "create", trace_create), patch.object(Future, "result", trace_result), \
             patch.object(pymupdf.Page, "get_textpage_ocr", trace_ocr):
            app.run()
            if app.exception:
                raise RuntimeError("Исключение запуска приложения")
            next(item for item in app.text_input if item.label == "Путь к папке проекта на диске").set_value(str(inputs))
            app.run()
            if app.exception:
                raise RuntimeError("Исключение загрузки документов")
            next(item for item in app.button if item.label == "🚀 Запустить аудит").click().run()
            if app.exception:
                raise RuntimeError("Исключение аудита")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    rows, report_files = [], []
    saved_results = app.session_state["audit_results"]
    for vor_name, saved in saved_results.items():
        stem = Path(vor_name).stem
        (output / f"{stem}-audit.xlsx").write_bytes(saved["excel_bytes"])
        (output / f"{stem}-audit.docx").write_bytes(saved["word_bytes"])
        report_files.append({"source": vor_name, "excel": f"{stem}-audit.xlsx", "word": f"{stem}-audit.docx"})
        excel = pd.read_excel(io.BytesIO(saved["excel_bytes"]))
        word = docx.Document(io.BytesIO(saved["word_bytes"]))
        table = saved["df"]
        table.to_json(output / f"{stem}-internal.json", orient="table", force_ascii=False)
        (output / f"{stem}-tracking.json").write_text(
            json.dumps(saved["tracking"], ensure_ascii=False), encoding="utf-8")
        name_col = next(c for c in table.columns if "наименование работ" in c.lower())
        for idx, (_, row) in enumerate(table.iterrows()):
            status = str(row["Статус аудита"])
            page = str(row["[ПД] Фактическая страница"])
            details = str(row["Детали расхождений"])
            record = {"position": str(row.iloc[0]), "work": str(row[name_col]),
                      "vor_pages": str(row.get("[ВОР] Ссылка на страницу", "-")),
                      "vor_volume": str(row.get("[ВОР] Заявленный объем", "-")),
                      "vor_unit": str(row.get("[ВОР] Ед. изм.", "-")),
                      "status": status, "page": page, "found_volume": str(row["[ПД] Найденный объем (нейросеть)"]),
                      "details": details, "technical_failure": is_technical(status, details),
                      "exports_match": exports_match(excel, word, row, saved["tracking"][idx])}
            tracking = saved["tracking"][idx]
            record.update(
                evidence=tracking.get("evidence", []),
                found_pages=tracking.get("found_pages", []),
                work_found=tracking.get("work_found"),
                volume_confirmed=tracking.get("volume_confirmed"),
                unit_confirmed=tracking.get("unit_confirmed"),
                comment_confirmed=tracking.get("comment_confirmed"),
                search_complete=tracking.get("search_complete"),
                requests=tracking.get("requests", 0))
            rows.append(record)
    try:
        run_issues = app.session_state["audit_run_issues"]
    except KeyError:
        run_issues = []
    summary = {"label": label, "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=app_path.parent, text=True).strip(),
               "working_tree_changes": label == "current", "model": model,
               "input_hashes": hashes_before, "inputs_unchanged": hashes_before == file_hashes(inputs),
               "application_error": error, "elapsed_seconds": round(time.monotonic() - started, 3),
               "api_calls": len(records), "ocr_pages": ocr_count,
               "total_tokens": sum(r.get("usage", {}).get("total_tokens", 0) for r in records),
               "status_counts": dict(Counter(row["status"] for row in rows)),
               "technical_failures": sum(row["technical_failure"] for row in rows),
               "found_page_count": sum(row["page"] != "-" for row in rows),
               "incomplete_checks": sum(row.get("search_complete") is False for row in rows),
               "max_requests_per_position": max((row["requests"] for row in rows), default=0),
               "verified_evidence_count": sum(len(row["evidence"]) for row in rows),
               "rows": rows, "reports": report_files,
               "run_issues": run_issues}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k not in ("rows", "input_hashes", "reports")}, ensure_ascii=False, indent=2), flush=True)
    for pdf in app.session_state["pdf_pool"].values():
        if not pdf.is_closed:
            pdf.close()
    return 1 if error else 0


def compare(output):
    main = json.loads((output / "main" / "summary.json").read_text(encoding="utf-8"))
    current = json.loads((output / "current" / "summary.json").read_text(encoding="utf-8"))
    identical = main["input_hashes"] == current["input_hashes"] and main["inputs_unchanged"] and current["inputs_unchanged"]
    if not identical or len(main["rows"]) != len(current["rows"]) or current["application_error"]:
        raise RuntimeError("Сравнение требует завершенных отчетов по всем позициям на одинаковых исходниках")
    rows = []
    for old, new in zip(main["rows"], current["rows"]):
        if old["position"] != new["position"] or old["work"] != new["work"]:
            raise RuntimeError("Порядок позиций в отчетах различается")
        rows.append({"position": old["position"], "work": old["work"], "vor_pages": old["vor_pages"],
                     "main_status": old["status"], "main_page": old["page"], "main_volume": old["found_volume"],
                     "current_status": new["status"], "current_page": new["page"], "current_volume": new["found_volume"],
                     "main_technical_failure": old["technical_failure"], "current_technical_failure": new["technical_failure"],
                     "main_details": old["details"], "current_details": new["details"],
                     "changed": any(old[k] != new[k] for k in ("status", "page", "found_volume"))})
    payload = {"identical_inputs": identical, "main": main, "current": current, "comparison": rows}
    (output / "comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(output / "comparison.csv", index=False, encoding="utf-8-sig")
    text = (f"# ВР-1 ИГДИ: main и текущая версия\n\n"
            f"Модель: `{main['model']}`. main: `{main['commit'][:7]}`. Текущая: `{current['commit'][:7]}` с локальными исправлениями.\n\n"
            f"Одинаковые исходные файлы, проверка SHA-256: {identical}. Позиций в отчетах: {len(main['rows'])} / {len(current['rows'])}.\n\n"
            "| Показатель | main | Текущая версия |\n|---|---|---|\n"
            f"| ОК | {main['status_counts'].get('ОК',0)} | {current['status_counts'].get('ОК',0)} |\n"
            f"| Фактическая страница указана | {main['found_page_count']} | {current['found_page_count']} |\n"
            f"| Технические сбои | {main['technical_failures']} | {current['technical_failures']} |\n"
            f"| Запросы API | {main['api_calls']} | {current['api_calls']} |\n"
            f"| Токены | {main['total_tokens']} | {current['total_tokens']} |\n"
            f"| Время, секунды | {main['elapsed_seconds']} | {current['elapsed_seconds']} |\n\n"
            "## По позициям\n\n| № | Ссылка ВОР | main: статус / страница / объем | Текущая: статус / страница / объем |\n|---|---|---|---|\n")
    for row in rows:
        text += f"| {row['position']} | {row['vor_pages']} | {row['main_status']} / {row['main_page']} / {row['main_volume']} | {row['current_status']} / {row['current_page']} / {row['current_volume']} |\n"
    text += "\nКоличество ОК и найденных страниц не доказывает правильность аудита. Расхождения требуют проверки по ПД. Полные пояснения сохранены в comparison.csv и comparison.json.\n"
    (output / "comparison.md").write_text(text, encoding="utf-8")
    print(text, flush=True)


def recover(label, app_path, inputs, output):
    """Recover completed native exports without repeating paid API requests."""
    reports, rows = [], []
    for excel_path in output.glob("*-audit.xlsx"):
        word_path = excel_path.with_suffix(".docx")
        excel = pd.read_excel(excel_path)
        internal_path = excel_path.with_name(excel_path.name.replace("-audit.xlsx", "-internal.json"))
        tracking_path = excel_path.with_name(excel_path.name.replace("-audit.xlsx", "-tracking.json"))
        table = pd.read_json(internal_path, orient="table") if internal_path.exists() else excel
        tracking = json.loads(tracking_path.read_text(encoding="utf-8")) if tracking_path.exists() else [{} for _ in range(len(table))]
        if "[ПД] Фактическая страница" not in table:
            raise RuntimeError(f"{excel_path.name}: для восстановления нужен внутренний JSON аудита")
        word = docx.Document(word_path)
        name_col = next(c for c in table.columns if "наименование работ" in c.lower())
        reports.append({"source": excel_path.name.removesuffix("-audit.xlsx") + ".xlsx",
                        "excel": excel_path.name, "word": word_path.name})
        for idx, (_, row) in enumerate(table.iterrows()):
            status, page, details = str(row["Статус аудита"]), str(row["[ПД] Фактическая страница"]), str(row["Детали расхождений"])
            rows.append({"position": str(row.iloc[0]), "work": str(row[name_col]),
                         "vor_pages": str(row.get("[ВОР] Ссылка на страницу", "-")),
                         "vor_volume": str(row.get("[ВОР] Заявленный объем", "-")),
                         "vor_unit": str(row.get("[ВОР] Ед. изм.", "-")),
                         "status": status, "page": page, "found_volume": str(row["[ПД] Найденный объем (нейросеть)"]),
                         "details": details, "technical_failure": is_technical(status, details),
                         "exports_match": exports_match(excel, word, row, tracking[idx])})
    if not rows:
        raise RuntimeError("Завершенных отчетов для восстановления нет")
    records = [json.loads(line) for line in (output / "api_calls.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    before = file_hashes(inputs)
    metadata_path = output / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    original_hashes = metadata.get("input_hashes", before)
    settings = dotenv_values(ROOT / ".env", encoding="utf-8-sig")
    summary = {"label": label, "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=app_path.parent, text=True).strip(),
               "working_tree_changes": label == "current", "model": settings.get("YANDEX_CLOUD_MODEL") or "qwen3.6-35b-a3b/latest",
               "input_hashes": original_hashes, "inputs_unchanged": original_hashes == before,
               "application_error": None, "elapsed_seconds": None, "api_calls": len(records), "ocr_pages": None,
               "total_tokens": sum(r.get("usage", {}).get("total_tokens", 0) for r in records),
               "status_counts": dict(Counter(row["status"] for row in rows)),
               "technical_failures": sum(row["technical_failure"] for row in rows),
               "found_page_count": sum(row["page"] != "-" for row in rows), "rows": rows, "reports": reports,
               "run_issues": [], "summary_recovered_from_completed_exports": True}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k not in ("rows", "input_hashes", "reports")}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", choices=("main", "current"))
    parser.add_argument("--app", type=Path)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    if args.run:
        if args.recover:
            recover(args.run, args.app.resolve(), args.inputs.resolve(), args.output.resolve())
            raise SystemExit(0)
        raise SystemExit(run(args.run, args.app.resolve(), args.inputs.resolve(), args.output.resolve()))
    compare(args.output.resolve())
