"""Run-scoped HTTP workspace over the existing repository and audit reports."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote

import fitz
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from workspace_models import (
    FormatDTO,
    ProjectCreate,
    ProjectDTO,
    RowsDTO,
    RunDTO,
    StructureDTO,
    SummaryDTO,
    VersionDTO,
    WorkspaceDTO,
)
from workspace_service import (
    inputs_for_run,
    present_results,
    run_progress,
    sheet_window,
    source_evidence,
)


def workspace_router(get_context) -> APIRouter:
    from api import (
        ApiError,
        _load_run_record,
        _parse_uuid,
        _require_api_token,
        _require_configured,
    )

    def guarded(request: Request, context=Depends(get_context)):
        _require_api_token(context, request)
        _require_configured(context)
        return context

    router = APIRouter()

    def project(context, value):
        try:
            return context.repository.load_project(
                _parse_uuid(value, field="project_id")
            )
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc

    def version(context, value):
        try:
            return context.repository.load_version(
                _parse_uuid(value, field="version_id")
            )
        except KeyError as exc:
            raise ApiError(
                404, "version_not_found", "Версия документа не найдена"
            ) from exc

    def terminal(context, run_id):
        record = _load_run_record(context, _parse_uuid(run_id, field="run_id"))
        if record["status"] not in {"done", "failed"}:
            raise ApiError(409, "audit_not_finished", "Аудит ещё выполняется")
        return record

    @router.get("/api/v1/projects", response_model=list[ProjectDTO])
    def projects(context=Depends(guarded)):
        return context.repository.list_projects()

    @router.post("/api/v1/projects", response_model=ProjectDTO, status_code=201)
    def create_project(payload: ProjectCreate, context=Depends(guarded)):
        name = payload.name.strip()
        if not name:
            raise ApiError(400, "project_required", "Укажите название проекта")
        value = context.repository.get_or_create_project(name)
        return {"id": str(value.id), "name": value.name}

    @router.get(
        "/api/v1/projects/{project_id}/documents", response_model=list[VersionDTO]
    )
    def documents(project_id: str, context=Depends(guarded)):
        return context.repository.list_versions(project(context, project_id).id)

    @router.get("/api/v1/projects/{project_id}/audits", response_model=list[RunDTO])
    def history(
        project_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        context=Depends(guarded),
    ):
        runs = context.repository.run_progress_records(
            project_id=project(context, project_id).id,
            offset=offset,
            limit=limit,
        )
        return [run_progress(context, record) for record in runs]

    @router.get("/api/v1/audits/{run_id}/progress", response_model=RunDTO)
    def progress(run_id: str, context=Depends(guarded)):
        records = context.repository.run_progress_records(
            run_id=_parse_uuid(run_id, field="run_id")
        )
        if not records:
            raise ApiError(404, "run_not_found", "Запуск аудита не найден")
        return run_progress(context, records[0])

    @router.get("/api/v1/audits/{run_id}/summary", response_model=SummaryDTO)
    def summary(run_id: str, context=Depends(guarded)):
        record = terminal(context, run_id)
        owner = project(context, str(record["project_id"]))
        ids = {
            str(record["vor_version_id"]),
            *record["config"].get("pdf_versions", {}).values(),
        }
        return {
            "run": run_progress(context, record),
            "project": {"id": str(owner.id), "name": owner.name},
            "queues": dict(
                Counter(row.queue for row in present_results(context, record))
            ),
            "documents": [
                v for v in context.repository.list_versions(owner.id) if v["id"] in ids
            ],
        }

    @router.get("/api/v1/audits/{run_id}/rows", response_model=RowsDTO)
    def rows(
        run_id: str,
        focus: str = Query("all", pattern="^(all|fix|review|missing|ok)$"),
        q: str = Query("", max_length=500),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        context=Depends(guarded),
    ):
        records = present_results(context, terminal(context, run_id))
        query = q.strip().casefold()
        records = [
            r
            for r in records
            if (focus == "all" or r.queue == focus)
            and (
                not query
                or query
                in " ".join([r.number, r.name, r.sheet, *r.cells.values()]).casefold()
            )
        ]
        return {
            "run_id": run_id,
            "total": len(records),
            "offset": offset,
            "limit": limit,
            "rows": records[offset : offset + limit],
        }

    @router.get(
        "/api/v1/audits/{run_id}/positions/{position_index}/workspace",
        response_model=WorkspaceDTO,
    )
    def workspace(run_id: str, position_index: int, context=Depends(guarded)):
        record = terminal(context, run_id)
        row = next(
            (
                r
                for r in present_results(context, record)
                if r.position_index == position_index
            ),
            None,
        )
        if row is None:
            raise ApiError(404, "position_not_found", "Позиция не найдена")
        position = next(
            p for p in inputs_for_run(context, record) if p.index == position_index
        )
        vor = version(context, str(record["vor_version_id"]))
        return {
            "row": row,
            "position": position,
            "vor_version_id": str(vor.id),
            "excel": sheet_window(context.blob_store.get(vor.blob_key), position)
            if vor.filename.lower().endswith(".xlsx")
            else None,
            "evidence": source_evidence(context, record, position_index, position),
        }

    @router.get("/api/v1/documents/{version_id}/content")
    def content(
        version_id: str,
        request: Request,
        download: bool = False,
        context=Depends(guarded),
    ):
        ref = version(context, version_id)
        try:
            data = context.blob_store.get(ref.blob_key)
        except FileNotFoundError as exc:
            raise ApiError(404, "blob_missing", "Исходный файл недоступен") from exc
        mime = (
            "application/pdf"
            if ref.kind == "pdf"
            else (
                "text/csv; charset=utf-8"
                if ref.filename.lower().endswith(".csv")
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        )
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": f'"{ref.sha256}"',
            "Cache-Control": "private, no-cache",
            "Content-Disposition": f"{'attachment' if download else 'inline'}; filename*=UTF-8''{quote(ref.filename)}",
        }
        if (
            request.headers.get("if-none-match") == headers["ETag"]
            and "range" not in request.headers
        ):
            return Response(status_code=304, headers=headers)
        requested = request.headers.get("range")
        if (
            requested
            and request.headers.get("if-range", headers["ETag"]) == headers["ETag"]
        ):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                return Response(
                    status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{len(data)}"},
                )
            first, last = match.groups()
            start = int(first) if first else max(0, len(data) - int(last))
            end = min(int(last), len(data) - 1) if first and last else len(data) - 1
            if start >= len(data) or start > end:
                return Response(
                    status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{len(data)}"},
                )
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            return Response(
                data[start : end + 1], status_code=206, media_type=mime, headers=headers
            )
        return Response(data, media_type=mime, headers=headers)

    @router.get("/api/v1/documents/{version_id}/structure", response_model=StructureDTO)
    def structure(version_id: str, context=Depends(guarded)):
        from document_structure import build_structure, native_records

        ref = version(context, version_id)
        if ref.kind != "pdf":
            raise ApiError(400, "not_pdf_version", "Выберите PDF")
        with fitz.open(
            stream=context.blob_store.get(ref.blob_key), filetype="pdf"
        ) as doc:
            return build_structure(
                native_records(doc), doc.get_toc(), sha256=ref.sha256
            )

    @router.post(
        "/api/v1/documents/{version_id}/format-checks", response_model=FormatDTO
    )
    def formatting(
        version_id: str,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        import io
        from vor_rules import validate_vor_formatting

        ref = version(context, version_id)
        if ref.kind != "vor" or not ref.filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        try:
            issues = validate_vor_formatting(
                io.BytesIO(context.blob_store.get(ref.blob_key)), sheet_name=sheet
            )
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        return {"version_id": version_id, "issues": issues}

    return router
