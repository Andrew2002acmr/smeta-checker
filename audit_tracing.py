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
def _configured_client(public_key, secret_key, base_url):
    from langfuse import Langfuse
    return Langfuse(public_key=public_key, secret_key=secret_key,
                    base_url=base_url, timeout=10, environment="development")


class TracingRun:
    def __init__(self, public_key="", secret_key="", base_url="https://cloud.langfuse.com"):
        self.session_id = "audit-" + uuid4().hex
        self.client = None
        self.error = ""
        self.positions = []
        if public_key and secret_key:
            try:
                self.client = _configured_client(public_key, secret_key, base_url)
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
                self.trace_id = run.client.create_trace_id()
                with propagate_attributes(session_id=run.session_id, tags=["vor-audit"],
                                          trace_name="audit-vor-position"):
                    self.root = run.client.start_observation(
                        name="audit-vor-position", input=input, metadata=metadata,
                        trace_context={"trace_id": self.trace_id})
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
