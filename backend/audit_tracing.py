"""Optional OpenTelemetry traces for Phoenix: one position per trace.

The public surface mirrors the previous tracer so callers and tests can switch
backends without rewriting audit code.  When no collector endpoint is configured
the tracer is disabled and every observation becomes a no-op, exactly like the
previous optional Langfuse tracer.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

_logger = logging.getLogger(__name__)
_client = contextvars.ContextVar("audit_otel_client", default=None)
_position: contextvars.ContextVar["PositionTrace | None"] = contextvars.ContextVar(
    "audit_otel_position", default=None
)
_redact = contextvars.ContextVar("audit_otel_redact_pdf", default=False)
_mask = contextvars.ContextVar("audit_otel_mask_prompts", default=False)

_redact_pdf_names: frozenset[str] = frozenset({
    "select-context", "locate-work", "confirm-quantity-context",
    "confirm-quantities", "generate-audit", "validate-audit-response",
    "read-ocr-page",
})
_context_map_stages: frozenset[str] = frozenset({
    "select-context", "locate-work", "confirm-quantity-context",
})
_fragment_keys: frozenset[str] = frozenset({
    "context", "response", "text", "evidence", "discrepancy_details", "quote",
})

_PROVIDERS: dict[str, TracerProvider] = {}


def _redacted_context(context):
    if isinstance(context, str):
        return f"[redacted {len(context)} chars]"
    if isinstance(context, dict):
        redacted = {}
        for key, value in context.items():
            if isinstance(value, str):
                redacted[key] = f"[redacted {len(value)} chars]"
            else:
                redacted[key] = _redacted_context(value)
        return redacted
    if isinstance(context, list):
        return [_redacted_context(item) for item in context]
    return context


def _maybe_redact(name, kind, payload):
    redact_pdf = _redact.get()
    mask_prompts = _mask.get()
    if not redact_pdf and not mask_prompts:
        return payload
    if name in {"generate-audit", "normative-query-plan", "normative-rank", "normative-position-check"} and isinstance(payload, list):
        redacted_messages = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("content"), str) and (mask_prompts or redact_pdf):
                marker = "[redacted prompt]" if mask_prompts else f"[redacted {len(item['content'])} chars]"
                item = {**item, "content": marker}
            redacted_messages.append(item)
        return redacted_messages
    if isinstance(payload, str):
        if redact_pdf and name in _redact_pdf_names:
            return f"[redacted {len(payload)} chars]"
        return payload
    if isinstance(payload, dict) and kind in {"input", "output"}:
        whole_map = redact_pdf and name in _context_map_stages
        redacted = {}
        for key, value in payload.items():
            if whole_map and key not in {"pages", "per_page_chars", "context_chars"}:
                redacted[key] = _redacted_context(value)
            elif redact_pdf and key in _fragment_keys:
                redacted[key] = _redacted_context(value)
            else:
                redacted[key] = value
        return redacted
    if redact_pdf and isinstance(payload, (dict, list)):
        return _redacted_context(payload)
    return payload


STAGE_DESCRIPTIONS = {
    "audit-vor-position": "Позиция ВОР: вход — строка ведомости, выход — итог после сверки ссылки",
    "prepare-position": "Привязка PDF и подготовка текстового снимка для позиции",
    "parse-page-reference": "Разбор ссылки ВОР в порядковые страницы PDF (счёт с 1)",
    "index-native-pdf": "Однократный разбор PDF через PyMuPDF4LLM: текст и HTML-таблицы",
    "prepare-ocr-snapshot": "Точечный OCR сканов для позиции; неизменяемый снимок текстов",
    "read-ocr-page": "OCR одной страницы: кеш, параметры, текст или ошибка",
    "audit-position": "Проверка позиции в worker-потоке: поиск работы и подтверждение",
    "rank-candidate-pages": "Ранжирование страниц-кандидатов по словам работы и объёму",
    "select-context": "Нарезка фрагментов ≤4 стр./10000 симв. для одного запроса",
    "locate-work": "Поиск работы по наименованию; объём/единица не влияют",
    "confirm-quantities": "Один ограниченный добор объёма/единицы по найденной работе",
    "rank-quantity-pages": "Ранжирование страниц поддержки объёма/единицы",
    "confirm-quantity-context": "Единственный подтверждающий запрос количества",
    "check-request-budget": "Проверка лимитов ≤6 запросов/60000 токенов до HTTP",
    "generate-audit": "Один HTTP-запрос к выбранной LLM: контекст, сырой ответ, usage",
    "normative-query-plan": "Построение поисковых запросов по контексту ВОР и ПД",
    "normative-rank": "Сопоставление контекста с найденными нормативными вариантами",
    "normative-position-check": "Одна позиция: ПД, связанные строки ВОР и нормативные источники",
    "validate-audit-response": "Guardrail: JSON и дословность цитат по фрагментам",
    "wait-before-retry": "Пауза перед ретраем: причина и тип ошибки",
}


def _describe(name, kwargs):
    description = STAGE_DESCRIPTIONS.get(name)
    if description is None:
        return kwargs
    metadata = kwargs.get("metadata")
    if metadata is None:
        kwargs["metadata"] = {"description_ru": description}
    elif isinstance(metadata, dict):
        kwargs["metadata"] = {"description_ru": description, **metadata}
    return kwargs


def _to_attr(value):
    if isinstance(value, (str, bool, int, float)):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _kwargs_to_attrs(name, kwargs):
    kwargs = _describe(name, dict(kwargs))
    attrs = {}
    for key in ("input", "output"):
        if key in kwargs:
            value = kwargs.pop(key)
            if _redact.get() or _mask.get():
                value = _maybe_redact(name, key, value)
            attrs[key] = _to_attr(value)
    metadata = kwargs.pop("metadata", None)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            attrs[f"metadata.{key}"] = _to_attr(value)
    for key, value in kwargs.items():
        attrs[key] = _to_attr(value)
    return attrs


class Observation:
    def __init__(self, span=None, name=""):
        self.span = span
        self.name = name

    def update(self, **kwargs):
        if self.span is None:
            return
        try:
            if hasattr(self.span, "update") and callable(self.span.update):
                redacted = {}
                for key, value in kwargs.items():
                    if key in ("input", "output"):
                        value = _maybe_redact(self.name, key, value)
                    redacted[key] = value
                self.span.update(**redacted)
                return
            attrs = _kwargs_to_attrs(self.name, kwargs)
            for key, value in attrs.items():
                self.span.set_attribute(key, value)
        except Exception:
            _logger.warning("Phoenix observation update failed", exc_info=True)


class _EnabledClient:
    """Sentinel object used where callers expect a non-None tracing client."""


def _ensure_provider(endpoint, project, environment):
    key = endpoint.rstrip("/")
    if key in _PROVIDERS:
        return _PROVIDERS[key]
    resource = Resource.create({
        "service.name": project or "smeta-checker",
        "deployment.environment": environment or "production",
    })
    provider = TracerProvider(resource=resource)
    try:
        exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception as exc:
        _logger.warning("Phoenix exporter init failed: %s", type(exc).__name__, exc_info=True)
    _PROVIDERS[key] = provider
    trace.set_tracer_provider(provider)
    return provider


@contextmanager
def span(name, *, client=None, trace_context=None, **kwargs):
    """Instrumentation failures must not swallow or replace audit failures."""
    kwargs = _describe(name, kwargs)
    redact_token = _redact.set(_redact.get())
    mask_token = _mask.set(_mask.get())
    client = client if client is not None else _client.get()
    manager = None
    observation = Observation()
    call_kwargs = dict(kwargs)
    for key in ("input", "output"):
        if key in call_kwargs:
            call_kwargs[key] = _maybe_redact(name, key, call_kwargs[key])
    if client is not None:
        try:
            manager = client.start_as_current_observation(name=name, trace_context=trace_context, **call_kwargs)
            observation = Observation(manager.__enter__(), name)
        except Exception:
            manager = None
            _logger.warning("Phoenix span start failed", exc_info=True)
    else:
        attrs = _kwargs_to_attrs(name, dict(kwargs))
        otel_manager = None
        try:
            tracer = trace.get_tracer("smeta-checker")
            ctx = None
            if isinstance(trace_context, trace.Span):
                ctx = trace.set_span_in_context(trace_context)
            elif trace_context is not None and hasattr(trace_context, "trace_id"):
                from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
                sc = SpanContext(
                    trace_id=trace_context.trace_id_int if hasattr(trace_context, "trace_id_int") else int(str(trace_context.trace_id), 16),
                    span_id=getattr(trace_context, "span_id_int", 0),
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
                ctx = trace.set_span_in_context(NonRecordingSpan(sc))
            if ctx is None:
                otel_manager = tracer.start_as_current_span(name, attributes=attrs)
            else:
                otel_manager = tracer.start_as_current_span(name, context=ctx, attributes=attrs)
            manager = otel_manager
            observation = Observation(otel_manager.__enter__(), name)
        except Exception:
            manager = None
            _logger.warning("Phoenix span start failed", exc_info=True)
    try:
        yield observation
    except BaseException:
        if manager is not None:
            try:
                manager.__exit__(*sys.exc_info())
            except Exception:
                _logger.warning("Phoenix span close failed", exc_info=True)
        raise
    else:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                _logger.warning("Phoenix span close failed", exc_info=True)
    finally:
        _mask.reset(mask_token)
        _redact.reset(redact_token)


class TracingRun:
    def __init__(self, endpoint="", project="", environment="production",
                 audit_run_id="", request_id="", redact_pdf=False, mask_prompts=False,
                 public_url="", flush_on_close=True):
        self.audit_run_id = str(audit_run_id or "")
        self.request_id = str(request_id or "")
        self.session_id = (
            f"audit-{self.audit_run_id}" if self.audit_run_id else "audit-" + __import__("uuid").uuid4().hex
        )
        self.redact_pdf = bool(redact_pdf)
        self.mask_prompts = bool(mask_prompts)
        self.flush_on_close = bool(flush_on_close)
        self.environment = environment or "production"
        self.endpoint = (endpoint or os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "")).strip()
        self.project = (project or os.environ.get("PHOENIX_PROJECT_NAME", "smeta-checker")).strip()
        self.public_url = (public_url or os.environ.get("PHOENIX_PUBLIC_URL", "") or self.endpoint).strip()
        self.client = None
        self.error = ""
        self.positions = []
        if self.endpoint:
            try:
                _ensure_provider(self.endpoint, self.project, self.environment)
            except Exception as exc:
                self.error = f"Phoenix initialization failed: {type(exc).__name__}"
                _logger.warning(self.error, exc_info=True)
        elif endpoint or project:
            self.error = "Для Phoenix нужен endpoint коллектора."

    def position(self, input, metadata):
        position = PositionTrace(self, input, metadata)
        self.positions.append(position)
        return position

    def close(self):
        for position in self.positions:
            if not position.finished:
                position.finish({"status": "Технический сбой",
                                 "discrepancy_details": "Прогон прерван до завершения позиции"})
        # Long-lived workers rely on the provider background exporter.
        if not self.flush_on_close:
            return
        try:
            provider = _PROVIDERS.get(self.endpoint.rstrip("/"))
            if provider is not None:
                provider.force_flush()
        except Exception:
            _logger.warning("Phoenix flush failed", exc_info=True)


class PositionTrace:
    def __init__(self, run, input, metadata):
        self.run = run
        self.root = None
        self.trace_id = None
        self.context = None
        self.finished = False
        if run.endpoint:
            try:
                tracer = trace.get_tracer("smeta-checker")
                attrs = _kwargs_to_attrs("audit-vor-position", {"input": input})
                if isinstance(metadata, dict):
                    enriched = {"description_ru": STAGE_DESCRIPTIONS["audit-vor-position"]}
                    if run.audit_run_id:
                        enriched["audit_run_id"] = run.audit_run_id
                    if run.request_id:
                        enriched["request_id"] = run.request_id
                    metadata = {**enriched, **metadata}
                    for key, value in metadata.items():
                        attrs[f"metadata.{key}"] = _to_attr(value)
                attrs["session.id"] = run.session_id
                attrs["audit_run_id"] = run.audit_run_id
                if run.request_id:
                    attrs["request_id"] = run.request_id
                self.root = tracer.start_span("audit-vor-position", attributes=attrs)
                sc = self.root.get_span_context()
                self.trace_id = format(sc.trace_id, "032x")
                self.context = trace.set_span_in_context(self.root)
            except Exception:
                self.trace_id = None
                _logger.warning("Phoenix position start failed", exc_info=True)

    @contextmanager
    def stage(self, name, **kwargs):
        token = _client.set(None)
        position_token = _position.set(self)
        redact_token = _redact.set(bool(self.run.redact_pdf))
        mask_token = _mask.set(bool(self.run.mask_prompts))
        try:
            if self.root is None:
                yield Observation()
            else:
                tracer = trace.get_tracer("smeta-checker")
                attrs = _kwargs_to_attrs(name, kwargs)
                attrs.setdefault("session.id", self.run.session_id)
                with trace.use_span(self.root, end_on_exit=False):
                    with tracer.start_as_current_span(name, attributes=attrs) as otel_span:
                        obs = Observation(otel_span, name)
                        yield obs
        finally:
            _mask.reset(mask_token)
            _redact.reset(redact_token)
            _position.reset(position_token)
            _client.reset(token)

    def url(self):
        if self.trace_id is not None:
            base = os.environ.get("PHOENIX_PUBLIC_URL", "").strip() or self.run.public_url
            base = (base or self.run.endpoint or "").rstrip("/")
            return f"{base}/traces/{self.trace_id}"
        return None

    def finish(self, result):
        if self.finished:
            return
        self.finished = True
        if self.root is not None:
            try:
                status = result.get("status", result.get("Статус аудита"))
                level = "ERROR" if status == "Технический сбой" else ("WARNING" if status == "Неполная проверка" else "DEFAULT")
                self.root.set_attribute("status", str(status))
                self.root.set_attribute("level", level)
                self.root.set_attribute("output", _to_attr(result))
                self.root.set_status(trace.Status(trace.StatusCode.ERROR if level == "ERROR" else trace.StatusCode.OK))
                self.root.end()
            except Exception:
                _logger.warning("Phoenix position end failed", exc_info=True)



def token_usage(usage):
    result = {}
    for target, names in (("input", ("input_tokens", "prompt_tokens")),
                          ("output", ("output_tokens", "completion_tokens")),
                          ("total", ("total_tokens",))):
        for name in names:
            value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[target] = value
                break
    details = (usage.get("completion_tokens_details") if isinstance(usage, dict)
               else getattr(usage, "completion_tokens_details", None))
    reasoning = (details.get("reasoning_tokens") if isinstance(details, dict)
                 else getattr(details, "reasoning_tokens", None))
    if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
        result["reasoning"] = reasoning
    return result

def _configured_client(*args, **kwargs):
    return None
