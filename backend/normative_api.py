"""Authenticated routes for the position-scoped electrical norms pilot."""
import re
from urllib.parse import unquote
from uuid import UUID

from fastapi import Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from workspace_models import (NormativeSearchRequest, NormativeSelectionRequest, NormativePositionCheckRequest,
                              NormativeReviewDTO, NormativeStateDTO, NormativeFacts,
                              NormativeWorkbenchSearchDTO, NormativeVORCheckDTO, NormativeManagerDTO)


def register_normative_routes(router, guarded, available):
    from api import ApiError
    import normative_service as service
    base = "/api/v1/audits/{run_id}/positions/{position_index}/normative"

    def actor(request):
        return unquote(request.headers.get("x-workspace-user", "api"))[:256]

    def call(operation):
        try:
            return operation()
        except KeyError as exc:
            raise ApiError(404, "normative_not_found", "Позиция или сохранённый нормативный поиск не найдены.") from exc
        except (FileNotFoundError, OSError) as exc:
            raise ApiError(503, "normative_unavailable", "Нормативная база или закреплённый выпуск недоступны. Проверьте подключение локальной базы.") from exc
        except ValueError as exc:
            raise ApiError(409, "normative_conflict", str(exc)) from exc

    @router.post("/api/v1/normative/search", response_model=NormativeWorkbenchSearchDTO)
    def workbench_search(payload: NormativeSearchRequest, profile: str = Query("electrical", pattern="^(electrical|roads)$"), context=Depends(guarded)):
        if payload.mode != "lexical":
            raise ApiError(400, "lexical_search_only", "В общем поиске доступен локальный поиск по корпусу.")
        import normative_service as service
        kb = call(lambda: service.corpus(profile=profile))
        facts = payload.facts.model_dump()
        items = call(lambda: kb.search(payload.query, kind=payload.kind, facts=facts, limit=12))
        candidates = [service.candidate(kb, item, facts) for item in items]
        for item in candidates:
            item["resources"] = item["resources"][:30]
        return dict(profile=profile, identity=kb.identity, query=payload.query, candidates=candidates)

    @router.post("/api/v1/normative/vor-check", response_model=NormativeVORCheckDTO)
    async def workbench_vor_check(
        file: UploadFile = File(...),
        profile: str = Form("electrical", pattern="^(electrical|roads)$"),
        context=Depends(guarded),
    ):
        from api import _check_upload_limits, _check_upload_mime, _check_vor_magic, _safe_filename
        import io
        from normative_corpus import decimal
        from normative_workbench import code_from_position, title_similarity
        from vor_parse import load_vor_positions
        import normative_service as service

        data = await file.read(context.settings.max_upload_bytes + 1)
        filename = _safe_filename(file.filename or "")
        _check_upload_limits(context, bytes(data))
        _check_upload_mime(file, "vor")
        if not filename.lower().endswith(".xlsx"):
            raise ApiError(400, "xlsx_required", "Сопоставление с ГЭСН доступно для XLSX.")
        if not data:
            raise ApiError(400, "empty_upload", "Загруженный файл пуст.")
        _check_vor_magic(bytes(data), filename)
        try:
            positions, _meta = load_vor_positions(io.BytesIO(bytes(data)), filename)
        except Exception as exc:
            raise ApiError(400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}") from exc
        if not positions:
            raise ApiError(400, "vor_without_positions", "В файле не найдены строки с работами.")

        kb = call(lambda: service.corpus(profile=profile))
        output = []
        limit = 200
        for position in positions[:limit]:
            source_code = code_from_position(position.work_name, position.reference)
            volume = position.volume if decimal(position.volume) is not None else ""
            facts = NormativeFacts(unit=position.unit, quantity=volume).model_dump()
            exact_items = kb.search(source_code, kind="GESN", facts=facts, limit=3) if source_code else []
            name_items = kb.search(position.work_name, kind="GESN", facts=facts, limit=6)
            raw_items = list({item["id"]: item for item in reversed(exact_items + name_items)}.values())
            exact = next((item for item in raw_items if source_code and item.get("code") == source_code), None)
            ordered = ([exact] if exact else []) + [item for item in raw_items if item is not exact]
            candidates = [service.candidate(kb, item, facts) for item in ordered[:3]]
            for candidate in candidates:
                candidate["resources"] = candidate["resources"][:30]
                candidate["composition"] = candidate["composition"][:30]
                candidate["rules"] = candidate["rules"][:10]
            top = candidates[0] if candidates else None
            similarity = title_similarity(position.work_name, top["title"]) if top else None
            unit_check = next((item for item in (top or {}).get("checks", []) if item.get("label") == "Единица ВОР"), None)
            unit_match = unit_check.get("state") == "match" if unit_check else None
            code_match = bool(exact) if source_code else None
            if not candidates:
                status = "not_found"
            elif exact and unit_match is False:
                status = "unit_mismatch"
            elif exact and similarity is not None and similarity < 35:
                status = "name_mismatch"
            elif exact:
                status = "matched"
            else:
                status = "review"
            output.append(dict(index=position.index, number=position.number, work_name=position.work_name,
                volume=position.volume, unit=position.unit, source_code=source_code,
                code_match=code_match, unit_match=unit_match, name_similarity=similarity,
                status=status, candidates=candidates))

        return dict(filename=filename, profile=profile, total_positions=len(positions),
                    returned_positions=len(output), truncated=len(positions) > limit, rows=output)

    @router.get("/api/v1/normative/manager", response_model=NormativeManagerDTO)
    def normative_manager(context=Depends(guarded)):
        from normative_workbench import manager_profile
        import normative_service as service
        from normative_catalog import collections

        profiles = []
        imported_hashes = set()
        for profile in ("electrical", "roads"):
            try:
                kb = service.corpus(profile=profile)
                with kb.connect() as db:
                    for row in db.execute("SELECT blob_path FROM sources WHERE blob_path IS NOT NULL"):
                        blob_path = (kb.root / row["blob_path"]).resolve()
                        if blob_path.is_relative_to(kb.root) and re.fullmatch(
                            r"[a-f0-9]{64}", blob_path.stem
                        ):
                            imported_hashes.add(blob_path.stem)
                profiles.append(manager_profile(profile))
            except (OSError, ValueError, KeyError) as exc:
                raise ApiError(503, "normative_unavailable", "Один из нормативных корпусов недоступен.") from exc
        return dict(profiles=profiles, collections=collections(imported_hashes))

    @router.get("/api/v1/normative/catalog/pdf")
    def catalog_pdf(path: str = Query(..., min_length=1, max_length=1024), context=Depends(guarded)):
        from normative_catalog import pdf_path

        try:
            source = pdf_path(path)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ApiError(404, "normative_catalog_pdf_not_found", "PDF-файл каталога не найден.") from exc
        return FileResponse(
            source,
            media_type="application/pdf",
            filename=source.name,
            content_disposition_type="inline",
        )

    @router.get("/api/v1/normative/{profile}/sources/{source_id}")
    def workbench_source(profile: str, source_id: str, context=Depends(guarded)):
        import normative_service as service
        if profile not in {"electrical", "roads"}:
            raise ApiError(404, "normative_not_found", "Нормативный корпус не найден.")
        kb = call(lambda: service.corpus(profile=profile))
        path, designation = call(lambda: service.source_path({"payload": {"kb": kb.identity}}, source_id))
        return FileResponse(path, media_type="application/pdf", filename=designation + ".pdf", content_disposition_type="inline")

    @router.get(base, response_model=NormativeStateDTO)
    def state(run_id: str, position_index: int, context=Depends(guarded)):
        record = available(context, run_id)
        return call(lambda: service.state(context, record, position_index))

    @router.post(base + "/search", response_model=NormativeReviewDTO)
    def search(run_id: str, position_index: int, payload: NormativeSearchRequest, request: Request, context=Depends(guarded)):
        record = available(context, run_id)
        return call(lambda: service.search(context, record, position_index, payload, actor(request)))

    @router.post(base + "/selection", response_model=NormativeReviewDTO)
    def select(run_id: str, position_index: int, payload: NormativeSelectionRequest, request: Request, context=Depends(guarded)):
        record = available(context, run_id)
        return call(lambda: service.select(context, record, position_index, payload, actor(request)))

    @router.post(base + "/check", response_model=NormativeReviewDTO)
    def check(run_id: str, position_index: int, payload: NormativePositionCheckRequest, request: Request, context=Depends(guarded)):
        from normative_position_check import check as review_position
        record = available(context, run_id)
        return call(lambda: review_position(context, record, position_index, payload, actor(request)))

    @router.get(base + "/library/{source_id}")
    def library_pdf(run_id: str, position_index: int, source_id: str, context=Depends(guarded)):
        record = available(context, run_id)
        call(lambda: service.project_context(context, record, position_index))
        kb = call(lambda: service.corpus(profile=service.profile_for(record)))
        path, designation = call(lambda: service.source_path({"payload": {"kb": kb.identity}}, source_id))
        return FileResponse(path, media_type="application/pdf", filename=designation+".pdf", content_disposition_type="inline")

    @router.post(base + "/{review_id}/validate", response_model=NormativeReviewDTO)
    def validate_saved(run_id: str, position_index: int, review_id: str, request: Request, context=Depends(guarded)):
        from normative_position_check import validate_saved as validate
        record = available(context, run_id)
        return call(lambda: validate(context, record, position_index, review_id, actor(request)))

    @router.get(base + "/{review_id}/check-images/{source_id}")
    def check_image(run_id: str, position_index: int, review_id: str, source_id: str, context=Depends(guarded)):
        record = available(context, run_id)
        row = call(lambda: context.repository.normative_review(record["id"], position_index, UUID(review_id)))
        source = next((s for s in (row["payload"].get("position_check") or {}).get("sources", []) if s["id"] == source_id), None)
        if not source or not source.get("image_key"):
            raise ApiError(404, "image_not_found", "Изображение OCR не найдено")
        return Response(call(lambda: context.blob_store.get(source["image_key"])), media_type="image/png")

    @router.get(base + "/{review_id}/check-ocr/{source_id}")
    def check_ocr(run_id: str, position_index: int, review_id: str, source_id: str, context=Depends(guarded)):
        record = available(context, run_id)
        row = call(lambda: context.repository.normative_review(record["id"], position_index, UUID(review_id)))
        source = next((s for s in (row["payload"].get("position_check") or {}).get("sources", []) if s["id"] == source_id), None)
        if not source or not source.get("words_key"):
            raise ApiError(404, "ocr_not_found", "Данные OCR не найдены")
        return Response(call(lambda: context.blob_store.get(source["words_key"])), media_type="application/json")

    @router.get(base + "/{review_id}/sources/{source_id}")
    def source(run_id: str, position_index: int, review_id: str, source_id: str, context=Depends(guarded)):
        record = available(context, run_id)
        review = call(lambda: context.repository.normative_review(record["id"], position_index, UUID(review_id)))
        path, designation = call(lambda: service.source_path(review, source_id))
        return FileResponse(path, media_type="application/pdf", filename=designation + ".pdf", content_disposition_type="inline")
