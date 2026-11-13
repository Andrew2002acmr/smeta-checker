"""Run the experimental audit queue under the Ubuntu Codex service account."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import signal
from threading import Event
from time import monotonic, monotonic_ns

from types import SimpleNamespace

from audit_models import AuditSettings
from codex_runtime import codex_audit_enabled
from storage_blobs import open_blob_store
from storage_db import open_repository

logger = logging.getLogger(__name__)
LEASE_SECONDS = 180


def _json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def _artifacts(record, metrics, results, terminal, events, error):
    report = [
        "# Производительность Codex Agent", "",
        f"- Запуск {record['id']}",
        f"- Ведомость {record['vor_filename']}",
        f"- Статус {terminal}", "",
    ]
    if error and terminal == "failed":
        report += [error, ""]
    for label, field in (
        ("Общее время, мс", "total_duration_ms"),
        ("Ожидание очереди, мс", "queue_duration_ms"),
        ("Подготовка документов, мс", "preparation_duration_ms"),
        ("Работа Codex, мс", "agent_execution_duration_ms"),
        ("Поиск по документам, мс", "document_search_duration_ms"),
        ("Распознавание, мс", "ocr_duration_ms"),
        ("Поиск по нормативам, мс", "normative_search_duration_ms"),
        ("Расчёты, мс", "calculation_duration_ms"),
        ("Проверка результатов, мс", "validation_duration_ms"),
        ("Запись результатов, мс", "persistence_duration_ms"),
        ("Позиций в минуту", "positions_per_minute"),
        ("Среднее время на позицию, мс", "mean_position_duration_ms"),
        ("Законченные инженерные заключения", "completed_conclusions"),
        ("Позиций с результатом", "processed_positions"),
        ("Модель", "model"),
        ("Входные токены главного агента", "input_tokens"),
        ("Выходные токены главного агента", "output_tokens"),
        ("Ответы модели по журналу", "model_response_count"),
        ("Запущено субагентов", "subagents_started"),
        ("Отклонено ссылок", "invalid_citations"),
        ("Пропущено агентом позиций", "omitted_positions"),
        ("Повторно использовано адресных оснований", "reused_evidence_count"),
    ):
        value = metrics.get(field)
        report.append(f"- {label} — {value if value is not None else 'нет данных'}")
    for label, status in (
        ("Подтверждено", "confirmed"), ("Доказано расхождений", "mismatch"),
        ("Не хватает оснований", "needs_facts"), ("Не проверено", "not_checked"),
        ("Неприменимо", "not_applicable"),
    ):
        value = (metrics.get("overall_statuses") or {}).get(status)
        report.append(f"- {label} — {value if value is not None else 'нет данных'}")
    quota = ((metrics.get('chatgpt_quota') or {}).get('by_bucket') or {}).get('codex') or {}
    if quota:
        windows = quota.get('windows') or {}
        before = (((metrics.get('chatgpt_quota') or {}).get('before') or {}).get('buckets') or {}).get('codex') or {}
        weekly_key = next((key for key, window in (before.get('windows') or {}).items()
                           if window.get('windowDurationMins') == 10080), None)
        window = windows.get(weekly_key) or {}
        delta = window.get('account_delta_percentage_points')
        credits = quota.get('account_credit_balance_decrease')
        report.extend([
            f"- Изменение недельной квоты аккаунта, п. п. — {delta if delta is not None else 'нет данных'}",
            f"- Уменьшение баланса кредитов — {credits if credits is not None else 'нет данных'}",
        ])
    report += ["", "Среднее время на позицию рассчитано по длительности запуска. "
               "Время вызовов инструментов суммируется и может перекрываться между агентами.", ""]
    return {
        "metrics.json": _json(metrics),
        "performance_report.md": "\n".join(report).encode(),
        "audit_results.json": _json({
            "project_id": str(record["project_id"]),
            "vor_version_id": str(record["vor_version_id"]),
            "positions": results,
        }),
        "execution_events.jsonl": "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ).encode(),
    }


def _work_one(context, stop: Event) -> bool:
    from codex_worker import CodexAuditError, execute_codex_run

    claim = context.repository.claim_codex_run(lease_seconds=LEASE_SECONDS)
    if claim is None:
        return False
    run_id, lease_token, queue_ms = claim
    started = monotonic_ns()
    record = context.repository.load_run(run_id, include_results=False)
    metrics = {"queue_duration_ms": queue_ms, "auth_mode": "chatgpt"}
    events = []
    results = []
    metadata = {}
    error = None
    next_heartbeat = 0.0
    last_stage = ""

    def record_stage(stage):
        events.append({"stage": stage, "at": datetime.now(timezone.utc).isoformat(),
                       "elapsed_ms": (monotonic_ns() - started) / 1_000_000})

    def publish(stage, telemetry):
        nonlocal last_stage
        metrics.update(telemetry)
        if last_stage != stage:
            record_stage(stage)
            last_stage = stage
        return context.repository.heartbeat_codex_run(
            run_id, lease_token, lease_seconds=LEASE_SECONDS, stage=stage, metrics=metrics,
        )

    def cancelled():
        nonlocal next_heartbeat
        if stop.is_set():
            return True
        now = monotonic()
        if now < next_heartbeat:
            return False
        next_heartbeat = now + 2
        return not context.repository.heartbeat_codex_run(
            run_id, lease_token, lease_seconds=LEASE_SECONDS,
        )

    try:
        record_stage("claimed")
        results, metadata = execute_codex_run(context, record, cancelled=cancelled, publish=publish)
        metrics.update(metadata.get("metrics", {}))
        events.extend(metadata.get("events", []))
    except Exception as exc:
        # This exception carries a fixed runner error code, never process output.
        kind = str(exc) if isinstance(exc, CodexAuditError) else type(exc).__name__
        logger.error("Codex audit %s failed (%s)", run_id, kind)
        error = f"Проверка Codex прервана ({kind})."
        safe_metrics = getattr(exc, "metrics", None)
        if isinstance(safe_metrics, dict):
            metrics.update(safe_metrics)
        safe_events = getattr(exc, "events", None)
        if isinstance(safe_events, list):
            events.extend(safe_events)
    finally:
        if stop.is_set():
            error = "Воркер Codex остановлен. Запустите проверку повторно."
        for key in (
            "preparation_duration_ms", "agent_execution_duration_ms", "document_search_duration_ms",
            "ocr_duration_ms", "normative_search_duration_ms", "calculation_duration_ms",
            "validation_duration_ms", "input_tokens", "output_tokens", "cached_tokens",
            "reasoning_tokens", "chatgpt_limits", "model_calls", "model_wait_duration_ms",
            "median_position_duration_ms", "p90_position_duration_ms", "p95_position_duration_ms",
        ):
            metrics.setdefault(key, None)
        if metadata.get("model"):
            metrics["model"] = metadata["model"]
        if metadata.get("cli_version"):
            metrics["cli_version"] = metadata["cli_version"]
        submission_ms = record["config"].get("codex_submission_duration_ms")
        if not record["config"].get("codex_attempts") and isinstance(submission_ms, (int, float)):
            metrics["submission_duration_ms"] = submission_ms
        else:
            metrics["submission_duration_ms"] = None
        metrics["total_duration_ms"] = (
            (metrics["submission_duration_ms"] or 0) + queue_ms
            + (monotonic_ns() - started) / 1_000_000
        )
        metrics["total_duration_boundary"] = "position_rows_flushed"
        record_stage("persisting")

        def artifacts(measured, persisted_results, terminal):
            measured["total_duration_ms"] += measured["persistence_duration_ms"]
            count = len(persisted_results) - (measured.get('omitted_positions') or 0)
            minutes = measured["total_duration_ms"] / 60_000
            measured["processed_positions"] = count
            measured["positions_per_minute"] = count / minutes if minutes else None
            measured["positions_per_hour"] = measured["positions_per_minute"] * 60 if minutes else None
            measured["mean_position_duration_ms"] = measured["total_duration_ms"] / count if count else None
            measured["position_duration_basis"] = "whole_vor_average" if count else None
            completed = measured.get("completed_conclusions")
            measured["conclusions_per_minute"] = completed / minutes if minutes and isinstance(completed, int) else None
            measured["vor_duration_ms"] = measured["total_duration_ms"]
            record_stage(terminal)
            ordered = sorted(events, key=lambda event: event.get("at", ""))
            payloads = _artifacts(record, measured, persisted_results, terminal, ordered, error)
            if persisted_results and metadata.get('evidence'):
                payloads['codex_evidence.json'] = _json(metadata['evidence'])
            return payloads

        context.repository.complete_codex_run(
            run_id, lease_token, results=results, error=error,
            metadata={**metadata, "metrics": metrics}, artifact_payloads=artifacts,
        )
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one queued audit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if not codex_audit_enabled():
        raise SystemExit("Set CODEX_AUDIT_ENABLED=1 to enable the Codex audit worker.")
    stop = Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())
    settings = AuditSettings.load()
    if not settings.database_url or not settings.blob_store_url:
        raise SystemExit("DATABASE_URL and BLOB_STORE_URL are required.")
    blobs = open_blob_store(settings.blob_store_url)
    context = SimpleNamespace(
        blob_store=blobs,
        repository=open_repository(settings.database_url, blob_store=blobs, migrate_schema=False),
    )
    while not stop.is_set():
        try:
            found = _work_one(context, stop)
        except Exception as exc:
            logger.error("Codex worker could not process the queue (%s)", type(exc).__name__)
            if args.once:
                return 1
            found = False
        if args.once:
            break
        if not found:
            stop.wait(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
