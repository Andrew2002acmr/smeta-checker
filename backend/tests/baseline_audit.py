"""Deterministic offline baseline audit: fixed inputs, scripted LLM, diff."""

import argparse
import hashlib
import io
import json
import os
import re
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = (
    {"work": "Copper cable", "volume": 45, "unit": "m", "page": 2},
    {"work": "Copper cable", "volume": 45, "unit": "m", "page": 1},
    {"work": "Circulating pump", "volume": 3, "unit": "pcs", "page": 3},
    {"work": "Steel fence", "volume": 10, "unit": "m", "page": 1},
    {"work": "Failing device", "volume": 1, "unit": "pcs", "page": 2},
)

HEADER = ["№ п.п.", "Наименование работ, ресурсов, затрат по проекту",
          "Объём работ / Количество", "Ед. изм.", "Ссылка на ПД",
          "Наименование файла", "Номер страниц (через пробел)"]

PAGE_TEXTS = (
    "Project overview. This PDF page is counted from 1; footer sheet numbers are ignored. ",
    "Copper cable specification. The audited work is Copper cable with quantity 45 m. ",
    "Pump equipment specification. The audited work is Circulating pump with quantity 2 pcs. ",
)


def _block_network():
    import http.client
    import urllib.request

    def guarded(*args, **kwargs):
        raise RuntimeError("Baseline is offline: network access is disabled")
    return (patch.object(socket, "create_connection", guarded),
            patch.object(http.client.HTTPConnection, "connect", guarded),
            patch.object(urllib.request, "urlopen", guarded))

class BaselineClient:
    """Scripted Responses-compatible stub: five deterministic outcomes."""

    calls = 0

    def __init__(self, **kwargs):
        self.responses = SimpleNamespace(create=self.create)

    @classmethod
    def create(cls, *, model, instructions, input, max_output_tokens, **_):
        cls.calls += 1
        work = re.search(r"Проверяемая работа:\s*([^\n]+)", input)
        work = work.group(1).strip() if work else ""
        fragments = {}
        for page in re.finditer(
                r"\[СТРАНИЦА (\d+) В PDF[^\]]*\]:\n((?:\[[a-z0-9]+\].*\n?)+)", input):
            sources = dict(re.findall(r"\[([a-z0-9]+)\]\s*(.*)", page.group(2)))
            fragments[int(page.group(1))] = sources
        if work == "Failing device":
            raise RuntimeError("Simulated provider failure")
        if work == "Steel fence":
            return cls.negative()
        marker = "Copper cable" if work == "Copper cable" else "Circulating pump"
        for page in sorted(fragments):
            for source_id, text in fragments[page].items():
                if marker in text:
                    if work == "Copper cable":
                        return cls.positive(source_id, page, "45", "m", "Confirmed")
                    return cls.positive(source_id, page, "2", "pcs",
                                        "Volume differs: VOR 3 pcs, PD 2 pcs")
        return cls.negative()
    @classmethod
    def positive(cls, source_id, page, volume, unit, details):
        data = {"claims": [
                    {"claim_id": "presence", "source_ids": [source_id]},
                    {"claim_id": "quantity", "source_ids": [source_id],
                     "value_source_id": source_id, "unit_source_id": source_id},
                    {"claim_id": "unit", "source_ids": [source_id]}],
                "discrepancy_details": details}
        return SimpleNamespace(output_text=json.dumps(data),
                               usage=SimpleNamespace(total_tokens=100))

    @classmethod
    def negative(cls):
        data = {"claims": [],
                "discrepancy_details": "Work not found in these fragments"}
        return SimpleNamespace(output_text=json.dumps(data),
                               usage=SimpleNamespace(total_tokens=100))

    def close(self):
        pass


def build_inputs(folder):
    import pymupdf

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as pdf:
        for text in PAGE_TEXTS:
            page = pdf.new_page()
            page.insert_textbox(pymupdf.Rect(50, 50, 500, 600), text * 12)
        pdf.save(folder / "project.pdf")
    import csv

    with (folder / "vor.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(HEADER)
        for idx, case in enumerate(CASES, 1):
            writer.writerow([idx, case["work"], case["volume"], case["unit"], "",
                             "project.pdf", case["page"]])
    (folder / "expected.json").write_text(
        json.dumps([{"position": str(i), **case} for i, case in enumerate(CASES, 1)],
                   ensure_ascii=False, indent=2), encoding="utf-8")


def logical_input_hash(folder):
    folder = Path(folder)
    payload = {(folder / "vor.csv").read_bytes().decode("utf-8-sig"),
               (folder / "expected.json").read_text(encoding="utf-8")}
    import pymupdf

    with pymupdf.open(folder / "project.pdf") as pdf:
        payload.add("\n".join(page.get_text(sort=True).strip() for page in pdf))
    digest = hashlib.sha256()
    for chunk in sorted(payload):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def versions():
    info = {"python": sys.version.split()[0]}
    for module, key in (("openai", "openai"), ("fitz", "pymupdf"),
                        ("pandas", "pandas"), ("docx", "python-docx")):
        try:
            imported = __import__(module)
            info[key] = getattr(imported, "__version__", "?")
        except Exception:
            info[key] = "?"
    return info


def summarize(saved, inputs):
    import docx

    from export_checks import word_matches, word_status_for

    table, tracking = saved["df"], saved["tracking"]
    word = docx.Document(io.BytesIO(saved["word_bytes"]))
    cases = []
    for (_, row), track in zip(table.iterrows(), tracking, strict=False):
        position = str(row.iloc[0]).removesuffix(".0")
        cases.append({
            "position": position,
            "work": track.get("work_name", ""),
            "status": str(row["Статус аудита"]),
            "page": str(row["[ПД] Фактическая страница"]),
            "found_volume": str(row["[ПД] Найденный объем (нейросеть)"]),
            "found_unit": str(row["[ПД] Найденная единица"]),
            "found_pages": list(track.get("found_pages", [])),
            "work_found": bool(track.get("work_found")),
            "volume_confirmed": bool(track.get("volume_confirmed")),
            "unit_confirmed": bool(track.get("unit_confirmed")),
            "comment_confirmed": bool(track.get("comment_confirmed")),
            "search_complete": bool(track.get("search_complete")),
            "requests": int(track.get("requests", 0)),
            "contexts": int(track.get("contexts", 0)),
            "evidence": track.get("evidence", []),
            "details": str(row["Детали расхождений"]),
            "word_status": word_status_for(word, position),
            "exports_match": bool(word_matches(word, row, track)),
        })
    return {
        "positions": len(cases),
        "logical_hash": logical_input_hash(inputs),
        "versions": versions(),
        "offline": BaselineClient.calls > 0,
        "api_calls": BaselineClient.calls,
        "fake_calls": BaselineClient.calls,
        "status_counts": {status: sum(c["status"] == status for c in cases)
                          for status in ("ОК", "Ошибка", "Неполная проверка", "Технический сбой")},
        "cases": cases,
    }


def run_baseline():
    import pymupdf

    from audit_models import AuditSettings
    from audit_service import run_audit

    BaselineClient.calls = 0
    connection_guard, http_guard, url_guard = _block_network()
    with tempfile.TemporaryDirectory(prefix="smeta-baseline-") as folder:
        folder = Path(folder)
        build_inputs(folder)
        pool = {"project.pdf": pymupdf.open(folder / "project.pdf")}
        try:
            with patch.dict(os.environ, {"YANDEX_CLOUD_API_KEY": "test-only",
                                         "YANDEX_CLOUD_FOLDER": "test-folder",
                                         }), \
                    connection_guard, http_guard, url_guard:
                settings = AuditSettings(
                    yandex_cloud_folder="test-folder",
                    yandex_cloud_api_key="test-only",
                    allowed_llm_hosts=("ai.api.cloud.yandex.net",),
                )
                outcome = run_audit(
                    {"vor.csv": str(folder / "vor.csv")}, ["vor.csv"], pool,
                    api_key="test-only", folder="test-folder",
                    model_name="qwen-test",
                    client_factory=lambda **kwargs: BaselineClient(**kwargs),
                    allowed_llm_hosts=settings.allowed_llm_hosts,
                )
                assert not outcome["issues"], outcome["issues"]
                saved = outcome["results"]["vor.csv"]
                return summarize(saved, folder)
        finally:
            for pdf in pool.values():
                pdf.close()


def normalize_evidence(evidence):
    return sorted((item.get("page"), item.get("kind"), item.get("quote"))
                  for item in evidence or [])


def compare_summaries(old, new):
    if old["positions"] != new["positions"]:
        raise ValueError("Baseline comparison requires the same five positions")
    rows, changes = [], []
    fields = ("status", "page", "found_volume", "found_unit", "work_found",
              "volume_confirmed", "unit_confirmed", "comment_confirmed",
              "search_complete", "requests", "contexts",
              "evidence", "word_status", "exports_match")
    for before, after in zip(old["cases"], new["cases"], strict=False):
        if before["position"] != after["position"] or before["work"] != after["work"]:
            raise ValueError("Position order changed; inputs are not comparable")
        row = {"position": before["position"], "work": before["work"],
               "before": {k: before[k] for k in fields},
               "after": {k: after[k] for k in fields}, "changed": False}
        for field in fields:
            left = (normalize_evidence(before[field]) if field == "evidence"
                    else before[field])
            right = (normalize_evidence(after[field]) if field == "evidence"
                     else after[field])
            if left != right:
                row["changed"] = True
                changes.append({"position": before["position"], "field": field,
                                "before": before[field], "after": after[field]})
        rows.append(row)
    totals = {
        "old_incomplete": sum(c["status"] == "Неполная проверка" for c in old["cases"]),
        "new_incomplete": sum(c["status"] == "Неполная проверка" for c in new["cases"]),
        "old_technical": sum(c["status"] == "Технический сбой" for c in old["cases"]),
        "new_technical": sum(c["status"] == "Технический сбой" for c in new["cases"]),
        "old_requests": sum(c["requests"] for c in old["cases"]),
        "new_requests": sum(c["requests"] for c in new["cases"]),
    }
    return {"identical_inputs": old["logical_hash"] == new["logical_hash"],
            "changed": bool(changes), "changes": changes, "rows": rows,
            "totals": totals}


def write_report(diff, output):
    lines = ["# Baseline до/после",
             "",
             f"Одинаковые входы: {diff['identical_inputs']}. Изменения: {diff['changed']}.",
             "",
             "| Позиция | Статус | Страница | Объём | Ед. | Запросы | Неполная/Сбой | Изменено |",
             "|---|---|---|---|---|---|---|---|"]
    for row in diff["rows"]:
        before, after = row["before"], row["after"]
        flag = ("неполная" if after["status"] == "Неполная проверка"
                else "сбой" if after["status"] == "Технический сбой" else "-")
        lines.append(
            f"| {row['position']} {row['work']} | {before['status']} → {after['status']} | "
            f"{before['page']} → {after['page']} | {before['found_volume']} → {after['found_volume']} | "
            f"{before['found_unit']} → {after['found_unit']} | {before['requests']} → {after['requests']} | "
            f"{flag} | {'да' if row['changed'] else 'нет'} |")
    lines += ["",
              f"Неполные проверки: {diff['totals']['old_incomplete']} → {diff['totals']['new_incomplete']}.",
              f"Технические сбои: {diff['totals']['old_technical']} → {diff['totals']['new_technical']}.",
              f"Запросы: {diff['totals']['old_requests']} → {diff['totals']['new_requests']}."]
    (output / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline baseline audit and diff")
    parser.add_argument("--output", type=Path, default=Path(".audit_cache/baseline"))
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("OLD", "NEW"),
                        help="Compare two summary.json files instead of running the audit")
    args = parser.parse_args(argv)
    if args.compare:
        old = json.loads(args.compare[0].read_text(encoding="utf-8"))
        new = json.loads(args.compare[1].read_text(encoding="utf-8"))
        diff = compare_summaries(old, new)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "comparison.json").write_text(
            json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(diff, args.output)
        print((args.output / "comparison.md").read_text(encoding="utf-8"))
        return 0 if diff["identical_inputs"] and not diff["changed"] else 1
    args.output.mkdir(parents=True, exist_ok=True)
    summary = run_baseline()
    target = args.output / "summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(target), "status_counts": summary["status_counts"],
                      "logical_hash": summary["logical_hash"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
