"""Celery background jobs: CPU-bound PDF/OCR and network LLM in separate queues (issue 19).

Design: one ``runs`` row per audit, one ``position_jobs`` row per VOR position.
``prepare`` (queue ``pdf``) binds PDFs, builds PageTextIndex snapshots and creates
queued jobs; ``audit_position`` (queue ``llm``) runs one position through the same
``worker_check_row`` path as the in-process runner and persists its result;
``finalize`` (queue ``pdf``) merges stored results into Excel/Word with the shared
``build_run_artifacts``. ``recover`` requeues jobs whose worker died.

Celery is imported lazily: importing this module stays cheap without a broker,
and tests call the ``*_run`` functions directly with injected fakes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable
from uuid import UUID

from audit_core import technical_error
from audit_llm import audit_position_hybrid
from audit_models import AuditSettings
from audit_observability import RunMetrics, bind, estimate_cost, get_logger, reset
from audit_service import (
    build_run_artifacts,
    get_optimized_page_indices,
    traced_worker_check_row,
    worker_check_row,
)
from audit_tracing import TracingRun
from pdf_binding import resolve_position_pdfs
from pdf_text import PDFTextSource
from storage_blobs import BlobStore, open_blob_store
from storage_db import Repository, open_repository
from vor_parse import load_vor_frame_positions

logger = get_logger(__name__)

PDF_QUEUE = "pdf"
LLM_QUEUE = "llm"

# Single source for Celery routing and worker limits; workers are documented in README.
DEFAULT_TASK_ROUTES = {
    "audit_tasks.prepare_run": {"queue": PDF_QUEUE},
    "audit_tasks.audit_position": {"queue": LLM_QUEUE},
    "audit_tasks.finalize_run": {"queue": PDF_QUEUE},
    "audit_tasks.recover_run": {"queue": PDF_QUEUE},
}


@dataclass(frozen=True, slots=True)
class TaskContext:
    settings: AuditSettings
    repository: Repository
    blob_store: BlobStore
    client_factory: Callable[..., Any] | None = None
    auditor: Callable[..., Any] = audit_position_hybrid


def celery_app(settings: AuditSettings):  # type: ignore[no-untyped-def]
    """Build the Celery app; broker required, Redis recommended."""
    from celery import Celery

    if not settings.broker_url:
        raise RuntimeError("CELERY_BROKER_URL is required for background workers")
    app = Celery("smeta-checker", broker=settings.broker_url)
    app.conf.update(
        task_routes=dict(DEFAULT_TASK_ROUTES),
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_reject_on_worker_lost=True,
        task_time_limit=settings.task_time_limit,
        task_soft_time_limit=settings.task_soft_time_limit,
    )
    return app


def open_task_context(settings: AuditSettings | None = None) -> TaskContext:
    """Open repository + blobs from settings; worker entry point."""
    resolved = settings or AuditSettings.load()
    if not resolved.database_url or not resolved.blob_store_url:
        raise RuntimeError("DATABASE_URL and BLOB_STORE_URL are required")
    blobs = open_blob_store(resolved.blob_store_url)
    return TaskContext(
        settings=resolved,
        repository=open_repository(resolved.database_url, blob_store=blobs),
        blob_store=blobs,
    )


def _task_payload(
    position: Any, pdf_name: str, target_indices: list[int], page_count: int
) -> dict[str, Any]:
    return {
        "work_name": position.work_name,
        "volume": position.volume,
        "unit": position.unit,
        "reference": position.reference,
        "page_ref": position.page_ref,
        "comment": position.comment,
        "formula": position.formula,
        "pdf_name": pdf_name,
        "explicit_refs": list(position.explicit_refs),
        "target_indices": list(target_indices),
        "page_count": page_count,
    }


def _prepare_one_position(
    *,
    context: TaskContext,
    run_id: UUID,
    worker: str,
    vor_name: str,
    position: Any,
    meta: Any,
    pool_keys: list[str],
    pdf_versions: dict[str, Any],
) -> dict[str, Any]:
    """Bind one position and claim its job row; pure except the claim."""
    refs = list(position.explicit_refs)
    pdfs, _, _, bind_error = resolve_position_pdfs(
        refs, pool_keys, meta.get("basis_code", "")
    )
    pdf_name = pdfs[0] if not bind_error and pdfs else ""
    payload = _task_payload(position, pdf_name, [], 0)
    payload["bind_error"] = bind_error
    job = context.repository.create_position_job(
        run_id, int(position.index), queue=PDF_QUEUE, payload=payload
    )
    if job.status == "done":
        return {"position_index": int(position.index), "skipped": "done"}
    context.repository.claim_position_job(run_id, int(position.index), worker=worker)
    return {
        "position_index": int(position.index),
        "pdf_name": pdf_name,
        "bind_error": bind_error,
        "refs": refs,
        "vor_name": vor_name,
    }


def _build_task_dict(
    *,
    position: Any,
    pdf_name: str,
    bind_error: str | None,
    prep_error: str | None,
    text_index: Any,
    target_indices: list[int],
    loc_desc: str,
    primary_pages: list[int],
    position_trace: Any,
) -> dict[str, Any]:
    return {
        "idx": int(position.index),
        "position_trace": position_trace,
        "w_name": position.work_name,
        "w_vol": position.volume,
        "w_unit": position.unit,
        "w_ref": position.reference,
        "w_page": position.page_ref,
        "w_comment": position.comment,
        "w_formula": position.formula,
        "text_index": text_index,
        "target_pdf_name": pdf_name,
        "explicit_refs": list(position.explicit_refs),
        "bind_error": bind_error,
        "prep_error": prep_error,
        "target_indices": list(target_indices),
        "loc_desc": loc_desc,
        "primary_phys_pages": list(primary_pages),
    }


def prepare_run(
    context: TaskContext,
    run_id: UUID,
    *,
    project: str,
    vor_filename: str,
    vor_bytes: bytes,
    pdf_files: dict[str, bytes],
    pdf_versions: dict[str, Any],
    worker: str,
    dispatch: Callable[[str, dict[str, Any]], None] | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """CPU stage: bind PDFs, pre-warm one OCR allowance, queue LLM jobs.

    Runs on the ``pdf`` queue. Opens each PDF once, builds one PDFTextSource
    per document (closed on exit), and pre-warms ``for_position`` for every
    bound position so LLM workers hit the persistent cache. One position
    failure never aborts the rest: it is recorded as a technical payload and
    finished ``done`` so finalize still merges it.
    """
    import fitz

    started = time.monotonic()
    token = bind(
        audit_run_id=str(run_id), request_id=request_id or None,
        stage="prepare", worker=worker, queue=PDF_QUEUE,
    )
    pool: dict[str, Any] = {}
    trace_run = TracingRun(
        context.settings.langfuse_public_key,
        context.settings.langfuse_secret_key,
        context.settings.langfuse_base_url,
        context.settings.langfuse_environment,
        audit_run_id=str(run_id),
        request_id=request_id,
        redact_pdf=context.settings.telemetry_redact_pdf,
        mask_prompts=context.settings.telemetry_mask_prompts,
    )
    sources: dict[str, PDFTextSource] = {}
    queued: list[int] = []
    failed: list[int] = []
    try:
        for name, data in pdf_files.items():
            pool[name] = fitz.open(stream=bytes(data), filetype="pdf")
        df_works, positions, meta = load_vor_frame_positions(vor_bytes, vor_filename)
        pool_keys = list(pool.keys())
        for position in positions:
            index = int(position.index)
            try:
                prep = _prepare_one_position(
                    context=context,
                    run_id=run_id,
                    worker=worker,
                    vor_name=vor_filename,
                    position=position,
                    meta=meta,
                    pool_keys=pool_keys,
                    pdf_versions=pdf_versions,
                )
                if prep.get("skipped"):
                    queued.append(index)
                    continue
                pdf_name = prep["pdf_name"]
                bind_error = prep["bind_error"]
                target_indices: list[int] = []
                loc_desc = "Страница не указана"
                primary_pages: list[int] = []
                if pdf_name and not bind_error:
                    doc = pool[pdf_name]
                    target_indices, loc_desc, primary_pages = (
                        get_optimized_page_indices(doc, position.page_ref)
                    )
                    if pdf_name not in sources:
                        def progress(done, total):
                            context.repository.update_run_config(run_id, {
                                "pdf_preparation": {"done": done, "total": total},
                            })

                        progress(0, len(doc))
                        sources[pdf_name] = PDFTextSource(doc, progress=progress)
                    # Pre-warm: one OCR allowance now, so LLM workers reuse
                    # memory/disk cache instead of repeating recognition.
                    sources[pdf_name].for_position(
                        target_indices,
                        position.work_name,
                        position.volume,
                        position.reference,
                        position.comment,
                    )
                payload = _task_payload(
                    position,
                    pdf_name,
                    target_indices,
                    len(pool[pdf_name]) if pdf_name else 0,
                )
                payload.update(
                    {
                        "bind_error": bind_error,
                        "loc_desc": loc_desc,
                        "primary_pages": primary_pages,
                        "vor_filename": vor_filename,
                        "project": project,
                        "request_id": request_id,
                        "pdf_versions": {
                            key: str(row.id) for key, row in pdf_versions.items()
                        },
                    }
                )
                context.repository.requeue_position_job(
                    run_id, index, queue=LLM_QUEUE, payload=payload
                )
                if dispatch is not None:
                    position_payload: dict[str, Any] = {
                        "run_id": str(run_id),
                        "position_index": index,
                    }
                    if request_id:
                        position_payload["request_id"] = request_id
                    dispatch("audit_tasks.audit_position", position_payload)
                queued.append(index)
            except Exception as exc:
                logger.exception("prepare position %s of run %s failed", index, run_id)
                failure = technical_error(
                    f"Подготовка позиции не выполнена: {type(exc).__name__}: {str(exc)[:300]}"
                )
                context.repository.finish_position_job(
                    run_id,
                    index,
                    "done",
                    result={"technical": dict(failure), "w_name": position.work_name},
                    error=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
                failed.append(index)
        logger.info(
            "prepare finished",
            extra={"queued": len(queued), "failed": len(failed)},
        )
        prepare_seconds = round(time.monotonic() - started, 3)
        try:
            context.repository.update_run_config(
                run_id, {"prepare_seconds": prepare_seconds}
            )
        except Exception:
            logger.warning("could not persist prepare timing", exc_info=True)
        return {
            "run_id": str(run_id),
            "vor_filename": vor_filename,
            "df_rows": len(df_works),
            "queued": queued,
            "failed": failed,
            "prepare_seconds": prepare_seconds,
        }
    finally:
        for source in sources.values():
            try:
                source.close()
            except Exception:
                pass
        for doc in pool.values():
            try:
                if not doc.is_closed:
                    doc.close()
            except Exception:
                pass
        trace_run.close()
        reset(token)


def audit_position(
    context: TaskContext,
    run_id: UUID,
    position_index: int,
    *,
    worker: str,
    pdf_files: dict[str, bytes] | None = None,
    pdf_versions: dict[str, Any] | None = None,
    request_id: str = "",
    dispatch: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Network stage: audit one position through the LLM, persist its result.

    Runs on the ``llm`` queue. Rebuilds the PageTextIndex snapshot from the
    persistent PDF cache (same ``PDFTextSource`` + ``for_position`` path as
    the in-process runner), calls the same ``worker_check_row`` adapter, and
    stores the outcome in both ``position_results`` and ``position_jobs``.
    Idempotent: a finished ``done`` job returns its stored payload.
    With ``dispatch`` set (Celery path), the last finished position enqueues
    ``finalize_run`` on the ``pdf`` queue; without it (tests/fallback) the
    caller runs ``finalize_run`` itself.
    """
    import fitz

    started = time.monotonic()
    job = context.repository.load_position_job(run_id, position_index)
    if job.status == "done" and job.result:
        return {"position_index": position_index, "duplicate": True}
    job = context.repository.claim_position_job(run_id, position_index, worker=worker)
    payload = dict(job.payload or {})
    resolved_request = str(request_id or payload.get("request_id") or "")
    token = bind(
        audit_run_id=str(run_id), position_index=position_index,
        request_id=resolved_request or None, stage="audit",
        worker=worker, queue=LLM_QUEUE,
    )
    stored: dict[str, Any] | None = None
    try:
        payload = job.payload
        w_name = payload.get("work_name", "")
        bind_error = payload.get("bind_error")
        pdf_name = payload.get("pdf_name", "")
        if bind_error or not pdf_name:
            details = (
                bind_error or f"Не удалось привязать PDF к строке. Искали: '{w_name}'"
            )
            res_item = {
                "Статус аудита": "Ошибка",
                "Фактический объем": "-",
                "Фактическая страница в ПД (найдено моделью)": "-",
                "Детали расхождений": details,
                "work_found": False,
                "volume_confirmed": False,
                "unit_confirmed": False,
                "comment_confirmed": False,
                "found_pages": [],
                "evidence": [],
                "search_complete": False,
                "requests": 0,
                "contexts": 0,
                "budget_tokens": None,
                "usage": {},
            }
            track_item = {
                "pdf_name": payload.get("pdf_name", ""),
                "vor_pages": list(payload.get("primary_pages") or []),
                "found_pages": [],
                "evidence": [],
                "work_found": False,
                "volume_confirmed": False,
                "unit_confirmed": False,
                "comment_confirmed": False,
                "found_unit": "-",
                "found_volume": "-",
                "search_complete": False,
                "requests": 0,
                "contexts": 0,
                "budget_tokens": None,
                "usage": {},
                "work_name": w_name,
            }
        else:
            files = (
                pdf_files
                if pdf_files is not None
                else _load_run_pdf_bytes(context, run_id)
            )
            data = files.get(pdf_name)
            if data is None:
                raise KeyError(f"PDF bytes missing for {pdf_name!r}")
            with fitz.open(stream=bytes(data), filetype="pdf") as doc:
                target_indices = list(payload.get("target_indices") or [])
                source = PDFTextSource(doc)
                try:
                    text_index = source.for_position(
                        target_indices,
                        w_name,
                        payload.get("volume", ""),
                        payload.get("reference", ""),
                        payload.get("comment", ""),
                    )
                finally:
                    source.close()
            trace_run = TracingRun(
                context.settings.langfuse_public_key,
                context.settings.langfuse_secret_key,
                context.settings.langfuse_base_url,
                context.settings.langfuse_environment,
                audit_run_id=str(run_id),
                request_id=resolved_request,
                redact_pdf=context.settings.telemetry_redact_pdf,
                mask_prompts=context.settings.telemetry_mask_prompts,
            )
            try:
                position_trace = trace_run.position(
                    input={
                        "work": w_name,
                        "volume": payload.get("volume", ""),
                        "unit": payload.get("unit", ""),
                        "reference": payload.get("reference", ""),
                        "pages": payload.get("page_ref", ""),
                        "comment": payload.get("comment", ""),
                        "formula": payload.get("formula", ""),
                    },
                    metadata={
                        "vor_file": payload.get("vor_filename", ""),
                        "position_index": position_index,
                        "provider": "Yandex Cloud (Qwen)",
                        "model": context.settings.yandex_cloud_model,
                    },
                )
                from audit_models import VorPosition

                task = _build_task_dict(
                    position=VorPosition(
                        index=position_index,
                        work_name=w_name or "position",
                        volume=payload.get("volume", "") or "-",
                        unit=payload.get("unit", "") or "-",
                        reference=payload.get("reference", ""),
                        page_ref=payload.get("page_ref", ""),
                        comment=payload.get("comment", ""),
                        formula=payload.get("formula", ""),
                        explicit_refs=list(payload.get("explicit_refs") or []),
                    ),
                    pdf_name=pdf_name,
                    bind_error=None,
                    prep_error=None,
                    text_index=text_index,
                    target_indices=target_indices,
                    loc_desc=payload.get("loc_desc", "Страница не указана"),
                    primary_pages=list(payload.get("primary_pages") or []),
                    position_trace=position_trace,
                )
                audit_fn = partial(
                    context.auditor,
                    api_key=context.settings.yandex_cloud_api_key,
                    folder=context.settings.yandex_cloud_folder,
                    model_name=context.settings.yandex_cloud_model,
                    client_factory=context.client_factory,
                    sleeper=(lambda _: None),
                    allowed_hosts=tuple(context.settings.allowed_llm_hosts or ()),
                )
                _, res_item, track_item = traced_worker_check_row(task, audit_fn)
                position_trace.finish(res_item)
                track_item["trace_id"] = position_trace.trace_id
                track_item["trace_url"] = position_trace.url()
            finally:
                trace_run.close()
        versions = (
            pdf_versions
            if pdf_versions is not None
            else _load_run_pdf_versions(context, run_id)
        )
        stored = _position_result_payload(
            w_name=w_name,
            res_item=res_item,
            track_item=track_item,
            index=position_index,
            pdf_versions=versions,
            settings=context.settings,
        )
        context.repository.save_position_result(run_id, position_index, stored)
        context.repository.finish_position_job(
            run_id, position_index, "done", result=stored
        )
        _maybe_chain_finalize(
            context, run_id, dispatch=dispatch, request_id=resolved_request
        )
        logger.info(
            "position audited",
            extra={
                "status": stored.get("status"),
                "requests": stored.get("requests", 0),
                "seconds": round(time.monotonic() - started, 3),
            },
        )
        return {"position_index": position_index, "duplicate": False}
    except Exception as exc:
        logger.exception("audit position %s of run %s failed", position_index, run_id)
        context.repository.finish_position_job(
            run_id,
            position_index,
            "failed",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )
        raise
    finally:
        reset(token)


def _maybe_chain_finalize(
    context: TaskContext,
    run_id: UUID,
    *,
    dispatch: Callable[[str, dict[str, Any]], None] | None,
    request_id: str = "",
) -> None:
    """Enqueue ``finalize_run`` once every position reached a terminal state.

    The ``finalize_chained`` flag in ``runs.config`` makes the trigger
    exactly-once across racing llm workers; only the worker that flips the
    flag dispatches. Failures here never fail the position itself.
    """
    if dispatch is None:
        return
    try:
        jobs = context.repository.list_position_jobs(run_id)
    except Exception:
        logger.warning("finalize chain: cannot list jobs", exc_info=True)
        return
    if not jobs:
        return
    if any(job.status not in {"done", "failed"} for job in jobs):
        return
    try:
        claimed = context.repository.claim_finalize(run_id)
    except Exception:
        logger.warning("finalize chain: cannot claim flag", exc_info=True)
        return
    if not claimed:
        return
    payload: dict[str, Any] = {"run_id": str(run_id)}
    if request_id:
        payload["request_id"] = str(request_id)
    try:
        dispatch("audit_tasks.finalize_run", payload)
    except Exception:
        logger.warning("finalize chain: dispatch failed", exc_info=True)


def _position_result_payload(
    *,
    w_name: str,
    res_item: dict[str, Any],
    track_item: dict[str, Any],
    index: int,
    pdf_versions: dict[str, Any],
    settings: Any = None,
) -> dict[str, Any]:
    """Same stored shape as ``save_vor_run_payload``; never diverges."""
    _ = index
    payload = {
        "status": res_item.get("Статус аудита"),
        "work_name": w_name,
        "work_found": bool(res_item.get("work_found")),
        "volume_confirmed": bool(res_item.get("volume_confirmed")),
        "unit_confirmed": bool(res_item.get("unit_confirmed")),
        "comment_confirmed": bool(res_item.get("comment_confirmed")),
        "found_volume": res_item.get("Фактический объем", "-"),
        "found_unit": track_item.get("found_unit", "-"),
        "found_page": track_item.get("found_page"),
        "found_pages": list(track_item.get("found_pages") or []),
        "vor_pages": list(track_item.get("vor_pages") or []),
        "evidence": [dict(item) for item in track_item.get("evidence") or []],
        "volume_calculation": track_item.get("volume_calculation"),
        "discrepancy_details": res_item.get("Детали расхождений", ""),
        "search_complete": bool(track_item.get("search_complete")),
        "requests": track_item.get("requests", 0),
        "contexts": track_item.get("contexts", 0),
        "budget_tokens": res_item.get("budget_tokens", track_item.get("budget_tokens")),
        "usage": dict(track_item.get("usage") or {}),
        "estimated_cost": track_item.get("estimated_cost"),
        "pdf_name": track_item.get("pdf_name", ""),
        "trace_id": track_item.get("trace_id"),
        "trace_url": track_item.get("trace_url"),
    }
    pdf_name = track_item.get("pdf_name") or ""
    if pdf_name in pdf_versions:
        row = pdf_versions[pdf_name]
        payload["pdf_version_id"] = str(row.id)
        payload["pdf_sha256"] = row.sha256
    _ = settings
    return payload


def _load_run_pdf_bytes(context: TaskContext, run_id: UUID) -> dict[str, bytes]:
    from uuid import UUID as _UUID

    record = context.repository.load_run(run_id)
    config = record.get("config") or {}
    mapping = config.get("pdf_versions") or {}
    files: dict[str, bytes] = {}
    for key, version_id in mapping.items():
        ref = context.repository.load_version(_UUID(str(version_id)))
        files[key] = context.blob_store.get(ref.blob_key)
    return files


def _load_run_pdf_versions(context: TaskContext, run_id: UUID) -> dict[str, Any]:
    from uuid import UUID as _UUID

    from storage_db import DocumentVersionRow

    record = context.repository.load_run(run_id)
    config = record.get("config") or {}
    mapping = config.get("pdf_versions") or {}
    versions: dict[str, Any] = {}
    for key, version_id in mapping.items():
        ref = context.repository.load_version(_UUID(str(version_id)))
        versions[key] = DocumentVersionRow(
            id=ref.id,
            document_id=ref.document_id,
            version_no=ref.version_no,
            sha256=ref.sha256,
            blob_key=ref.blob_key,
            size=ref.size,
        )
    return versions


def finalize_run(
    context: TaskContext,
    run_id: UUID,
    *,
    worker: str,
    vor_filename: str | None = None,
    vor_bytes: bytes | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Merge stage: rebuild df/excel/word from stored results, finish the run.

    Runs on the ``pdf`` queue. Every position must be ``done`` or ``failed``
    (a failed worker position becomes a technical payload, never a silent
    gap). Uses the shared ``build_run_artifacts`` so exports match the
    in-process runner byte-for-shape.
    """
    started = time.monotonic()
    record = context.repository.load_run(run_id)
    config = record.get("config") or {}
    resolved_request = str(request_id or config.get("request_id") or "")
    token = bind(
        audit_run_id=str(run_id), request_id=resolved_request or None,
        stage="finalize", worker=worker, queue=PDF_QUEUE,
    )
    try:
        vor_name = vor_filename or config.get("vor_filename") or record.get("vor_filename")
        if vor_bytes is None:
            ref = context.repository.load_version(record["vor_version_id"])
            vor_data = context.blob_store.get(ref.blob_key)
        else:
            vor_data = bytes(vor_bytes)
        df_works, positions, meta = load_vor_frame_positions(vor_data, vor_name)
        jobs = {
            job.position_index: job for job in context.repository.list_position_jobs(run_id)
        }
        results = record.get("results") or []
        # load_run orders by position_index: zip with sorted jobs, never enumerate.
        ordered_jobs = [jobs[position.index] for position in positions]
        ordered = [jobs[position.index].result or {} for position in positions]
        if len(results) == len(positions):
            ordered = list(results)
        tasks: list[dict[str, Any]] = []
        tracking: list[dict[str, Any]] = []
        results_list: list[dict[str, Any]] = []
        for position, stored, job in zip(positions, ordered, ordered_jobs):
            if job.status not in {"done", "failed"}:
                raise RuntimeError(
                    f"position {position.index} is {job.status}; finalize needs done/failed"
                )
            res_item, track_item = _stored_to_items(position, stored, job)
            job_payload = job.payload or {}
            position_trace = _NullTrace()
            tasks.append(
                _build_task_dict(
                    position=position,
                    pdf_name=track_item.get("pdf_name", ""),
                    bind_error=job_payload.get("bind_error"),
                    prep_error=None,
                    text_index=None,
                    target_indices=list(job_payload.get("target_indices") or []),
                    loc_desc=str(job_payload.get("loc_desc", "") or ""),
                    primary_pages=list(track_item.get("vor_pages") or []),
                    position_trace=position_trace,
                )
            )
            tracking.append(track_item)
            results_list.append(res_item)
        artifacts = build_run_artifacts(df_works, meta, tasks, tracking, results_list)
        versions = _load_run_pdf_versions(context, run_id)
        context.repository.save_vor_run_payload(
            run_id,
            tasks=tasks,
            tracking=tracking,
            results=results_list,
            excel_bytes=bytes(artifacts["excel_bytes"]),
            word_bytes=bytes(artifacts["word_bytes"]),
            pdf_versions=dict(versions),
        )
        metrics = RunMetrics(run_id=str(run_id))
        metrics.record_stage("finalize", time.monotonic() - started)
        if isinstance(config.get("prepare_seconds"), (int, float)):
            metrics.record_stage("prepare", float(config["prepare_seconds"]))
        for stored, job in zip(ordered, ordered_jobs):
            payload = dict(stored or {})
            if not payload and job.result:
                payload = {"status": "Технический сбой"}
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            metrics.record_position(
                {"status": payload.get("status", "unknown"),
                 "requests": payload.get("requests", 0),
                 "contexts": payload.get("contexts", 0)},
                usage=usage,
                cost=estimate_cost(
                    usage,
                    float(context.settings.llm_input_cost_per_1k or 0.0),
                    float(context.settings.llm_output_cost_per_1k or 0.0),
                ),
            )
            if job.status == "failed" or payload.get("status") == "Технический сбой":
                metrics.record_error(job.error or "technical")
        summary = metrics.finish()
        context.repository.finish_run(run_id, "done", metrics=summary)
        logger.info(
            "finalize finished",
            extra={
                "positions": len(positions),
                "seconds": round(time.monotonic() - started, 3),
            },
        )
        return {"run_id": str(run_id), "positions": len(positions), "metrics": summary}
    finally:
        reset(token)


def _stored_to_items(
    position: Any, stored: dict[str, Any], job: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    from audit_core import TECHNICAL_ERROR

    if stored:
        found_pages = sorted(
            {int(p) for p in (stored.get("found_pages") or []) if isinstance(p, int)}
        )
        display = ("Стр. " + ", ".join(map(str, found_pages))) if found_pages else "-"
        work_found = bool(stored.get("work_found"))
        vol_ok = bool(stored.get("volume_confirmed"))
        unit_ok = bool(stored.get("unit_confirmed"))
        comm_ok = bool(stored.get("comment_confirmed"))
        evidence = [dict(e) for e in stored.get("evidence") or []]
        kind_ru = {
            "work": "работа",
            "volume": "объём",
            "unit": "ед.",
            "comment": "прим.",
        }
        res_item = {
            "Статус аудита": stored.get("status", TECHNICAL_ERROR),
            "Фактический объем": stored.get("found_volume", "-"),
            "Фактическая страница в ПД (найдено моделью)": display,
            "Детали расхождений": stored.get("discrepancy_details", ""),
            "[ПД] Найденная единица": stored.get("found_unit", "-"),
            "[ПД] Подтверждения (работа/объём/ед./прим.)": (
                f"Работа: {'да' if work_found else 'нет'}; "
                f"Объём: {'да' if vol_ok else 'нет'}; "
                f"Ед.: {'да' if unit_ok else 'нет'}; "
                f"Прим.: {'да' if comm_ok else 'нет'}"
            ),
            "[ПД] Доказательства (цитаты со страницами)": (
                "; ".join(
                    f"[Стр. {e.get('page')}, {kind_ru.get(e.get('kind'), e.get('kind'))}] «{e.get('quote', '')}»"
                    for e in evidence
                )
                or "-"
            ),
            "[ПД] Найденные страницы": ", ".join(map(str, found_pages))
            if found_pages
            else "-",
            "[ПД] Поиск завершён": "да" if stored.get("search_complete") else "нет",
            "[ПД] Запросов": stored.get("requests", 0),
            "[ПД] Контекстов": stored.get("contexts", 0),
            "work_found": work_found,
            "volume_confirmed": vol_ok,
            "unit_confirmed": unit_ok,
            "comment_confirmed": comm_ok,
            "budget_tokens": stored.get("budget_tokens"),
            "usage": dict(stored.get("usage") or {}),
        }
        track_item = {
            "pdf_name": stored.get("pdf_name", ""),
            "vor_pages": list(stored.get("vor_pages") or []),
            "found_page": stored.get("found_page"),
            "found_pages": found_pages,
            "evidence": evidence,
            "work_found": work_found,
            "volume_confirmed": vol_ok,
            "unit_confirmed": unit_ok,
            "comment_confirmed": comm_ok,
            "found_unit": stored.get("found_unit", "-"),
            "found_volume": stored.get("found_volume", "-"),
            "volume_calculation": stored.get("volume_calculation"),
            "search_complete": bool(stored.get("search_complete")),
            "requests": stored.get("requests", 0),
            "contexts": stored.get("contexts", 0),
            "budget_tokens": stored.get("budget_tokens"),
            "usage": dict(stored.get("usage") or {}),
            "work_name": stored.get("work_name", position.work_name),
            "trace_id": stored.get("trace_id"),
            "trace_url": stored.get("trace_url"),
        }
        return res_item, track_item
    technical = (job.result or {}).get("technical") if job.result else None
    details = str(
        (technical or {}).get("discrepancy_details") or job.error or "Сбой позиции"
    )
    res_item = {
        "Статус аудита": TECHNICAL_ERROR,
        "Фактический объем": "-",
        "Фактическая страница в ПД (найдено моделью)": "-",
        "Детали расхождений": details,
        "[ПД] Найденная единица": "-",
        "[ПД] Найденные страницы": "-",
        "[ПД] Поиск завершён": "нет",
        "[ПД] Запросов": 0,
        "[ПД] Контекстов": 0,
        "work_found": False,
        "volume_confirmed": False,
        "unit_confirmed": False,
        "comment_confirmed": False,
        "budget_tokens": None,
        "usage": {},
    }
    track_item = {
        "pdf_name": "",
        "vor_pages": [],
        "found_pages": [],
        "evidence": [],
        "work_found": False,
        "volume_confirmed": False,
        "unit_confirmed": False,
        "comment_confirmed": False,
        "found_unit": "-",
        "found_volume": "-",
        "search_complete": False,
        "requests": 0,
        "contexts": 0,
        "budget_tokens": None,
        "usage": {},
        "work_name": position.work_name,
    }
    return res_item, track_item


class _NullTrace:
    def stage(self, *_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        from contextlib import nullcontext

        from audit_tracing import Observation

        return nullcontext(Observation())

    def finish(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def recover_run(
    context: TaskContext,
    run_id: UUID,
    *,
    alive_workers: set[str] | None = None,
    dispatch: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Restart recovery: requeue orphaned jobs and redispatch them."""
    recovered = context.repository.recover_stale_jobs(
        run_id, alive_workers=set(alive_workers or set())
    )
    for index in recovered:
        job = context.repository.load_position_job(run_id, index)
        if dispatch is not None:
            # prepare_run takes whole-run args; per-position requeue
            # always redispatches the single-position auditor.
            payload = {"run_id": str(run_id), "position_index": index}
            request_id = (job.payload or {}).get("request_id")
            if request_id:
                payload["request_id"] = str(request_id)
            dispatch("audit_tasks.audit_position", payload)
    return {"run_id": str(run_id), "recovered": recovered}


def register_tasks(app):  # type: ignore[no-untyped-def]
    """Bind ``*_run`` functions to Celery task names; called by the worker."""

    @app.task(name="audit_tasks.prepare_run", bind=True)
    def _prepare(  # type: ignore[no-untyped-def]
        self, run_id, project, vor_filename, vor_blob_key, pdf_blob_keys,
        request_id="",
    ):
        context = open_task_context()
        vor_data = context.blob_store.get(vor_blob_key)
        files = {
            key: context.blob_store.get(blob_key)
            for key, blob_key in pdf_blob_keys.items()
        }
        versions = _load_run_pdf_versions(context, UUID(str(run_id)))
        # prepare_run dispatches each queued position itself: no second fan-out.
        return prepare_run(
            context,
            UUID(str(run_id)),
            project=project,
            vor_filename=vor_filename,
            vor_bytes=vor_data,
            pdf_files=files,
            pdf_versions=versions,
            worker=self.request.hostname or "pdf-worker",
            dispatch=lambda name, payload: app.send_task(name, kwargs=payload),
            request_id=str(request_id or ""),
        )

    @app.task(
        name="audit_tasks.audit_position",
        bind=True,
        max_retries=3,
        autoretry_for=(Exception,),
        retry_backoff=True,
    )
    def _audit(self, run_id, position_index, request_id=""):  # type: ignore[no-untyped-def]
        context = open_task_context()
        return audit_position(
            context,
            UUID(str(run_id)),
            int(position_index),
            worker=self.request.hostname or "llm-worker",
            request_id=str(request_id or ""),
            dispatch=lambda name, payload: app.send_task(name, kwargs=payload),
        )

    @app.task(name="audit_tasks.finalize_run", bind=True)
    def _finalize(self, run_id, request_id=""):  # type: ignore[no-untyped-def]
        context = open_task_context()
        return finalize_run(
            context,
            UUID(str(run_id)),
            worker=self.request.hostname or "pdf-worker",
            request_id=str(request_id or ""),
        )

    @app.task(name="audit_tasks.recover_run")
    def _recover(run_id):  # type: ignore[no-untyped-def]
        context = open_task_context()
        alive: set[str] = set()
        try:
            inspect = app.control.inspect()
            stats = inspect.stats() or {}
            alive = set(stats.keys())
        except Exception:
            alive = set()
        return recover_run(
            context,
            UUID(str(run_id)),
            alive_workers=alive,
            dispatch=lambda name, payload: app.send_task(name, kwargs=payload),
        )

    return app


def make_worker(settings: AuditSettings | None = None):  # type: ignore[no-untyped-def]
    """Worker entry: ``celery -A audit_tasks:worker worker -Q pdf``."""
    app = celery_app(settings or AuditSettings.load())
    return register_tasks(app)


worker = None
try:
    _settings = AuditSettings.load()
    if _settings.broker_url:
        worker = make_worker(_settings)
except Exception:
    worker = None

__all__ = [
    "LLM_QUEUE",
    "PDF_QUEUE",
    "DEFAULT_TASK_ROUTES",
    "TaskContext",
    "audit_position",
    "celery_app",
    "finalize_run",
    "make_worker",
    "open_task_context",
    "prepare_run",
    "recover_run",
    "register_tasks",
    "worker_check_row",
]
