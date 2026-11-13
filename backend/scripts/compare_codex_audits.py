"""Compare saved audits of the same pinned documents and selected VOR positions.

An optional reviewed sample JSON has project_id, vor_version_id, reviewer,
and samples. Each sample has position_index, direction, expected_status,
and basis. Optional run_checks map a run UUID to citations_correct
(one true, false or null per examined citation) and calculation_reproducible
(true, false or null).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from uuid import UUID

from codex_contract import DIRECTIONS, PositionConclusion, aggregate_status, position_status


MODE_NAMES = {
    "existing": "Existing Pipeline",
    "codex_multi": "Codex Agent с субагентами",
    "codex_single": "Codex Agent без субагентов",
}
REPORT_FIELDS = {
    "processed_positions": "Обработано позиций",
    "confirmed": "Подтверждено",
    "mismatch": "Доказано расхождений",
    "review": "Требуется ручная проверка",
    "not_checked": "Не проверено",
    "not_applicable": "Неприменимо",
    "confirmed_calculations": "Подтверждено расчётов",
    "checked_normative_conditions": "Проверено нормативных условий",
    "total_duration_ms": "Общее время, мс",
    "positions_per_minute": "Позиций в минуту",
    "conclusions_per_minute": "Законченных заключений в минуту",
    "input_tokens": "Входные токены",
    "output_tokens": "Выходные токены",
    "cached_tokens": "Токены из кэша",
    "reasoning_tokens": "Токены рассуждения",
    "subagents_launched": "Запущено субагентов",
    "chatgpt_limits": "Лимиты ChatGPT",
    "cost": "Денежные расходы",
}


def _normative_hashes(record) -> frozenset[str]:
    config = record["config"]
    if config.get("mode") == "codex":
        pin = config.get("codex_normative") or {}
        if not pin.get("release"):
            raise ValueError("Нет закреплённого нормативного выпуска Codex")
        hashes = [doc.get("sha256") for doc in pin.get("documents", [])]
    else:
        pin = config.get("normative_kb_pin")
        if not pin or not pin.get("sha256"):
            raise ValueError("Исторический запуск не содержит закреплённого нормативного выпуска")
        from normative_service import corpus

        kb = corpus(pin)
        if kb.identity != pin:
            raise ValueError("Нормативный выпуск Existing Pipeline отличается от сохранённой версии")
        with kb.connect() as database:
            hashes = [row["sha256"] for row in database.execute("SELECT sha256 FROM sources")]
    if not hashes or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise ValueError("Не удалось сверить хеши закреплённых нормативных PDF")
    return frozenset(hashes)


def _run_inputs(record, repository) -> tuple:
    if record["status"] != "done":
        raise ValueError(f"Запуск {record['id']} не завершён")
    if record["config"].get("manual_review_only"):
        raise ValueError("Импортированное ручное заключение нельзя сравнить как Existing Pipeline")
    positions = {item["position_index"] for item in record["indexed_results"]}
    selected = {}
    for item in record["config"].get("position_inputs") or []:
        source = item.get("position_source") or {}
        if not isinstance(source.get("sheet"), str) or type(source.get("row")) is not int:
            raise ValueError("Не удалось сверить листы и строки выбранных позиций ВОР")
        selected[item["index"]] = (source["sheet"], source["row"])
    if not positions or selected.keys() != positions:
        raise ValueError(f"Запуск {record['id']} содержит неполный набор результатов")
    pdfs = frozenset(repository.run_pinned_versions(record["id"]).values())
    if not pdfs or not record.get("vor_sha256"):
        raise ValueError(f"Запуск {record['id']} не содержит закреплённых ПД и ВОР")
    return (
        record["project_id"], record["vor_version_id"], record["vor_sha256"],
        pdfs, _normative_hashes(record), selected,
    )


def _existing_statuses(result) -> dict[str, str] | None:
    if result.get("schema_version") != "evidence-assessment-v1":
        return None
    states = {}
    for direction in DIRECTIONS:
        stored_name = "relationships" if direction == "quantities" else direction
        saved = result[stored_name]
        state = saved["state"]
        if state == "possible_mismatch":
            state = "mismatch" if any(
                item.get("state") == "possible_mismatch" and item.get("mismatch_level") == "proved"
                for item in saved["obligations"]
            ) else "review"
        states[direction] = "not_checked" if state == "pending" else state
    return states


def _overall(states: dict[str, str]) -> str:
    for status in ("mismatch", "review", "needs_facts", "not_checked", "confirmed"):
        if status in states.values():
            return status
    return "not_applicable"


def _summarize(record, *, codex: bool) -> tuple[dict, dict[int, dict[str, str]]]:
    metrics = dict.fromkeys(REPORT_FIELDS)
    config = record["config"]
    rows = record["indexed_results"]
    counts: Counter = Counter()
    directions = {}
    if codex:
        telemetry = config.get("codex_metrics") or {}
        for field in metrics:
            metrics[field] = telemetry.get(field)
        metrics["subagents_launched"] = telemetry.get("subagents_started")
        for row in rows:
            saved = dict(row["result"])
            if saved.pop("schema_version", None) != "codex-conclusion-v1":
                raise ValueError("В запуске Codex найден неизвестный формат заключения")
            position = PositionConclusion.model_validate(saved)
            counts[position_status(position)] += 1
            directions[row["position_index"]] = {
                name: aggregate_status(finding.status for key, finding in position.findings() if key == name)
                for name in DIRECTIONS
            }
    else:
        for row in rows:
            states = _existing_statuses(row["result"])
            if states is not None:
                directions[row["position_index"]] = states
                counts[_overall(states)] += 1
        created, finished = record.get("created_at"), record.get("finished_at")
        if created is not None and finished is not None and finished >= created:
            duration = (finished - created).total_seconds() * 1000
            metrics["total_duration_ms"] = duration
            metrics["positions_per_minute"] = len(rows) * 60_000 / duration if duration else None

    if metrics['processed_positions'] is None:
        metrics['processed_positions'] = len(rows)
    if len(directions) == len(rows):
        for key in ("confirmed", "mismatch", "not_checked", "not_applicable"):
            metrics[key] = counts[key]
        metrics["review"] = counts["review"] + counts["needs_facts"]
    metrics["run_id"] = str(record["id"])
    return metrics, directions


def _reference_metrics(reference, identity, runs, directions) -> dict:
    if (reference.get("project_id"), reference.get("vor_version_id")) != tuple(map(str, identity[:2])):
        raise ValueError("Проверенная выборка относится к другой версии ВОР или проекту")
    samples = reference.get("samples")
    reviewer = reference.get("reviewer")
    if not isinstance(samples, list) or not samples or not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Укажите независимого проверяющего и проверенные позиции")
    allowed_statuses = {"confirmed", "mismatch", "needs_facts", "not_applicable"}
    known_runs = {str(record["id"]) for record in runs.values()}
    row_keys = set()
    for sample in samples:
        key = (sample["position_index"], sample["direction"])
        if type(key[0]) is not int or key in row_keys or key[0] not in identity[-1] or key[1] not in DIRECTIONS:
            raise ValueError("Выборка содержит повтор, постороннюю позицию или неизвестное направление")
        basis = sample.get("basis")
        if sample.get("expected_status") not in allowed_statuses or not isinstance(basis, str) or not basis.strip():
            raise ValueError("Для каждого наблюдения требуется проверенный статус и основание")
        checks = sample.get("run_checks") or {}
        for run_id, check in checks.items():
            if run_id not in known_runs:
                raise ValueError("В выборке указан запуск вне сравнения")
            if not isinstance(check, dict) or set(check) - {"citations_correct", "calculation_reproducible"}:
                raise ValueError("Неизвестное поле сверки ссылок и расчёта")
            links = check.get("citations_correct")
            calculation = check.get("calculation_reproducible")
            if links is not None and (not isinstance(links, list) or any(
                value is not None and type(value) is not bool for value in links
            )):
                raise ValueError("Итог сверки ссылок должен содержать true, false или null для каждой ссылки")
            if calculation is not None and type(calculation) is not bool:
                raise ValueError("Итог сверки ссылок и расчёта должен быть true, false или null")
        row_keys.add(key)

    def ratio(values):
        return {"verified": sum(values), "total": len(values),
                "share": sum(values) / len(values) if values else None}

    quality = {}
    for mode, record in runs.items():
        confirm, mismatch, links, calculations, correct = [], [], [], [], []
        for sample in samples:
            decision = directions[mode].get(sample["position_index"], {}).get(sample["direction"])
            expected = sample["expected_status"]
            if decision is not None:
                correct.append(decision == expected)
            if decision == "confirmed":
                confirm.append(decision == expected)
            if decision == "mismatch":
                mismatch.append(decision == expected)
            check = (sample.get("run_checks") or {}).get(str(record["id"]), {})
            links.extend(value for value in check.get("citations_correct") or [] if value is not None)
            if check.get("calculation_reproducible") is not None:
                calculations.append(check["calculation_reproducible"])
        quality[mode] = {
            "correctness": ratio(correct),
            "confirmation_precision": ratio(confirm),
            "mismatch_precision": ratio(mismatch),
            "correct_citation_samples": ratio(links),
            "reproducible_calculation_samples": ratio(calculations),
            "false_confirmations": len(confirm) - sum(confirm) if confirm else None,
            "false_mismatches": len(mismatch) - sum(mismatch) if mismatch else None,
        }
    return quality


def compare_runs(repository, runs: dict[str, dict], reference: dict | None = None) -> dict:
    if len(runs) < 2:
        raise ValueError("Для сравнения нужны два завершённых запуска или больше")
    summaries, directions = {}, {}
    identity = None
    for mode, record in runs.items():
        if mode not in MODE_NAMES:
            raise ValueError("Неизвестный режим сравнения")
        config = record["config"]
        codex = config.get("mode") == "codex"
        if codex != mode.startswith("codex_"):
            raise ValueError(f"Режим запуска {record['id']} не соответствует колонке сравнения")
        if codex and config.get("codex_subagents") is not (mode == "codex_multi"):
            raise ValueError(f"Режим субагентов запуска {record['id']} не соответствует сравнению")
        inputs = _run_inputs(record, repository)
        if identity is None:
            identity = inputs
        elif inputs != identity:
            raise ValueError("Наборы закреплённых ПД, ВОР, нормативов или выбранных позиций различаются")
        summaries[mode], directions[mode] = _summarize(record, codex=codex)

    if "codex_multi" in runs and "codex_single" in runs:
        multi, single = (runs[mode]["config"] for mode in ("codex_multi", "codex_single"))
        for key, label in (("model", "модель"), ("codex_cli_version", "версия Codex CLI")):
            if not multi.get(key) or multi[key] != single.get(key):
                raise ValueError(f"Для сравнения Codex требуется одинаковая {label}")
        if ((multi.get('codex_metrics') or {}).get('audit_protocol_version')
                != (single.get('codex_metrics') or {}).get('audit_protocol_version')):
            raise ValueError('Для сравнения Codex требуется одинаковая версия методики')
        if any(multi.get(key) != single.get(key) for key in
               ('codex_coefficient_clarification', 'codex_finding_clarification')):
            raise ValueError('Для сравнения Codex требуются одинаковые уточнения инженера')

    assert identity is not None
    return {
        "project_id": str(identity[0]), "vor_version_id": str(identity[1]),
        "vor_sha256": identity[2], "pdf_version_ids": sorted(map(str, identity[3])),
        "normative_pdf_sha256": sorted(identity[4]), "runs": summaries,
        "reference_quality": _reference_metrics(reference, identity, runs, directions) if reference is not None else None,
    }


def _format_value(value) -> str:
    if value is None:
        return "нет данных"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, dict) and {"verified", "total", "share"} <= value.keys():
        return f"{value['verified']}/{value['total']} ({value['share']:.1%})" if value["total"] else "нет данных"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False).replace("|", "&#124;")
    return str(value)


def markdown(report: dict) -> str:
    runs = report["runs"]
    rows = ["# Сравнение проверок ВОР", "", f"Версия ВОР — `{report['vor_version_id']}`.", "",
            "| Показатель | " + " | ".join(MODE_NAMES[mode] for mode in runs) + " |",
            "| --- | " + " | ".join("---" for _ in runs) + " |"]
    for label, field in [("Идентификатор запуска", "run_id"), *((label, field) for field, label in REPORT_FIELDS.items())]:
        rows.append("| " + label + " | " + " | ".join(_format_value(metrics[field]) for metrics in runs.values()) + " |")
    if "existing" in runs:
        rows += [
            "", "Для Existing Pipeline время взято из сохранённых отметок создания и завершения.",
            "Сомнительные расхождения Existing Pipeline входят в ручную проверку.",
        ]
    if "codex_multi" in runs and "codex_single" in runs:
        multi = runs["codex_multi"]["total_duration_ms"]
        single = runs["codex_single"]["total_duration_ms"]
        if multi is not None and single is not None and single > 0:
            rows += ["", f"Изменение полного времени с субагентами относительно одиночного запуска — {(multi - single) / single:+.1%}."]
    quality = report["reference_quality"]
    if quality is None:
        rows += ["", "Достоверность не оценивалась. Независимая проверенная выборка не приложена."]
    else:
        rows += ["", "## Оценка на проверенной выборке", "",
                 "| Показатель | " + " | ".join(MODE_NAMES[mode] for mode in runs) + " |",
                 "| --- | " + " | ".join("---" for _ in runs) + " |"]
        for field, label in {
            "correctness": "Корректность проверенной выборки",
            "confirmation_precision": "Точность подтверждений",
            "mismatch_precision": "Точность расхождений",
            "correct_citation_samples": "Корректность проверенных ссылок",
            "reproducible_calculation_samples": "Воспроизводимость расчётов по проверенным выводам",
            "false_confirmations": "Ложные подтверждения",
            "false_mismatches": "Ложные расхождения",
        }.items():
            rows.append("| " + label + " | " + " | ".join(_format_value(quality[mode][field]) for mode in runs) + " |")
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for mode in MODE_NAMES:
        parser.add_argument("--" + mode.replace("_", "-"), type=UUID)
    parser.add_argument("--reference", type=Path, help="Optional independently reviewed sample JSON")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from audit_models import AuditSettings
    from storage_blobs import open_blob_store
    from storage_db import open_repository

    try:
        settings = AuditSettings.load()
        repository = open_repository(
            settings.database_url, blob_store=open_blob_store(settings.blob_store_url), migrate_schema=False,
        )
        runs = {mode: repository.load_run(run_id) for mode in MODE_NAMES if (run_id := getattr(args, mode))}
        reference = json.loads(args.reference.read_text(encoding="utf-8-sig")) if args.reference else None
        report = compare_runs(repository, runs, reference)
    except (KeyError, OSError, ValueError) as exc:
        parser.error(str(exc))
    output = markdown(report) if args.format == "markdown" else json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
