"""Read persisted audit inputs/results. No OCR or model requests on reads."""

from __future__ import annotations

import io
from collections import Counter
from uuid import UUID

import fitz
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from audit_models import VorPosition
from audit_reports import build_user_report
from vor_parse import load_vor_positions
from workspace_models import ResultDTO, RunDTO


def run_progress(context, record) -> RunDTO:
    if "job_counts" not in record:
        record = context.repository.run_progress_records(run_id=record["id"])[0]
    counts = Counter(record["job_counts"])
    job_total = sum(counts.values())
    total = (
        record["input_total"]
        if record["input_total"] is not None
        else job_total or None
    )
    status = record["status"]
    finished = counts["done"] + counts["failed"]
    stage = (
        status
        if status in {"done", "failed"}
        else (
            "preparing"
            if record["prepare_seconds"] is None
            and (not job_total or record["pdf_jobs"])
            else "finalizing"
            if total is not None and finished == total
            else "auditing"
            if job_total
            else "preparing"
        )
    )
    return RunDTO(
        run_id=str(record["id"]),
        project_id=str(record["project_id"]),
        vor_version_id=str(record["vor_version_id"]),
        vor_filename=record["vor_filename"],
        status=status,
        stage=stage,
        total=total,
        done=counts["done"] if job_total else record["result_count"],
        failed=counts["failed"],
        running=counts["running"],
        queued=counts["queued"],
        created_at=record.get("created_at"),
        finished_at=record.get("finished_at"),
        preparation_done=(record.get("pdf_preparation") or {}).get("done", 0),
        preparation_total=(record.get("pdf_preparation") or {}).get("total"),
    )


def inputs_for_run(context, record) -> list[VorPosition]:
    snapshot = record["config"].get("position_inputs")
    if isinstance(snapshot, list):
        return [VorPosition.model_validate(item) for item in snapshot]
    version = context.repository.load_version(record["vor_version_id"])
    positions, _ = load_vor_positions(
        context.blob_store.get(version.blob_key), version.filename
    )
    return positions


def present_results(context, record) -> list[ResultDTO]:
    positions = {p.index: p for p in inputs_for_run(context, record)}
    jobs = {
        j.position_index: j for j in context.repository.list_position_jobs(record["id"])
    }
    result = []
    for item in record["indexed_results"]:
        index, stored = item["position_index"], item["result"]
        position = positions.get(index)
        if position is None:
            raise ValueError("Исходная позиция результата не найдена")
        job = jobs.get(index)
        payload = job.payload if job else {}
        task = position.legacy_task()
        task.update(
            {
                "bind_error": payload.get("bind_error"),
                "target_indices": payload.get(
                    "target_indices", [p - 1 for p in stored.get("vor_pages", [])]
                ),
                "loc_desc": payload.get("loc_desc", ""),
            }
        )
        tracking = {
            **stored,
            "found_page": stored.get("found_page")
            or next(iter(stored.get("found_pages") or []), None),
        }
        report = build_user_report(
            pd.DataFrame([{0: position.number, "Статус аудита": stored["status"]}]),
            [tracking],
            [task],
        ).iloc[0]
        report_status = report["Статус аудита"]
        queue = {
            "Обнаружено расхождение": "fix",
            "Требуется ручная проверка": "review",
            "Проверка не выполнена": "missing",
            "ОК": "ok",
        }[report_status]
        result.append(
            ResultDTO(
                id=f"{record['id']}:{index}",
                position_index=index,
                number=position.number,
                name=position.work_name,
                unit=position.unit,
                declared_quantity=position.volume,
                found_quantity=str(stored["found_volume"])
                if stored.get("found_volume") not in {None, "", "-"}
                else None,
                found_unit=stored.get("found_unit")
                if stored.get("found_unit") not in {None, "", "-"}
                else None,
                formula=position.formula,
                sheet=position.sheet,
                source_row=position.source_row,
                cells=position.source_cells,
                audit_status=stored["status"],
                report_status=report_status,
                queue=queue,
                problem=report["Что не так"],
                action=report["Что сделать"],
                work_found=bool(stored.get("work_found")),
                volume_confirmed=bool(stored.get("volume_confirmed")),
                unit_confirmed=bool(stored.get("unit_confirmed")),
                comment_confirmed=bool(stored.get("comment_confirmed")),
                details=stored.get("discrepancy_details") or "",
            )
        )
    return result


def sheet_window(data: bytes, position: VorPosition):
    if not position.sheet or position.source_row is None:
        return None
    book = load_workbook(io.BytesIO(data), data_only=False, keep_links=False)
    try:
        sheet = book[position.sheet]
        low, high = (
            max(1, position.source_row - 3),
            min(sheet.max_row, position.source_row + 4),
        )
        columns = list(range(1, min(20, sheet.max_column) + 1))
        merged = list(sheet.merged_cells.ranges)
        rows = []
        for number in range(low, high + 1):
            cells = []
            for col in columns:
                cell = sheet.cell(number, col)
                span = next(
                    (
                        m
                        for m in merged
                        if m.min_row <= number <= m.max_row
                        and m.min_col <= col <= m.max_col
                    ),
                    None,
                )
                if span and (col != span.min_col or number != span.min_row):
                    continue
                cells.append(
                    {
                        "coordinate": cell.coordinate,
                        "value": str(cell.value) if cell.value is not None else "",
                        "column_span": span.max_col - span.min_col + 1 if span else 1,
                        "row_span": span.max_row - span.min_row + 1 if span else 1,
                        "highlight": cell.coordinate in position.source_cells.values()
                        and number == position.source_row,
                    }
                )
            rows.append(
                {
                    "number": number,
                    "selected": number == position.source_row,
                    "cells": cells,
                }
            )
        return {
            "sheet": sheet.title,
            "columns": [get_column_letter(c) for c in columns],
            "rows": rows,
        }
    finally:
        book.close()


def source_evidence(context, record, index, position):
    stored = next(
        x["result"] for x in record["indexed_results"] if x["position_index"] == index
    )
    versions = record["config"].get("pdf_versions") or {}
    result = []
    for evidence in stored.get("evidence") or []:
        name = evidence.get("document_id") or stored.get("pdf_name") or ""
        version_id = versions.get(name) or (
            stored.get("pdf_version_id") if name == stored.get("pdf_name") else None
        )
        if not version_id or not isinstance(evidence.get("page"), int):
            continue
        version = context.repository.load_version(UUID(version_id))
        if context.repository.version_project_id(version.id) != record["project_id"]:
            continue
        page_number, quote = evidence["page"], str(evidence.get("quote") or "")
        with fitz.open(
            stream=context.blob_store.get(version.blob_key), filetype="pdf"
        ) as pdf:
            if not 1 <= page_number <= len(pdf):
                continue
            page = pdf[page_number - 1]
            normalized = " ".join(page.get_text().casefold().split())
            phrase = " ".join(quote.casefold().split())
            boxes = (
                page.search_for(quote)
                if phrase and normalized.count(phrase) == 1
                else []
            )
            # Repeated or long/multiline ambiguous hits do not become an accepted highlight.
            union = fitz.Rect(boxes[0]) if boxes else None
            for box in boxes[1:]:
                union |= box
            if union and union.height > page.rect.height * 0.2:
                boxes = []
            region = {
                "width": page.rect.width,
                "height": page.rect.height,
                "highlights": [
                    {
                        "bbox": list(b * page.rotation_matrix),
                        "kind": "quote",
                        "text": quote,
                    }
                    for b in boxes
                ],
                "reason": "Цитата найдена в текстовом слое"
                if boxes
                else "Координаты цитаты не установлены; проверьте страницу вручную",
            }
        result.append(
            {
                "version_id": version_id,
                "filename": version.filename,
                "page": page_number,
                "quote": quote,
                "kind": evidence.get("kind", "work"),
                "region": region,
                "confirmed": True,
            }
        )
    if not result:
        # Navigation to the VOR reference never constitutes proof.
        name = stored.get("pdf_name") or position.document_id
        version_id = versions.get(name)
        pages = stored.get("vor_pages") or position.primary_pages
        if version_id and pages:
            version = context.repository.load_version(UUID(version_id))
            if (
                context.repository.version_project_id(version.id)
                == record["project_id"]
            ):
                result.append(
                    {
                        "version_id": version_id,
                        "filename": version.filename,
                        "page": pages[0],
                        "quote": "Ссылка из ВОР; подтверждение не установлено",
                        "kind": "reference",
                        "region": None,
                        "confirmed": False,
                    }
                )
    return result
