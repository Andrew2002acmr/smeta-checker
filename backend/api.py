"""REST API over the extracted audit services (issue 18).

Headless counterpart to ``app.py``: documents import into the issue-17
repository, audits dispatch to Celery workers (issue 19) or run the same
``audit_tasks`` pipeline in a fallback thread without a broker, results and
report downloads read back from storage. No UI framework is imported here;
integration tests use an httpx ASGI transport against the same app factory.

Run locally: ``uv run uvicorn api:get_app --factory``.
"""

from __future__ import annotations

import hmac
import mimetypes
import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from audit_models import AuditSettings
from audit_observability import bind, get_logger, reset, restore, snapshot
from audit_service import preflight_bindings, run_audit
from storage_blobs import BlobStore, open_blob_store
from storage_db import DocumentVersionRow, Repository, open_repository
from sentry_setup import init_sentry
from metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    render_metrics,
)

logger = get_logger(__name__)

VOR_EXTENSIONS = (".xlsx", ".csv")
PDF_EXTENSION = ".pdf"
VOR_MIME_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
})
PDF_MIME_TYPES = frozenset({"application/pdf"})
OCTET_STREAM = "application/octet-stream"
POSITION_DONE_STATUSES = frozenset({"done", "failed"})
REQUEST_ID_HEADER = "X-Request-ID"


class ApiError(Exception):
    """Structured API failure; rendered as ``{"error": {code, message, details}}``."""

    def __init__(
        self, status: int, code: str, message: str, details: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class DocumentUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    version_no: int = Field(ge=1)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class PreflightRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vor: str
    refs: str
    status: str
    pdf: str


class PreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[PreflightRow]
    has_critical_error: bool


class AuditCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(default="", max_length=256)
    project_id: str | None = None
    vor_version_id: str = Field(min_length=1)
    pdf_version_ids: list[str] = Field(default_factory=list)


class AuditCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class AuditStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    vor_filename: str = ""
    position_count: int = Field(ge=0)
    config: dict[str, Any] = Field(default_factory=dict)


class AuditResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    vor_filename: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)


def _error_payload(exc: ApiError) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details is not None:
        payload["details"] = exc.details
    return {"error": payload}


@dataclass(frozen=True, slots=True)
class ApiContext:
    settings: AuditSettings
    repository: Repository
    blob_store: BlobStore
    client_factory: Callable[..., Any] | None = None
    auditor: Callable[..., Any] = run_audit
    dispatcher: Callable[[str, dict[str, Any]], None] | None = None


@dataclass(slots=True)
class AuditJob:
    run_id: UUID
    thread: threading.Thread | None = None
    started: threading.Event = field(default_factory=threading.Event)


class AuditRunner:
    """Fallback background audits; with a broker, Celery owns execution."""

    def __init__(self, context: ApiContext) -> None:
        self._context = context
        self._jobs: dict[UUID, AuditJob] = {}
        self._lock = threading.Lock()

    def start(
        self, run_id: UUID, *, target: Callable[[], None], daemon: bool = True
    ) -> None:
        job = AuditJob(run_id=run_id)
        thread = threading.Thread(
            target=self._guarded(target, run_id),
            name=f"audit-{run_id}",
            daemon=daemon,
        )
        job.thread = thread
        with self._lock:
            self._jobs[run_id] = job
        thread.start()
        job.started.set()

    def wait(self, run_id: UUID, timeout: float | None = None) -> bool:
        with self._lock:
            job = self._jobs.get(run_id)
        if job is None or job.thread is None:
            return True
        job.started.wait(timeout)
        job.thread.join(timeout)
        return not job.thread.is_alive()

    def _guarded(self, target: Callable[[], None], run_id: UUID) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except Exception:
                logger.exception("background audit %s failed", run_id)
                try:
                    self._context.repository.finish_run(run_id, "failed")
                except Exception:
                    logger.exception("could not mark audit %s failed", run_id)

        return run


def _request_id(request: Request | None) -> str:
    """Client-supplied ``X-Request-ID`` or a fresh one; echoed back."""
    if request is not None:
        supplied = str(request.headers.get(REQUEST_ID_HEADER, "")).strip()
        if supplied and len(supplied) <= 128:
            return supplied
    return uuid4().hex


def _parse_uuid(value: str, *, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            400, "invalid_uuid", f"Некорректный {field}: {value!r}."
        ) from exc


def _safe_filename(filename: str) -> str:
    """Basename only: strips directories, NUL bytes and blank names."""
    cleaned = str(filename or "").replace("\x00", "").replace("\\", "/")
    cleaned = cleaned.rsplit("/", 1)[-1].strip().strip(".")
    if not cleaned or cleaned in {".", ".."}:
        raise ApiError(400, "filename_required", "Имя файла обязательно.")
    return cleaned[:255]


def _clean_pool_key(filename: str) -> str:
    return _safe_filename(filename).lower()


def _check_upload_limits(context: ApiContext, data: bytes, *, count: int = 1) -> None:
    limit = max(1, int(context.settings.max_upload_bytes or 0))
    if len(data) > limit:
        raise ApiError(
            413,
            "upload_too_large",
            f"Файл больше лимита {limit} байт.",
        )
    allowed = max(1, int(context.settings.max_uploads_per_request or 1))
    if count > allowed:
        raise ApiError(
            400,
            "too_many_uploads",
            f"Больше {allowed} файлов за один запрос.",
        )


def _check_upload_mime(upload: UploadFile, kind: str) -> None:
    """Declared Content-Type must match the kind; bytes are verified later."""
    declared = str(upload.content_type or "").split(";")[0].strip().lower()
    if not declared or declared == OCTET_STREAM:
        guessed, _ = mimetypes.guess_type(upload.filename or "")
        declared = (guessed or "").lower()
    if not declared or declared == OCTET_STREAM:
        return
    allowed = VOR_MIME_TYPES if kind == "vor" else PDF_MIME_TYPES
    if declared not in allowed:
        raise ApiError(
            400,
            "unsupported_media_type",
            f"Тип {declared or 'неизвестен'} не подходит для {kind}.",
        )


def _check_pdf_magic(data: bytes) -> None:
    if not bytes(data).lstrip()[:5] == b"%PDF-":
        raise ApiError(400, "invalid_pdf", "Файл не похож на PDF (%PDF-).")


def _check_vor_magic(data: bytes, filename: str) -> None:
    payload = bytes(data)
    if filename.lower().endswith(".xlsx"):
        if payload[:2] != b"PK":
            raise ApiError(400, "invalid_vor", "Файл не похож на .xlsx (ZIP).")
    elif b"\x00" in payload[:1024]:
        raise ApiError(400, "invalid_vor", "Файл не похож на CSV (NUL-байты).")


def _require_configured(context: ApiContext) -> None:
    if not context.settings.database_url or not context.settings.blob_store_url:
        raise ApiError(
            503,
            "storage_not_configured",
            "Хранилище не настроено: заполните DATABASE_URL и BLOB_STORE_URL.",
        )


def _require_llm_credentials(settings: AuditSettings) -> None:
    if not settings.llm_credentials().api_key:
        raise ApiError(
            503,
            "llm_not_configured",
            "LLM не настроен: заполните YANDEX_CLOUD_API_KEY или ANYMODEL_API_KEY в .env.",
        )


def _require_api_token(context: ApiContext, request: Request) -> None:
    """Bearer-guard: with AUDIT_API_TOKEN set, /api/v1/* requires it.

    Empty token keeps local/dev usage open; production compose always sets it.
    """
    expected = (context.settings.api_token or "").strip()
    if not expected:
        return
    presented = (request.headers.get("authorization", "") or "").strip()
    scheme, _, value = presented.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), expected):
        raise ApiError(
            401, "unauthorized", "Требуется заголовок Authorization: Bearer <токен>."
        )


def _read_version_bytes(
    context: ApiContext, version_id: UUID
) -> tuple[bytes, DocumentVersionRow, str, str]:
    try:
        ref = context.repository.load_version(version_id)
    except KeyError as exc:
        raise ApiError(
            404, "version_not_found", "Версия документа не найдена."
        ) from exc
    try:
        data = context.blob_store.get(ref.blob_key)
    except FileNotFoundError as exc:
        raise ApiError(
            500, "blob_missing", "Байты документа отсутствуют в хранилище."
        ) from exc
    row = DocumentVersionRow(
        id=ref.id,
        document_id=ref.document_id,
        version_no=ref.version_no,
        sha256=ref.sha256,
        blob_key=ref.blob_key,
        size=ref.size,
        filename=ref.filename,
        metadata=dict(ref.metadata or {}),
        metadata_overrides=dict(ref.metadata_overrides or {}),
    )
    return data, row, ref.filename, ref.kind


def _upload_document(
    context: ApiContext, project_id: UUID, kind: str, upload: UploadFile, data: bytes,
    document_id: UUID | None = None,
    new_document: bool = False,
) -> DocumentUploadResponse:
    from vor_parse import load_vor_positions

    filename = _safe_filename(upload.filename or "")
    _check_upload_limits(context, data)
    _check_upload_mime(upload, kind)
    lowered = filename.lower()
    if kind == "vor" and not lowered.endswith(VOR_EXTENSIONS):
        raise ApiError(
            400,
            "unsupported_vor_extension",
            "ВОР принимает только .xlsx и .csv.",
        )
    if kind == "pdf" and not lowered.endswith(PDF_EXTENSION):
        raise ApiError(400, "unsupported_pdf_extension", "ПД принимает только .pdf.")
    if not data:
        raise ApiError(400, "empty_upload", "Загруженный файл пуст.")
    if kind == "vor":
        _check_vor_magic(data, filename)
        try:
            positions, _ = load_vor_positions(data, filename)
        except ValueError as exc:
            raise ApiError(
                400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}"
            ) from exc
        except Exception as exc:
            raise ApiError(
                400,
                "invalid_vor",
                f"Не удалось прочитать ВОР: {type(exc).__name__}: {str(exc)[:300]}",
            ) from exc
        if not positions:
            raise ApiError(400, "empty_vor", "В ВОР нет позиций для проверки.")
    else:
        _check_pdf_magic(data)
        try:
            import pymupdf

            with pymupdf.open(stream=bytes(data), filetype="pdf") as doc:
                if doc.page_count < 1:
                    raise ApiError(400, "empty_pdf", "PDF не содержит страниц.")
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                400,
                "invalid_pdf",
                f"Не удалось прочитать PDF: {type(exc).__name__}: {str(exc)[:200]}",
            ) from exc
    if document_id is not None and new_document:
        raise ApiError(400, "document_ambiguous",
                       "Укажите либо document_id, либо new_document.")
    try:
        version = context.repository.add_document_version(
            project_id, bytes(data), filename=filename, kind=kind,
            document_id=document_id, new_document=new_document,
        )
    except KeyError as exc:
        raise ApiError(404, "document_not_found", "Документ не найден") from exc
    except ValueError as exc:
        raise ApiError(400, "document_invalid", str(exc)) from exc
    except Exception as exc:
        raise ApiError(
            500, "storage_error", f"Не удалось сохранить документ: {exc}"
        ) from exc
    return DocumentUploadResponse(
        version_id=str(version.id),
        document_id=str(version.document_id),
        version_no=version.version_no,
        sha256=version.sha256,
        size=version.size,
    )


def _include_bound_versions(context, vor_id, pdf_ids):
    chosen = set(context.repository.load_pdf_bindings(vor_id).values())
    rows = []
    for raw in sorted(chosen):
        try:
            rows.append(context.repository.load_version(_parse_uuid(raw, field="pdf_bindings")))
        except KeyError as exc:
            raise ApiError(400, "binding_pdf_unavailable", "Выбранный ПД удалён. Обновите привязку ВОР.") from exc
    documents = {row.document_id for row in rows}
    result = [i for i in pdf_ids if context.repository.load_version(i).document_id not in documents]
    return list(dict.fromkeys([*result, *(row.id for row in rows)]))


def _pdf_pool_keys(context, pdf_ids):
    rows = [context.repository.load_version(i) for i in pdf_ids]
    counts = {}
    for row in rows:
        key = _clean_pool_key(row.filename)
        counts[key] = counts.get(key, 0) + 1
    return {row.id: (_clean_pool_key(row.filename) if counts[_clean_pool_key(row.filename)] == 1 else f"{_clean_pool_key(row.filename)} [версия {row.id}]") for row in rows}


def _pdf_binding_keys(context, vor_id, pdf_ids):
    """Freeze explicit version IDs into canonical worker keys; never guess a replacement."""
    allowed = set(pdf_ids)
    result = {}
    pool_keys = _pdf_pool_keys(context, pdf_ids)
    for ref, target in context.repository.load_pdf_bindings(vor_id).items():
        version_id = _parse_uuid(target, field="pdf_bindings")
        if version_id not in allowed:
            raise ApiError(400, "binding_pdf_unavailable", f"Для ссылки {ref} выбрана недоступная версия ПД. Обновите привязку или состав ПД.")
        result[ref] = pool_keys[version_id]
    return result


def _preflight_for_versions(
    context: ApiContext, vor_version_id: UUID, pdf_version_ids: list[UUID], bindings=None
) -> PreflightResponse:
    from vor_parse import load_vor_positions

    vor_bytes, _, vor_filename, vor_kind = _read_version_bytes(context, vor_version_id)
    if vor_kind != "vor":
        raise ApiError(400, "not_vor_version", "Указанная версия — не ВОР.")
    pool_keys: dict[str, str] = {}
    version_keys = _pdf_pool_keys(context, pdf_version_ids)
    for pdf_id in pdf_version_ids:
        _, _, filename, kind = _read_version_bytes(context, pdf_id)
        if kind != "pdf":
            raise ApiError(400, "not_pdf_version", "Указанная версия — не PDF.")
        pool_keys[version_keys[pdf_id]] = str(pdf_id)
    try:
        positions, meta = load_vor_positions(vor_bytes, vor_filename)
    except Exception as exc:
        raise ApiError(400, "invalid_vor", f"Не удалось прочитать ВОР: {exc}") from exc
    if bindings is None:
        bindings = _pdf_binding_keys(context, vor_version_id, pdf_version_ids)
    rows, has_critical_error = preflight_bindings(
        {vor_filename: vor_bytes}, [vor_filename], pool_keys.keys(), bindings
    )
    return PreflightResponse(
        rows=[
            PreflightRow(
                vor=str(row.get("Ведомость (ВОР)", vor_filename)),
                refs=str(row.get("Путь в ячейке ВОР", "")),
                status=str(row.get("Статус привязки", "")),
                pdf=str(row.get("Фактический PDF", "")),
            )
            for row in rows
        ],
        has_critical_error=bool(has_critical_error or not positions or not meta),
    )


def _run_record(run: dict[str, Any]) -> AuditStatusResponse:
    return AuditStatusResponse(
        run_id=str(run["id"]),
        status=str(run.get("status", "")),
        vor_filename=str(run.get("vor_filename", "")),
        position_count=len(run.get("results") or []),
        config=dict(run.get("config") or {}),
    )


def _load_run_record(context: ApiContext, run_id: UUID) -> dict[str, Any]:
    try:
        return context.repository.load_run(run_id)
    except KeyError as exc:
        raise ApiError(404, "run_not_found", "Запуск аудита не найден.") from exc


def _project_for_payload(context: ApiContext, payload: AuditCreateRequest):
    if payload.project_id:
        try:
            return context.repository.load_project(_parse_uuid(payload.project_id, field="project_id"))
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc
    name = payload.project.strip()
    if not name:
        raise ApiError(400, "project_required", "Название проекта обязательно")
    return context.repository.get_or_create_project(name)


def _check_project_versions(context: ApiContext, project_id: UUID, version_ids: list[UUID]):
    for version_id in version_ids:
        try:
            owner = context.repository.version_project_id(version_id)
        except KeyError as exc:
            raise ApiError(404, "version_not_found", "Версия документа не найдена") from exc
        if owner != project_id:
            raise ApiError(400, "project_version_mismatch", "Версия документа принадлежит другому проекту")


def _check_active_versions(context: ApiContext, project_id: UUID,
                           version_ids: list[UUID]):
    for version_id in version_ids:
        ref = context.repository.load_version(version_id)
        document = context.repository.load_document(ref.document_id)
        if bool(document.get("archived")):
            raise ApiError(400, "version_archived", "Версия принадлежит архиву")
    try:
        project = context.repository.load_project(project_id)
    except KeyError as exc:
        raise ApiError(404, "project_not_found", "Проект не найден") from exc
    if bool(project.archived):
        raise ApiError(400, "project_archived", "Проект архивирован")


def _resolve_pdf_pool(context: ApiContext, project_id: UUID,
                      pdf_ids: list[UUID]) -> list[UUID]:
    """Default to current revisions of every active project PDF.

    An explicit non-empty selection stays untouched (tests, API clients);
    the UI no longer selects revisions — designations verify against the
    whole project pool at audit time.
    """
    if pdf_ids:
        return pdf_ids
    pool = []
    for document in context.repository.catalog(project_id, archived=False):
        if document.get("kind") != "pdf" or not document.get("current_version_id"):
            continue
        pool.append(_parse_uuid(str(document["current_version_id"]),
                                field="pdf_version_ids"))
    if not pool:
        raise ApiError(400, "pdf_pool_empty",
                       "В проекте нет проектных PDF для проверки.")
    return pool


def _check_pdf_names(context: ApiContext, version_ids: list[UUID]):
    documents = set()
    for version_id in version_ids:
        ref = context.repository.load_version(version_id)
        if ref.document_id in documents:
            raise ApiError(400, "duplicate_pdf_name", "Выберите одну версию каждого документа ПД")
        documents.add(ref.document_id)


def _celery_dispatch(
    context: ApiContext,
) -> Callable[[str, dict[str, Any]], None] | None:
    """Return the Celery dispatcher, or None when no broker is configured."""
    if context.dispatcher is not None:
        return context.dispatcher
    if not context.settings.broker_url:
        return None
    from audit_tasks import celery_app, register_tasks

    app = register_tasks(celery_app(context.settings))

    def _send(name: str, payload: dict[str, Any]) -> None:
        app.send_task(name, kwargs=dict(payload))

    return _send


def run_tasks_job(
    context: ApiContext,
    run_id: UUID,
    *,
    project: str,
    vor_filename: str,
    vor_bytes: bytes,
    pdf_files: dict[str, bytes],
    pdf_versions: dict[str, DocumentVersionRow],
    request_id: str = "",
    log_context: dict[str, Any] | None = None,
) -> None:
    """Fallback pipeline without a broker: prepare → audit → finalize inline.

    Uses the same ``audit_tasks`` functions Celery workers call, so results
    match the queued path. Failures mark the run ``failed``.
    """
    from audit_tasks import TaskContext, audit_position, finalize_run, prepare_run

    task_context = TaskContext(
        settings=context.settings,
        repository=context.repository,
        blob_store=context.blob_store,
        client_factory=context.client_factory,
    )
    token = restore(dict(log_context or {}))
    try:
        out = prepare_run(
            task_context,
            run_id,
            project=project,
            vor_filename=vor_filename,
            vor_bytes=bytes(vor_bytes),
            pdf_files=dict(pdf_files),
            pdf_versions=dict(pdf_versions),
            worker="api-fallback",
            dispatch=None,
            request_id=request_id,
        )
        for index in out["queued"]:
            audit_position(
                task_context, run_id, index,
                worker="api-fallback", request_id=request_id,
            )
        finalize_run(
            task_context, run_id, worker="api-fallback", request_id=request_id
        )
    except Exception:
        logger.exception("audit %s failed", run_id)
        try:
            context.repository.finish_run(run_id, "failed")
        except Exception:
            logger.exception("could not mark audit %s failed", run_id)
        raise
    finally:
        reset(token)


def run_audit_job(
    context: ApiContext,
    run_id: UUID,
    *,
    project: str,
    vor_filename: str,
    vor_bytes: bytes,
    pdf_files: dict[str, bytes],
    pdf_versions: dict[str, DocumentVersionRow],
    request_id: str = "",
    log_context: dict[str, Any] | None = None,
) -> None:
    """Execute one stored ``runs`` row headless; failures mark it ``failed``.

    Issue 19: the in-process ``run_audit`` path is replaced by the same
    ``audit_tasks`` functions Celery workers call (prepare → audit →
    finalize), so fallback results match the queued path byte-for-shape.
    """
    run_tasks_job(
        context,
        run_id,
        project=project,
        vor_filename=vor_filename,
        vor_bytes=bytes(vor_bytes),
        pdf_files=dict(pdf_files),
        pdf_versions=dict(pdf_versions),
        request_id=request_id,
        log_context=log_context,
    )

def create_app(context: ApiContext | None = None) -> FastAPI:
    """Build the API; callers inject a test context or load settings from env."""
    resolved = context
    if resolved is None:
        settings = AuditSettings.load()
        if not settings.database_url or not settings.blob_store_url:
            raise RuntimeError("DATABASE_URL and BLOB_STORE_URL are required")
        blobs = open_blob_store(settings.blob_store_url)
        resolved = ApiContext(
            settings=settings,
            repository=open_repository(settings.database_url, blob_store=blobs),
            blob_store=blobs,
        )
    runner = AuditRunner(resolved)
    app = FastAPI(title="smeta-checker API", version="1.0.0")
    init_sentry("api")
    app.state.context = resolved
    app.state.runner = runner

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        import time as _time
        started = _time.monotonic()
        request_id = _request_id(request)
        request.state.request_id = request_id
        token = bind(request_id=request_id, stage="http")
        status_code = 500
        response = None
        try:
            try:
                import sentry_sdk
                sentry_sdk.set_tag("request_id", request_id)
            except Exception:
                pass
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            reset(token)
            duration = _time.monotonic() - started
            route = request.scope.get("route")
            path = getattr(route, "path", None) or request.url.path
            HTTP_REQUESTS_TOTAL.labels(request.method, path, str(status_code)).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(request.method, path).observe(duration)

    def get_context() -> ApiContext:
        return app.state.context

    def get_runner() -> AuditRunner:
        return app.state.runner

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        response = JSONResponse(status_code=exc.status, content=_error_payload(exc))
        response.headers[REQUEST_ID_HEADER] = _request_id(request)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        message = detail if isinstance(detail, str) else "Ошибка запроса."
        code = "not_found" if exc.status_code == 404 else "http_error"
        response = JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message}},
        )
        response.headers[REQUEST_ID_HEADER] = _request_id(request)
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        response = JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Некорректный запрос.",
                    # Validator exception objects and NaN input values are
                    # not JSON-safe. Return the public validation fields only.
                    "details": [{key: error[key] for key in ("loc", "msg", "type")}
                                for error in exc.errors()],
                }
            },
        )
        response.headers[REQUEST_ID_HEADER] = _request_id(request)
        return response

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        logger.exception("unhandled API error", exc_info=exc)
        response = JSONResponse(
            status_code=500,
            content={"error": {"code": "internal", "message": "Внутренняя ошибка."}},
        )
        response.headers[REQUEST_ID_HEADER] = _request_id(request)
        return response
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> Response:
        data, content_type = render_metrics()
        return Response(content=data, media_type=content_type)

    @app.post("/api/v1/documents", response_model=DocumentUploadResponse)
    async def upload_document(
        request: Request,
        kind: str = Form(pattern="^(vor|pdf)$"),
        file: UploadFile = File(...),
        project_id: str = Form(...),
        document_id: str | None = Form(None),
        new_document: bool = Form(False),
        context: ApiContext = Depends(get_context),
    ) -> DocumentUploadResponse:
        _require_api_token(context, request)
        _require_configured(context)
        data = await file.read(context.settings.max_upload_bytes + 1)
        owner_id = _parse_uuid(project_id, field="project_id")
        try:
            context.repository.load_project(owner_id)
        except KeyError as exc:
            raise ApiError(404, "project_not_found", "Проект не найден") from exc
        target_id = _parse_uuid(document_id, field="document_id") if document_id else None
        response = _upload_document(context, owner_id, kind, file, data,
                                    target_id, new_document)
        logger.info(
            "document uploaded",
            extra={
                "stage": "upload",
                "sha256": response.sha256,
                "size": response.size,
                "kind": kind,
            },
        )
        return response
    @app.post("/api/v1/documents/preflight", response_model=PreflightResponse)
    def preflight_documents(
        request: Request,
        payload: AuditCreateRequest,
        context: ApiContext = Depends(get_context),
    ) -> PreflightResponse:
        _require_api_token(context, request)
        _require_configured(context)
        vor_id = _parse_uuid(payload.vor_version_id, field="vor_version_id")
        owner = _project_for_payload(context, payload)
        pdf_ids = _resolve_pdf_pool(context, owner.id, [
            _parse_uuid(item, field="pdf_version_ids")
            for item in payload.pdf_version_ids
        ])
        if not payload.pdf_version_ids:
            pdf_ids = _include_bound_versions(context, vor_id, pdf_ids)
        _check_project_versions(context, owner.id, [vor_id, *pdf_ids])
        _check_active_versions(context, owner.id, [vor_id, *pdf_ids])
        _check_pdf_names(context, pdf_ids)
        return _preflight_for_versions(context, vor_id, pdf_ids)

    @app.post("/api/v1/audits", response_model=AuditCreateResponse, status_code=202)
    def create_audit(
        request: Request,
        payload: AuditCreateRequest,
        context: ApiContext = Depends(get_context),
        audit_runner: AuditRunner = Depends(get_runner),
    ) -> AuditCreateResponse:
        from vor_parse import load_vor_positions

        request_id = _request_id(request)
        _require_api_token(context, request)
        _require_configured(context)
        _require_llm_credentials(context.settings)
        stored_project = _project_for_payload(context, payload)
        project = stored_project.name
        vor_id = _parse_uuid(payload.vor_version_id, field="vor_version_id")
        pdf_ids = _resolve_pdf_pool(context, stored_project.id, [
            _parse_uuid(item, field="pdf_version_ids")
            for item in payload.pdf_version_ids
        ])
        if not payload.pdf_version_ids:
            pdf_ids = _include_bound_versions(context, vor_id, pdf_ids)
        _check_project_versions(context, stored_project.id, [vor_id, *pdf_ids])
        _check_pdf_names(context, pdf_ids)
        _check_active_versions(context, stored_project.id, [vor_id, *pdf_ids])
        allowed = max(1, int(context.settings.max_uploads_per_request or 1))
        if len(pdf_ids) + 1 > allowed:
            raise ApiError(
                400,
                "too_many_uploads",
                f"Больше {allowed} документов на один аудит.",
            )
        vor_bytes, _, vor_filename, vor_kind = _read_version_bytes(context, vor_id)
        if vor_kind != "vor":
            raise ApiError(400, "not_vor_version", "Указанная версия — не ВОР.")
        pdf_files: dict[str, bytes] = {}
        version_keys = _pdf_pool_keys(context, pdf_ids)
        pdf_versions: dict[str, DocumentVersionRow] = {}
        for pdf_id in pdf_ids:
            data, row, filename, kind = _read_version_bytes(context, pdf_id)
            if kind != "pdf":
                raise ApiError(400, "not_pdf_version", "Указанная версия — не PDF.")
            key = version_keys[pdf_id]
            if not key:
                raise ApiError(400, "filename_required", "Имя PDF обязательно.")
            if key in pdf_files:
                raise ApiError(400, "duplicate_pdf_name",
                               "Выберите одну версию каждого PDF; имена PDF должны различаться")
            pdf_files[key] = bytes(data)
            pdf_versions[key] = row
        bindings = _pdf_binding_keys(context, vor_id, pdf_ids)
        preflight = _preflight_for_versions(context, vor_id, pdf_ids, bindings)
        positions, _ = load_vor_positions(vor_bytes, vor_filename)
        llm = context.settings.llm_credentials()
        config = {
            "project": project,
            "model": llm.model_name,
            "provider": llm.provider,
            "pdf_versions": {key: str(row.id) for key, row in pdf_versions.items()},
            "pdf_bindings": bindings,
            "vor_filename": vor_filename,
            "preflight": [row.model_dump() for row in preflight.rows],
            "has_critical_error": preflight.has_critical_error,
            "request_id": request_id,
            "position_inputs": [p.model_dump(mode="json") for p in positions],
        }
        dispatch = _celery_dispatch(context)
        try:
            run = context.repository.create_run(
                stored_project.id, vor_id, config,
                pdf_pins={key: row.id for key, row in pdf_versions.items()},
                background=dispatch is not None,
            )
        except KeyError as exc:
            raise ApiError(404, "version_not_found", "Версия не найдена") from exc
        except ValueError as exc:
            raise ApiError(400, "run_invalid", str(exc)) from exc
        except Exception as exc:
            raise ApiError(
                500, "storage_error", f"Не удалось создать запуск: {exc}"
            ) from exc

        if dispatch is not None:
            try:
                from audit_delivery import publish_pending
                publish_pending(context.repository, dispatch)
            except Exception:
                # The run and its first task are committed together. The
                # dispatcher retries delivery when the broker becomes available.
                logger.warning("initial delivery deferred", exc_info=True)
            logger.info(
                "audit dispatched",
                extra={"audit_run_id": str(run.id), "stage": "dispatch"},
            )
            return AuditCreateResponse(run_id=str(run.id), status="running")

        log_fields = snapshot()
        log_fields.update({"audit_run_id": str(run.id), "stage": "fallback"})

        def _target() -> None:
            if context.client_factory is None:
                from scripts.audit_worker import run_isolated

                run_isolated(context.settings, run.id)
                return
            run_audit_job(
                context,
                run.id,
                project=project,
                vor_filename=vor_filename,
                vor_bytes=bytes(vor_bytes),
                pdf_files=dict(pdf_files),
                pdf_versions=dict(pdf_versions),
                request_id=request_id,
                log_context=log_fields,
            )

        audit_runner.start(run.id, target=_target)
        logger.info(
            "audit started",
            extra={"audit_run_id": str(run.id), "stage": "dispatch"},
        )
        return AuditCreateResponse(run_id=str(run.id), status="running")

    @app.get("/api/v1/audits/{run_id}", response_model=AuditStatusResponse)
    def get_audit(
        request: Request, run_id: str, context: ApiContext = Depends(get_context)
    ) -> AuditStatusResponse:
        _require_api_token(context, request)
        _require_configured(context)
        record = _load_run_record(context, _parse_uuid(run_id, field="run_id"))
        return _run_record(record)

    @app.get("/api/v1/audits/{run_id}/results", response_model=AuditResultsResponse)
    def get_audit_results(
        request: Request, run_id: str, context: ApiContext = Depends(get_context)
    ) -> AuditResultsResponse:
        _require_api_token(context, request)
        _require_configured(context)
        record = _load_run_record(context, _parse_uuid(run_id, field="run_id"))
        if str(record.get("status")) not in POSITION_DONE_STATUSES:
            raise ApiError(409, "audit_not_finished", "Аудит ещё выполняется.")
        return AuditResultsResponse(
            run_id=str(record["id"]),
            status=str(record.get("status", "")),
            vor_filename=str(record.get("vor_filename", "")),
            results=[dict(item) for item in (record.get("results") or [])],
        )

    @app.get("/api/v1/audits/{run_id}/reports/{kind}")
    def download_audit_report(
        request: Request, run_id: str, kind: str, context: ApiContext = Depends(get_context)
    ) -> Response:
        _require_api_token(context, request)
        _require_configured(context)
        if kind != "word":
            raise ApiError(400, "unknown_report", "Отчёт бывает только word.")
        record = _load_run_record(context, _parse_uuid(run_id, field="run_id"))
        if str(record.get("status")) not in POSITION_DONE_STATUSES:
            raise ApiError(409, "audit_not_finished", "Аудит ещё выполняется.")
        try:
            from workspace_service import validated_word_report
            payload = validated_word_report(context, record)
            if payload is None:
                payload = context.repository.load_run_report(record["id"], kind)
        except ValueError as exc:
            raise ApiError(409, "normative_not_finished", str(exc)) from exc
        except KeyError as exc:
            raise ApiError(404, "report_not_found", "Отчёт не найден.") from exc
        except FileNotFoundError as exc:
            raise ApiError(
                500, "blob_missing", "Байты отчёта отсутствуют в хранилище."
            ) from exc
        return Response(
            content=bytes(payload),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename=audit-{record['id']}.docx"
            },
        )

    from workspace_api import workspace_router
    app.include_router(workspace_router(get_context))
    return app


def get_app() -> FastAPI:
    """Uvicorn entry point: ``uvicorn api:get_app --factory``."""
    return create_app()


try:
    app = create_app()
except RuntimeError:
    # Import stays cheap without storage env: every endpoint returns 503
    # until DATABASE_URL/BLOB_STORE_URL are configured.
    settings = AuditSettings.load()

    class _UnconfiguredRepository:
        def __getattr__(self, name: str) -> Any:
            raise ApiError(
                503,
                "storage_not_configured",
                "Хранилище не настроено: заполните DATABASE_URL и BLOB_STORE_URL.",
            )

    class _UnconfiguredBlobs:
        def get(self, key: str) -> bytes:
            raise ApiError(
                503,
                "storage_not_configured",
                "Хранилище не настроено: заполните DATABASE_URL и BLOB_STORE_URL.",
            )

    app = create_app(
        ApiContext(
            settings=settings,
            repository=_UnconfiguredRepository(),  # type: ignore[arg-type]
            blob_store=_UnconfiguredBlobs(),  # type: ignore[arg-type]
        )
    )
