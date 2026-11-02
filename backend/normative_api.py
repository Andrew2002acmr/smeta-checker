"""Authenticated routes for the position-scoped electrical norms pilot."""
from urllib.parse import unquote
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import FileResponse, Response

from workspace_models import (NormativeSearchRequest, NormativeSelectionRequest, NormativePositionCheckRequest,
                              NormativeReviewDTO, NormativeStateDTO)


def register_normative_routes(router, guarded, available):
    from api import ApiError
    import normative_service as service
    base = "/api/v1/audits/{run_id}/positions/{position_index}/normative"

    def actor(request):
        return unquote(request.headers.get("x-workspace-user", "api"))[:256]

    def call(operation):
        try:
            from normative_public_sources import public_sources
            return public_sources(operation())
        except KeyError as exc:
            raise ApiError(404, "normative_not_found", "Позиция или сохранённый нормативный поиск не найдены.") from exc
        except (FileNotFoundError, OSError) as exc:
            raise ApiError(503, "normative_unavailable", "Нормативная база или закреплённый выпуск недоступны. Проверьте подключение локальной базы.") from exc
        except ValueError as exc:
            raise ApiError(409, "normative_conflict", str(exc)) from exc

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
