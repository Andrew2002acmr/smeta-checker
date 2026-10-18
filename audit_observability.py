"""Structured audit logging and run metrics (issue 21).

``bind`` attaches correlation keys (``audit_run_id``, ``position_index``,
``request_id``, ``stage``, ``worker``) to every record emitted in this
context; values flow through threads/tasks explicitly via ``snapshot`` /
``restore``. ``get_logger`` renders one JSON object per line with secret
scrubbing: any mapping key whose lowercase name contains ``key``,
``secret``, ``token``, ``password`` or ``auth`` is replaced by
``"[redacted]"``.

``RunMetrics`` aggregates the same identifiers plus counters and durations
for operators: errors by kind, per-stage durations, LLM token usage and an
optional cost estimate. ``AuditResult`` keeps ``budget_tokens``; the charged
total comes from ``usage.total_tokens`` when the provider reports it.
``estimate_cost`` multiplies reported input/output tokens by configured
per-1K rates (default zero = unknown, not free).
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_SECRET_MARKERS = ("key", "secret", "token", "password", "auth", "cookie")
REDACTED = "[redacted]"

_context: ContextVar[dict[str, Any]] = ContextVar("audit_log_context", default={})


def bind(**fields: Any) -> Any:
    """Attach correlation fields to subsequent log records; returns a token."""
    merged = dict(_context.get())
    merged.update({key: value for key, value in fields.items() if value is not None})
    return _context.set(merged)


def reset(token: Any) -> None:
    """Release a token previously returned by :func:`bind`."""
    _context.reset(token)


def snapshot() -> dict[str, Any]:
    """Copy the current correlation fields for another thread/task."""
    return dict(_context.get())


def restore(fields: dict[str, Any]) -> Any:
    """Install a snapshot taken in another thread/task; returns a token."""
    return _context.set(dict(fields or {}))


def current() -> dict[str, Any]:
    """Return the active correlation mapping (empty outside a run)."""
    return dict(_context.get())


def scrub(value: Any) -> Any:
    """Replace secret-looking mapping values with ``[redacted]``."""
    if isinstance(value, dict):
        return {
            key: REDACTED
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS)
            else scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub(item) for item in value)
    return value


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class _JsonFormatter(logging.Formatter):
    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "audit_run_id",
            "position_index",
            "request_id",
            "stage",
            "worker",
            "queue",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        for key, value in vars(record).items():
            if key not in self._RESERVED and key not in payload and value is not None:
                payload[key] = value
        if record.exc_info and not record.exc_text:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(scrub(payload), ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return ``name`` with the JSON correlation formatter attached once."""
    logger = logging.getLogger(name)
    if not any(isinstance(item, _ContextFilter) for item in logger.filters):
        logger.addFilter(_ContextFilter())
    for handler in logging.root.handlers:
        if not any(isinstance(item, _JsonFormatter) for item in [handler.formatter]):
            handler.setFormatter(_JsonFormatter())
    if not logging.root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.root.addHandler(handler)
        logging.root.addFilter(_ContextFilter())
    return logger


def estimate_cost(
    usage: dict[str, int] | Any,
    input_per_1k: float,
    output_per_1k: float,
) -> float | None:
    """Estimate LLM cost from reported tokens; ``None`` when unknown.

    ``input``/``output`` from :func:`audit_tracing.token_usage` are
    authoritative. A ``total``-only usage cannot be split, so it yields
    ``None`` rather than a fabricated split; zero or negative rates also
    yield ``None`` (unknown tariff, not free).
    """
    if isinstance(usage, dict):
        tokens = usage
    else:
        tokens = {
            "input": getattr(
                usage, "input_tokens", getattr(usage, "prompt_tokens", None)
            ),
            "output": getattr(
                usage, "output_tokens", getattr(usage, "completion_tokens", None)
            ),
        }

    def _count(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) and value >= 0 else None

    prompt = _count(tokens.get("input"))
    completion = _count(tokens.get("output"))
    if prompt is None or completion is None:
        return None
    if input_per_1k <= 0 or output_per_1k <= 0:
        return None
    return round(prompt / 1000 * input_per_1k + completion / 1000 * output_per_1k, 6)


class RunMetrics:
    """In-memory per-run counters; the repository persists the summary."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = str(run_id or "")
        self.started_at = time.monotonic()
        self.finished_at: float | None = None
        self.errors: dict[str, int] = {}
        self.statuses: dict[str, int] = {}
        self.stage_seconds: dict[str, float] = {}
        self.stage_counts: dict[str, int] = {}
        self.requests = 0
        self.contexts = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.unknown_usage = 0
        self.estimated_cost: float | None = None

    def record_stage(self, stage: str, seconds: float) -> None:
        self.stage_seconds[stage] = self.stage_seconds.get(stage, 0.0) + max(
            0.0, float(seconds)
        )
        self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1

    def record_error(self, kind: str) -> None:
        key = str(kind or "unknown")
        self.errors[key] = self.errors.get(key, 0) + 1

    def record_position(
        self,
        result: dict[str, Any],
        *,
        usage: dict[str, int] | None = None,
        cost: float | None = None,
    ) -> None:
        status = str(result.get("status", result.get("Статус аудита", "unknown")))
        self.statuses[status] = self.statuses.get(status, 0) + 1
        for field, attr in (("requests", "requests"), ("contexts", "contexts")):
            value = result.get(field, result.get(f"[ПД] {field.title()}", 0))
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                setattr(self, attr, getattr(self, attr) + value)
        if isinstance(usage, dict):
            if isinstance(usage.get("input"), int):
                self.input_tokens += max(0, usage["input"])
            if isinstance(usage.get("output"), int):
                self.output_tokens += max(0, usage["output"])
            if "input" not in usage and "output" not in usage:
                self.unknown_usage += 1
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.estimated_cost = round((self.estimated_cost or 0.0) + cost, 6)

    def finish(self) -> dict[str, Any]:
        self.finished_at = time.monotonic()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return {
            "run_id": self.run_id,
            "elapsed_seconds": round(max(0.0, end - self.started_at), 3),
            "requests": self.requests,
            "contexts": self.contexts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "unknown_usage": self.unknown_usage,
            "estimated_cost": self.estimated_cost,
            "statuses": dict(self.statuses),
            "errors": dict(deepcopy(self.errors)),
            "stage_seconds": {
                key: round(value, 3) for key, value in self.stage_seconds.items()
            },
            "stage_counts": dict(self.stage_counts),
        }
