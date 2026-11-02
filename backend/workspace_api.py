"""Run-scoped HTTP workspace over the existing repository and audit reports."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import Response

from workspace_models import (
    DocumentDTO,
    DocumentPatch,
    FormatDTO,
    ProjectCreate,
    ProjectDTO,
    ProjectUpdate,
    RowsDTO,
    RunDTO,
    StructureDTO,
    SummaryDTO,
    VersionDeletedDTO,
    VersionDTO,
    VersionMetadataPatch,
    WorkspaceDTO,
    HumanBasisDTO,
    ConfirmBasisRequest,
    SpecifyBasisRequest,
    BasisPreviewDTO,
    PdfBindingsDTO,
    PdfBindingsPatch,
    ReviewArtifactDTO,
    ReviewHistoryDTO,
    PreparedPageDTO,
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

    def binding_document(context, version_id):
        from vor_parse import load_vor_positions
        from pdf_binding import clean_pdf_name
        ref = version(context, version_id)
        if ref.kind != "vor":
            raise ApiError(400, "not_vor_version", "Привязки задаются для версии ВОР")
        owner_id = context.repository.version_project_id(ref.id)
        positions, _ = load_vor_positions(context.blob_store.get(ref.blob_key), ref.filename)
        references = sorted({clean_pdf_name(r) for p in positions for r in p.explicit_refs if clean_pdf_name(r)})
        return ref, owner_id, references

    @router.get("/api/v1/documents/{version_id}/pdf-bindings", response_model=PdfBindingsDTO)
    def pdf_bindings(version_id: str, context=Depends(guarded)):
        ref, _, references = binding_document(context, version_id)
        return dict(references=references, bindings=context.repository.load_pdf_bindings(ref.id))

    @router.put("/api/v1/documents/{version_id}/pdf-bindings", response_model=PdfBindingsDTO)
    def update_pdf_bindings(version_id: str, payload: PdfBindingsPatch, context=Depends(guarded)):
        from api import _check_active_versions, _check_project_versions
        from pdf_binding import clean_pdf_name
        ref, owner_id, references = binding_document(context, version_id)
        _check_active_versions(context, owner_id, [ref.id])
        bindings = {}
        for raw, target in payload.bindings.items():
            key = clean_pdf_name(raw)
            if key not in references or key in bindings:
                raise ApiError(400, "invalid_pdf_reference", "Ссылка отсутствует в этой версии ВОР или повторяется")
            pdf = version(context, target)
            if pdf.kind != "pdf":
                raise ApiError(400, "not_pdf_version", "Для ссылки нужно выбрать PDF")
            _check_project_versions(context, owner_id, [pdf.id])
            _check_active_versions(context, owner_id, [pdf.id])
            bindings[key] = str(pdf.id)
        context.repository.save_pdf_bindings(ref.id, bindings)
        return dict(references=references, bindings=bindings)

    def available(context, run_id):
        return _load_run_record(context, _parse_uuid(run_id, field="run_id"))

    from normative_api import register_normative_routes
    register_normative_routes(router, guarded, available)

    @router.get("/api/v1/projects", response_model=list[ProjectDTO])
    def projects(archived: bool = Query(False), context=Depends(guarded)):
        return context.repository.list_projects(archived=archived)

    @router.post("/api/v1/projects", response_model=ProjectDTO, status_code=201)
    def create_project(payload: ProjectCreate, context=Depends(guarded)):
        name = payload.name.strip()
        if not name:
            raise ApiError(400, "project_required", "Укажите название проекта")
        # Independent UUID per call: duplicate names never merge.
        value = context.repository.create_project(name)
        return {
            "id": str(value.id),
            "name": value.name,
            "archived": value.archived,
            "created_at": value.created_at,
        }

    @router.get("/api/v1/projects/{project_id}", response_model=ProjectDTO)
    def get_project(project_id: str, context=Depends(guarded)):
        return {
            "id": str(project(context, project_id).id),
            "name": project(context, project_id).name,
            "archived": project(context, project_id).archived,
            "created_at": project(context, project_id).created_at,
        }

    @router.patch("/api/v1/projects/{project_id}", response_model=ProjectDTO)
    def update_project(
        project_id: str, payload: ProjectUpdate, context=Depends(guarded)
    ):
        if payload.name is not None and not payload.name.strip():
            raise ApiError(400, "project_required", "Укажите название проекта")
        try:
            _, presented = context.repository.update_project(
                _parse_uuid(project_id, field="project_id"),
                name=(payload.name.strip() if payload.name is not None else None),
                archived=payload.archived,
            )
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc
        except ValueError as exc:
            raise ApiError(400, "project_invalid", str(exc)) from exc
        return presented

    @router.delete("/api/v1/projects/{project_id}", response_model=ProjectDTO)
    def delete_project(project_id: str, context=Depends(guarded)):
        try:
            row = project(context, project_id)
            context.repository.delete_project(row.id)
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc
        except LookupError as exc:
            raise ApiError(409, "project_in_use", str(exc)) from exc
        return {
            "id": str(row.id),
            "name": row.name,
            "archived": row.archived,
            "created_at": row.created_at,
        }

    @router.get(
        "/api/v1/projects/{project_id}/documents", response_model=list[VersionDTO]
    )
    def documents(project_id: str, context=Depends(guarded)):
        return context.repository.list_versions(project(context, project_id).id)

    def _enrich_last_run(context, document: dict) -> dict:
        last = dict(document.get("last_run") or {})
        if not last.get("run_id"):
            return document
        try:
            records = context.repository.run_progress_records(
                run_id=_parse_uuid(str(last["run_id"]), field="run_id")
            )
        except Exception:
            return document
        if not records:
            return document
        enriched = dict(document)
        enriched["last_run"] = run_progress(context, records[0])
        return enriched

    @router.get(
        "/api/v1/projects/{project_id}/catalog", response_model=list[DocumentDTO]
    )
    def catalog(
        project_id: str, archived: bool = Query(False), context=Depends(guarded)
    ):
        try:
            documents = context.repository.catalog(
                project(context, project_id).id, archived=archived
            )
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc
        return [_enrich_last_run(context, document) for document in documents]

    def _document_or_404(context, document_id: str) -> dict:
        try:
            return context.repository.load_document(
                _parse_uuid(document_id, field="document_id")
            )
        except KeyError as exc:
            raise ApiError(404, "document_not_found", "Документ не найден") from exc

    @router.get("/api/v1/documents/{document_id}/detail", response_model=DocumentDTO)
    def document_detail(document_id: str, context=Depends(guarded)):
        return _enrich_last_run(context, _document_or_404(context, document_id))

    @router.patch("/api/v1/documents/{document_id}", response_model=DocumentDTO)
    def update_document(
        document_id: str, payload: DocumentPatch, context=Depends(guarded)
    ):
        if payload.title is not None and not payload.title.strip():
            raise ApiError(400, "document_invalid", "Название не может быть пустым")
        try:
            current_id = (
                _parse_uuid(payload.current_version_id, field="current_version_id")
                if payload.current_version_id
                else ...
            )
            updated = context.repository.update_document(
                _parse_uuid(document_id, field="document_id"),
                title=(payload.title.strip() if payload.title is not None else None),
                archived=payload.archived,
                current_version_id=current_id,
            )
            return _enrich_last_run(context, updated)
        except KeyError as exc:
            raise ApiError(404, "document_not_found", "Документ не найден") from exc
        except ValueError as exc:
            raise ApiError(400, "document_invalid", str(exc)) from exc

    @router.delete("/api/v1/documents/{document_id}/detail", response_model=DocumentDTO)
    def delete_document(document_id: str, context=Depends(guarded)):
        try:
            row = _enrich_last_run(context, _document_or_404(context, document_id))
            from uuid import UUID as _UUID

            context.repository.delete_document(_UUID(str(row["id"])))
        except KeyError as exc:
            raise ApiError(404, "document_not_found", "Документ не найден") from exc
        except LookupError as exc:
            raise ApiError(409, "document_in_use", str(exc)) from exc
        return row

    @router.patch("/api/v1/documents/{version_id}/metadata", response_model=VersionDTO)
    def update_version_metadata(
        version_id: str, payload: VersionMetadataPatch, context=Depends(guarded)
    ):
        try:
            return context.repository.update_version_metadata(
                _parse_uuid(version_id, field="version_id"),
                dict(payload.fields or {}),
            )
        except KeyError as exc:
            raise ApiError(404, "version_not_found", "Версия не найдена") from exc
        except ValueError as exc:
            raise ApiError(400, "metadata_invalid", str(exc)) from exc

    @router.delete(
        "/api/v1/documents/{version_id}/version", response_model=VersionDeletedDTO
    )
    def delete_version(version_id: str, context=Depends(guarded)):
        try:
            context.repository.delete_version(
                _parse_uuid(version_id, field="version_id")
            )
        except KeyError as exc:
            raise ApiError(404, "version_not_found", "Версия не найдена") from exc
        except LookupError as exc:
            raise ApiError(409, "version_in_use", str(exc)) from exc
        return {"deleted": True}

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

    @router.post("/api/v1/audits/{run_id}/cancel", response_model=RunDTO)
    def cancel_audit(run_id: str, context=Depends(guarded)):
        parsed = _parse_uuid(run_id, field="run_id")
        record = context.repository.load_run(parsed)
        if str(record.get("status")) in {"done", "failed", "cancelled"}:
            raise ApiError(409, "audit_not_running", "Аудит уже завершён.")
        context.repository.finish_run(parsed, "cancelled")
        records = context.repository.run_progress_records(run_id=parsed)
        if not records:
            raise ApiError(404, "run_not_found", "Запуск аудита не найден.")
        return run_progress(context, records[0])

    @router.post("/api/v1/audits/{run_id}/resume", response_model=RunDTO)
    def resume_audit(run_id: str, context=Depends(guarded)):
        parsed = _parse_uuid(run_id, field="run_id")
        try:
            context.repository.resume_run(parsed, allow_running=False)
        except KeyError as exc:
            raise ApiError(404, "run_not_found", "Запуск аудита не найден.") from exc
        except ValueError as exc:
            message = (
                "Предыдущая задача ещё завершается. Повторите продолжение через минуту. "
                "Готовые результаты сохранены."
                if "lease" in str(exc)
                else "Продолжить можно только остановленную или сбойную проверку."
            )
            raise ApiError(409, "audit_resume_conflict", message) from exc
        records = context.repository.run_progress_records(run_id=parsed)
        return run_progress(context, records[0])

    @router.get("/api/v1/audits/{run_id}/summary", response_model=SummaryDTO)
    def summary(run_id: str, context=Depends(guarded)):
        record = available(context, run_id)
        ready = present_results(context, record)
        owner = project(context, str(record["project_id"]))
        ids = {
            str(record["vor_version_id"]),
            *record["config"].get("pdf_versions", {}).values(),
        }
        return {
            "run": run_progress(context, record),
            "ready_positions": [r.position_index for r in ready],
            "project": {"id": str(owner.id), "name": owner.name},
            "queues": dict(
                Counter(row.queue for row in ready)
            ),
            "documents": [
                v for v in context.repository.list_versions(owner.id) if v["id"] in ids
            ],
        }

    @router.get("/api/v1/audits/{run_id}/rows", response_model=RowsDTO)
    def rows(
        run_id: str,
        focus: str = Query("all", pattern="^(all|fix|review|missing|ok|pending)$"),
        q: str = Query("", max_length=500),
        offset: int = Query(0, ge=0),
        limit: int = Query(10, ge=1, le=200),
        context=Depends(guarded),
    ):
        records = present_results(context, available(context, run_id))
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
        record = available(context, run_id)
        row = next(
            (
                r
                for r in present_results(context, record)
                if r.position_index == position_index
            ),
            None,
        )
        if row is None:
            if not any(p.index == position_index for p in inputs_for_run(context, record)):
                raise ApiError(404, "position_not_found", "Позиция не найдена")
            raise ApiError(409, "position_not_ready", "Позиция ещё обрабатывается. Откройте готовую позицию.")
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

    def basis_context(context, run_id, position_index):
        from human_basis import locate_position
        record = available(context, run_id)
        try:
            position = locate_position(context, record, position_index)
        except KeyError as exc:
            raise ApiError(404, "position_not_found", "Позиция не найдена") from exc
        if not any(r.position_index == position_index for r in present_results(context, record)):
            raise ApiError(409, "position_not_ready", "Позиция ещё обрабатывается")
        return record, position

    @router.get("/api/v1/audits/{run_id}/positions/{position_index}/basis", response_model=list[HumanBasisDTO])
    def human_basis_list(run_id: str, position_index: int, context=Depends(guarded)):
        from human_basis import list_basis
        record, position = basis_context(context, run_id, position_index)
        return list_basis(context, record, position)

    @router.post("/api/v1/audits/{run_id}/positions/{position_index}/basis/confirm", response_model=list[HumanBasisDTO])
    def human_basis_confirm(run_id: str, position_index: int, payload: ConfirmBasisRequest,
                            request: Request, context=Depends(guarded)):
        from human_basis import save_basis
        from urllib.parse import unquote
        record, position = basis_context(context, run_id, position_index)
        try:
            return save_basis(context, record, position, payload,
                              unquote(request.headers.get("x-workspace-user", "api"))[:256],
                              evidence_index=payload.evidence_index)
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "basis_invalid", str(exc)) from exc

    @router.post("/api/v1/audits/{run_id}/positions/{position_index}/basis/preview", response_model=BasisPreviewDTO)
    def human_basis_preview(run_id: str, position_index: int, payload: SpecifyBasisRequest,
                            context=Depends(guarded)):
        from human_basis import preview_basis
        record, _ = basis_context(context, run_id, position_index)
        try:
            return preview_basis(context, record, payload)
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "selection_invalid", str(exc)) from exc

    @router.post("/api/v1/audits/{run_id}/positions/{position_index}/basis/recognize", response_model=BasisPreviewDTO)
    def human_basis_recognize(run_id: str, position_index: int, payload: SpecifyBasisRequest,
                            request: Request, context=Depends(guarded)):
        from human_basis import preview_basis
        record, _ = basis_context(context, run_id, position_index)
        try:
            result = preview_basis(context, record, payload, recognize=True)
            from urllib.parse import unquote
            selection = payload.model_dump(mode="json")
            selection["ocr_key"] = result["ocr_key"]
            context.repository.save_review_artifact(record["id"], position_index, "ocr", dict(selection=selection, preview=result), unquote(request.headers.get("x-workspace-user", "api"))[:256])
            return result
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "selection_invalid", str(exc)) from exc
        except RuntimeError as exc:
            raise ApiError(503, "ocr_unavailable", str(exc)) from exc

    @router.post("/api/v1/audits/{run_id}/positions/{position_index}/basis/drafts", response_model=ReviewArtifactDTO)
    def save_basis_draft(run_id: str, position_index: int, payload: SpecifyBasisRequest, request: Request, context=Depends(guarded)):
        import pymupdf
        from basis_fragments import crop_png
        from human_basis import preview_basis
        from urllib.parse import unquote
        record, _ = basis_context(context, run_id, position_index)
        try:
            preview = preview_basis(context, record, payload)
            ref = version(context, payload.version_id)
            with pymupdf.open(stream=context.blob_store.get(ref.blob_key), filetype="pdf") as pdf:
                for fragment in preview.get("fragments", []):
                    if not fragment.get("image_key"):
                        fragment["image_key"] = context.blob_store.put(crop_png(pdf[fragment["page"] - 1], fragment["rect"])).key
            return context.repository.save_review_artifact(record["id"], position_index, "draft", dict(selection=payload.model_dump(mode="json"), preview=preview), unquote(request.headers.get("x-workspace-user", "api"))[:256])
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "basis_invalid", str(exc)) from exc

    @router.get("/api/v1/audits/{run_id}/positions/{position_index}/extraction", response_model=PreparedPageDTO)
    def prepared_page(run_id: str, position_index: int, page: int = Query(1, ge=1), context=Depends(guarded)):
        from audit_snapshot import load_json
        record, _ = basis_context(context, run_id, position_index)
        try:
            job = context.repository.load_position_job(record["id"], position_index)
            ref = job.payload.get("snapshot") or {}
            if ref.get("snapshot_storage") != "pages-v1":
                raise ApiError(404, "snapshot_unavailable", "Для этой позиции нет постраничного снимка подготовки")
            manifest = load_json(context.blob_store, dict(key=ref["snapshot_key"], sha256=ref["snapshot_sha256"]))
            if page > len(manifest["pages"]):
                raise ApiError(404, "page_not_found", "Страницы нет в снимке")
            page_ref = manifest["pages"][page-1]
            data = load_json(context.blob_store, page_ref)
            origins = {locator.get("origin") for locator in (data.get("locators") or []) if isinstance(locator, dict)}
            text = str(data.get("text") or "")
            return dict(page=page, text=text[:20000], pending=bool(data.get("pending")), origin="ocr" if "ocr" in origins else "native" if "native" in origins else "unknown", quality=data.get("quality"), error=str(data["errors"]) if data.get("errors") else None, blob_key=page_ref["key"], sha256=page_ref["sha256"], truncated=len(text)>20000)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise ApiError(404, "snapshot_unavailable", "Сохранённый снимок этой страницы недоступен") from exc

    @router.get("/api/v1/audits/{run_id}/positions/{position_index}/history", response_model=ReviewHistoryDTO)
    def review_history(run_id: str, position_index: int, context=Depends(guarded)):
        from human_basis import position_key
        record, position = basis_context(context, run_id, position_index)
        versions = context.repository.result_versions(record["id"], position_index)
        pins = [_parse_uuid(v, field="version_id") for v in (record["config"].get("pdf_versions") or {}).values()]
        bases = context.repository.list_human_basis(record["project_id"], record["vor_version_id"], position_key(position), pins, include_revoked=True)
        return dict(results=[dict(id=v["id"], created_at=v["created_at"], status=str(v["result"].get("status", "")), found_volume=str(v["result"].get("found_volume", "")), found_unit=str(v["result"].get("found_unit", "")), details=str(v["result"].get("discrepancy_details", ""))) for v in versions], artifacts=context.repository.review_artifacts(record["id"], position_index), bases=[dict(**b, reused=b["run_id"] != run_id) for b in bases])

    @router.get("/api/v1/audits/{run_id}/positions/{position_index}/history/{kind}/{item_id}/images/{index}")
    def review_image(run_id: str, position_index: int, kind: str, item_id: str, index: int, context=Depends(guarded)):
        from human_basis import position_key
        record, position = basis_context(context, run_id, position_index)
        if kind == "artifact":
            item = next((a for a in context.repository.review_artifacts(record["id"], position_index) if a["id"] == item_id), None)
            fragments = (item or {}).get("preview", {}).get("fragments", [])
        elif kind == "basis":
            pins = [_parse_uuid(v, field="version_id") for v in record["config"].get("pdf_versions", {}).values()]
            item = next((a for a in context.repository.list_human_basis(record["project_id"], record["vor_version_id"], position_key(position), pins, include_revoked=True) if a["id"] == item_id), None)
            fragments = (item or {}).get("fragments", [])
        else:
            fragments = []
        if not 0 <= index < len(fragments) or not fragments[index].get("image_key"):
            raise ApiError(404, "image_not_found", "Изображение фрагмента не найдено")
        return Response(context.blob_store.get(fragments[index]["image_key"]), media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @router.post("/api/v1/audits/{run_id}/positions/{position_index}/basis", response_model=list[HumanBasisDTO])
    def human_basis_specify(run_id: str, position_index: int, payload: SpecifyBasisRequest,
                            request: Request, context=Depends(guarded)):
        from human_basis import save_basis
        from urllib.parse import unquote
        record, position = basis_context(context, run_id, position_index)
        try:
            return save_basis(context, record, position, payload,
                              unquote(request.headers.get("x-workspace-user", "api"))[:256])
        except (ValueError, KeyError) as exc:
            raise ApiError(400, "basis_invalid", str(exc)) from exc

    @router.delete("/api/v1/audits/{run_id}/positions/{position_index}/basis/{basis_id}", response_model=list[HumanBasisDTO])
    def human_basis_revoke(run_id: str, position_index: int, basis_id: str,
                           request: Request, context=Depends(guarded)):
        from human_basis import list_basis, position_key
        from urllib.parse import unquote
        record, position = basis_context(context, run_id, position_index)
        try:
            context.repository.revoke_human_basis(_parse_uuid(basis_id, field="basis_id"),
                record["project_id"], record["vor_version_id"], position_key(position),
                unquote(request.headers.get("x-workspace-user", "api"))[:256])
        except KeyError as exc:
            raise ApiError(404, "basis_not_found", "Основание не найдено") from exc
        return list_basis(context, record, position)

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
        import pymupdf

        from document_structure import build_structure, native_records

        ref = version(context, version_id)
        if ref.kind != "pdf":
            raise ApiError(400, "not_pdf_version", "Выберите PDF")
        with pymupdf.open(
            stream=context.blob_store.get(ref.blob_key), filetype="pdf"
        ) as doc:
            return build_structure(
                native_records(doc), doc.get_toc(), sha256=ref.sha256
            )

    @router.get("/api/v1/documents/{version_id}/format-report")
    def format_report(
        version_id: str,
        request: Request,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        import io
        from vor_rules import (
            get_highlighted_workbook,
            validate_vor_formatting,
            vor_sheet_names,
        )

        ref = version(context, version_id)
        if ref.kind != "vor" or not ref.filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        blob = bytes(context.blob_store.get(ref.blob_key))
        sheets = vor_sheet_names(io.BytesIO(blob))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(io.BytesIO(blob), sheet_name=active or None)
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        painted = get_highlighted_workbook(
            io.BytesIO(blob), rows, sheet_name=active or None
        )
        stem = (
            ref.filename[:-5]
            if ref.filename.lower().endswith(".xlsx")
            else ref.filename
        )
        return Response(
            content=painted or blob,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "ETag": f'"{ref.sha256}:format"',
                "Cache-Control": "private, no-cache",
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(stem + ' — оформление.xlsx')}"
                ),
            },
        )

    @router.get("/api/v1/documents/{version_id}/format-word")
    def format_word(
        version_id: str,
        request: Request,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        import io
        from vor_rules import (
            generate_rules_word_report,
            validate_vor_formatting,
            vor_sheet_names,
        )

        ref = version(context, version_id)
        if ref.kind != "vor" or not ref.filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        blob = bytes(context.blob_store.get(ref.blob_key))
        sheets = vor_sheet_names(io.BytesIO(blob))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(io.BytesIO(blob), sheet_name=active or None)
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        report = generate_rules_word_report(ref.filename, rows, sheet=active)
        stem = (
            ref.filename[:-5]
            if ref.filename.lower().endswith(".xlsx")
            else ref.filename
        )
        return Response(
            content=report,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "ETag": f'"{ref.sha256}:format-word"',
                "Cache-Control": "private, no-cache",
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(stem + ' — оформление.docx')}"
                ),
            },
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
        from vor_rules import (
            summarize_format_rows,
            validate_vor_formatting,
            vor_sheet_names,
        )

        ref = version(context, version_id)
        if ref.kind != "vor" or not ref.filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        blob = context.blob_store.get(ref.blob_key)
        sheets = vor_sheet_names(io.BytesIO(blob))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(io.BytesIO(blob), sheet_name=active or None)
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        issues, errors = summarize_format_rows(rows)
        from datetime import datetime, timezone

        context.repository.record_format_status(
            _parse_uuid(version_id, field="version_id"),
            {
                "state": "passed" if not issues else "failed",
                "sheet": active,
                "error_count": errors,
                "warning_count": len(issues) - errors,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return {
            "version_id": version_id,
            "sheet": active,
            "sheets": sheets,
            "error_count": errors,
            "warning_count": len(issues) - errors,
            "issues": issues,
        }

    @router.post("/api/v1/format-checks", response_model=FormatDTO)
    async def formatting_upload(
        request: Request,
        file: UploadFile,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        from api import (
            _check_upload_limits,
            _check_upload_mime,
            _safe_filename,
        )

        import io
        from vor_rules import (
            summarize_format_rows,
            validate_vor_formatting,
            vor_sheet_names,
        )

        data = await file.read(context.settings.max_upload_bytes + 1)
        filename = _safe_filename(file.filename or "")
        _check_upload_limits(context, bytes(data))
        _check_upload_mime(file, "vor")
        if not filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        if not data:
            raise ApiError(400, "empty_upload", "Загруженный файл пуст.")
        sheets = vor_sheet_names(io.BytesIO(bytes(data)))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(
                io.BytesIO(bytes(data)), sheet_name=active or None
            )
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        except Exception as exc:
            raise ApiError(
                400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}"
            ) from exc
        issues, errors = summarize_format_rows(rows)
        return {
            "version_id": "",
            "sheet": active,
            "sheets": sheets,
            "error_count": errors,
            "warning_count": len(issues) - errors,
            "issues": issues,
        }

    @router.post("/api/v1/format-report")
    async def format_report_upload(
        request: Request,
        file: UploadFile,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        from api import (
            _check_upload_limits,
            _check_upload_mime,
            _safe_filename,
        )

        import io
        from vor_rules import (
            get_highlighted_workbook,
            validate_vor_formatting,
            vor_sheet_names,
        )

        data = await file.read(context.settings.max_upload_bytes + 1)
        filename = _safe_filename(file.filename or "")
        _check_upload_limits(context, bytes(data))
        _check_upload_mime(file, "vor")
        if not filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        if not data:
            raise ApiError(400, "empty_upload", "Загруженный файл пуст.")
        blob = bytes(data)
        sheets = vor_sheet_names(io.BytesIO(blob))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(io.BytesIO(blob), sheet_name=active or None)
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        except Exception as exc:
            raise ApiError(
                400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}"
            ) from exc
        painted = get_highlighted_workbook(
            io.BytesIO(blob), rows, sheet_name=active or None
        )
        stem = filename[:-5] if filename.lower().endswith(".xlsx") else filename
        return Response(
            content=painted or blob,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Cache-Control": "private, no-cache",
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(stem + ' — оформление.xlsx')}"
                ),
            },
        )

    @router.post("/api/v1/format-word")
    async def format_word_upload(
        request: Request,
        file: UploadFile,
        sheet: str | None = Query(None, max_length=100),
        context=Depends(guarded),
    ):
        from api import (
            _check_upload_limits,
            _check_upload_mime,
            _safe_filename,
        )

        import io
        from vor_rules import (
            generate_rules_word_report,
            validate_vor_formatting,
            vor_sheet_names,
        )

        data = await file.read(context.settings.max_upload_bytes + 1)
        filename = _safe_filename(file.filename or "")
        _check_upload_limits(context, bytes(data))
        _check_upload_mime(file, "vor")
        if not filename.lower().endswith(".xlsx"):
            raise ApiError(
                400, "xlsx_required", "Оформление проверяется только для XLSX"
            )
        if not data:
            raise ApiError(400, "empty_upload", "Загруженный файл пуст.")
        blob = bytes(data)
        sheets = vor_sheet_names(io.BytesIO(blob))
        wanted = (sheet or "").strip()
        if wanted and wanted not in sheets:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден")
        active = wanted or (sheets[0] if sheets else "")
        try:
            rows = validate_vor_formatting(io.BytesIO(blob), sheet_name=active or None)
        except KeyError as exc:
            raise ApiError(404, "sheet_not_found", "Лист ВОР не найден") from exc
        except Exception as exc:
            raise ApiError(
                400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}"
            ) from exc
        report = generate_rules_word_report(filename, rows, sheet=active)
        stem = filename[:-5] if filename.lower().endswith(".xlsx") else filename
        return Response(
            content=report,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Cache-Control": "private, no-cache",
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(stem + ' — оформление.docx')}"
                ),
            },
        )

    return router
