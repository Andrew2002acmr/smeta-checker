"""Optional Langfuse traces: one position per trace, one audit run per session.

Only explicit payloads are captured; API clients, credentials and PDF objects
never become observation inputs. Context is reattached explicitly in workers.

Every position trace carries the durable correlation keys ``audit_run_id`` and
``position_index``: an HTTP request (``X-Request-ID``) resolves to a stored
``runs`` row, the row resolves to Job payloads, and each payload rebuilds the
same keys in the worker. ``redact_pdf`` additionally replaces PDF fragments
with page/quote identifiers before anything reaches Langfuse.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import logging
import sys
from uuid import uuid4


_logger = logging.getLogger(__name__)
_client = ContextVar("audit_langfuse_client", default=None)
_position = ContextVar("audit_langfuse_position", default=None)
_redact = ContextVar("audit_langfuse_redact_pdf", default=False)
_mask = ContextVar("audit_langfuse_mask_prompts", default=False)
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


def _redacted_context(context):
    """Replace fragment bodies with stable identifiers; keeps routing keys."""
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
    """Apply PDF redaction and prompt masking independently.

    ``redact_pdf`` replaces fragment bodies with ``[redacted N chars]``
    markers: whole ``{page: fragment}`` maps on context stages, message
    ``content`` on ``generate-audit``, and ``context``/``response``/``text``/
    ``evidence``/``discrepancy_details``/``quote`` keys elsewhere. Routing
    keys (pages, counts, flags, statuses) survive. ``mask_prompts`` replaces
    only ``generate-audit`` message contents with ``[redacted prompt]``,
    keeping metadata (provider, attempt, pages, chars) for cost debugging.
    """
    redact_pdf = _redact.get()
    mask_prompts = _mask.get()
    if not redact_pdf and not mask_prompts:
        return payload
    if name in {"generate-audit", "normative-query-plan", "normative-rank", "normative-position-check"} and isinstance(payload, list):
        redacted_messages = []
        for item in payload:
            if (
                isinstance(item, dict)
                and isinstance(item.get("content"), str)
                and (mask_prompts or redact_pdf)
            ):
                marker = (
                    "[redacted prompt]"
                    if mask_prompts
                    else f"[redacted {len(item['content'])} chars]"
                )
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
    """Default Russian stage caption; explicit metadata always wins."""
    description = STAGE_DESCRIPTIONS.get(name)
    if description is None:
        return kwargs
    metadata = kwargs.get("metadata")
    if metadata is None:
        kwargs["metadata"] = {"description_ru": description}
    elif isinstance(metadata, dict):
        kwargs["metadata"] = {"description_ru": description, **metadata}
    return kwargs


class Observation:
    def __init__(self, observation=None, name=""):
        self.observation = observation
        self.name = name

    def update(self, **kwargs):
        if self.observation is not None:
            try:
                if _redact.get() or _mask.get():
                    for key in ("input", "output"):
                        if key in kwargs:
                            kwargs[key] = _maybe_redact(self.name, key, kwargs[key])
                self.observation.update(**kwargs)
            except Exception:
                _logger.warning("Langfuse observation update failed", exc_info=True)


@contextmanager
def span(name, *, client=None, trace_context=None, **kwargs):
    """Instrumentation failures must not swallow or replace audit failures."""
    kwargs = _describe(name, kwargs)
    if _redact.get() or _mask.get():
        for key in ("input", "output"):
            if key in kwargs:
                kwargs[key] = _maybe_redact(name, key, kwargs[key])
    client = client if client is not None else _client.get()
    manager = None
    observation = Observation(name=name)
    if client is not None:
        try:
            manager = client.start_as_current_observation(
                name=name, trace_context=trace_context, **kwargs)
            observation = Observation(manager.__enter__(), name=name)
        except Exception:
            manager = None
            _logger.warning("Langfuse observation start failed", exc_info=True)
    try:
        yield observation
    except BaseException:
        if manager is not None:
            try:
                manager.__exit__(*sys.exc_info())
            except Exception:
                _logger.warning("Langfuse observation close failed", exc_info=True)
        raise
    else:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                _logger.warning("Langfuse observation close failed", exc_info=True)

@lru_cache(maxsize=4)
def _configured_client(public_key, secret_key, base_url, environment):
    from langfuse import Langfuse
    return Langfuse(public_key=public_key, secret_key=secret_key,
                    base_url=base_url, timeout=10, environment=environment or "development")


class TracingRun:
    def __init__(self, public_key="", secret_key="", base_url="https://cloud.langfuse.com",
                 environment="development", audit_run_id="", request_id="",
                 redact_pdf=False, mask_prompts=False):
        self.audit_run_id = str(audit_run_id or "")
        self.request_id = str(request_id or "")
        # One audit run = one Langfuse session across all workers. A random
        # id per TracingRun fragments the session (prepare + every position
        # land in different sessions) and durations collapse to ~0.
        self.session_id = (
            f"audit-{self.audit_run_id}" if self.audit_run_id
            else "audit-" + uuid4().hex
        )
        self.redact_pdf = bool(redact_pdf)
        self.mask_prompts = bool(mask_prompts)
        self.client = None
        self.error = ""
        self.positions = []
        self.environment = environment or "development"
        if public_key and secret_key:
            try:
                self.client = _configured_client(public_key, secret_key, base_url, self.environment)
            except Exception as exc:
                self.error = f"Langfuse initialization failed: {type(exc).__name__}"
                _logger.warning(self.error, exc_info=True)
        elif public_key or secret_key:
            self.error = "Для Langfuse нужны оба ключа: public и secret."

    def position(self, input, metadata):
        position = PositionTrace(self, input, metadata)
        self.positions.append(position)
        return position

    def close(self):
        for position in self.positions:
            if not position.finished:
                position.finish({"status": "Технический сбой",
                                 "discrepancy_details": "Прогон прерван до завершения позиции"})
        if self.client is not None:
            try:
                self.client.flush()
            except Exception:
                _logger.warning("Langfuse flush failed", exc_info=True)


class PositionTrace:
    def __init__(self, run, input, metadata):
        self.run = run
        self.root = None
        self.trace_id = None
        self.context = None
        self.finished = False
        if run.client is not None:
            try:
                from langfuse import propagate_attributes
                # The root owns its trace: no remote trace_context, or the SDK
                # invents a phantom parent span id for it.
                tags = ["vor-audit"]
                if run.audit_run_id:
                    tags.append(f"audit-run-{run.audit_run_id}")
                with propagate_attributes(session_id=run.session_id, tags=tags,
                                          trace_name="audit-vor-position",
                                          user_id=run.audit_run_id or None):
                    if isinstance(metadata, dict):
                        enriched = {"description_ru": STAGE_DESCRIPTIONS["audit-vor-position"]}
                        if run.audit_run_id:
                            enriched["audit_run_id"] = run.audit_run_id
                        if run.request_id:
                            enriched["request_id"] = run.request_id
                        metadata = {**enriched, **metadata}
                    self.root = run.client.start_observation(
                        name="audit-vor-position", input=input, metadata=metadata)
                self.trace_id = self.root.trace_id
                self.context = {"trace_id": self.trace_id, "parent_span_id": self.root.id}
            except Exception:
                self.trace_id = None
                _logger.warning("Langfuse position start failed", exc_info=True)

    @contextmanager
    def stage(self, name, **kwargs):
        token = _client.set(self.run.client if self.root is not None else None)
        context = None if _position.get() is self else self.context
        position_token = _position.set(self)
        redact_token = _redact.set(bool(self.run.redact_pdf))
        mask_token = _mask.set(bool(self.run.mask_prompts))
        try:
            if self.root is None:
                yield Observation()
            else:
                from langfuse import propagate_attributes
                tags = ["vor-audit"]
                if self.run.audit_run_id:
                    tags.append(f"audit-run-{self.run.audit_run_id}")
                with propagate_attributes(session_id=self.run.session_id, tags=tags):
                    with span(name, trace_context=context, **kwargs) as observation:
                        yield observation
        finally:
            _mask.reset(mask_token)
            _redact.reset(redact_token)
            _position.reset(position_token)
            _client.reset(token)

    def finish(self, result):
        if self.finished:
            return
        self.finished = True
        if self.root is not None:
            status = result.get("status", result.get("Статус аудита"))
            Observation(self.root).update(
                output=result, level="ERROR" if status == "Технический сбой" else (
                    "WARNING" if status == "Неполная проверка" else "DEFAULT"),
                status_message=status)
            try:
                self.root.end()
            except Exception:
                _logger.warning("Langfuse position end failed", exc_info=True)

    def url(self):
        if self.trace_id is not None:
            try:
                return self.run.client.get_trace_url(trace_id=self.trace_id)
            except Exception:
                _logger.warning("Langfuse trace link unavailable", exc_info=True)
        return None


def token_usage(usage):
    """Normalize Responses and Chat Completions usage without estimating billing."""
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
