"""Optional Langfuse traces: one position per trace, one audit run per session.

Only explicit payloads are captured; API clients, credentials and PDF objects
never become observation inputs. Context is reattached explicitly in workers.
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

STAGE_DESCRIPTIONS = {
    "audit-vor-position": "Позиция ВОР: вход — строка ведомости, выход — итог после сверки ссылки",
    "prepare-position": "Привязка PDF и подготовка текстового снимка для позиции",
    "parse-page-reference": "Разбор ссылки ВОР в порядковые страницы PDF (счёт с 1)",
    "index-native-pdf": "Однократное чтение нативного текста PDF (спан первой позиции)",
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
    "generate-audit": "Один HTTP-запрос к Qwen: контекст, сырой ответ, usage",
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
    def __init__(self, observation=None):
        self.observation = observation

    def update(self, **kwargs):
        if self.observation is not None:
            try:
                self.observation.update(**kwargs)
            except Exception:
                _logger.warning("Langfuse observation update failed", exc_info=True)


@contextmanager
def span(name, *, client=None, trace_context=None, **kwargs):
    """Instrumentation failures must not swallow or replace audit failures."""
    kwargs = _describe(name, kwargs)
    client = client if client is not None else _client.get()
    manager = None
    observation = Observation()
    if client is not None:
        try:
            manager = client.start_as_current_observation(
                name=name, trace_context=trace_context, **kwargs)
            observation = Observation(manager.__enter__())
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
                 environment="development"):
        self.session_id = "audit-" + uuid4().hex
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
                with propagate_attributes(session_id=run.session_id, tags=["vor-audit"],
                                          trace_name="audit-vor-position"):
                    if isinstance(metadata, dict):
                        metadata = {"description_ru": STAGE_DESCRIPTIONS["audit-vor-position"], **metadata}
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
        try:
            if self.root is None:
                yield Observation()
            else:
                from langfuse import propagate_attributes
                with propagate_attributes(session_id=self.run.session_id, tags=["vor-audit"]):
                    with span(name, trace_context=context, **kwargs) as observation:
                        yield observation
        finally:
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
            value = getattr(usage, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[target] = value
                break
    return result
