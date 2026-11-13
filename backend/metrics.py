"""Prometheus metrics for the smeta-checker API."""

from __future__ import annotations

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

HTTP_REQUESTS_TOTAL = Counter(
    "smeta_http_requests_total",
    "Total HTTP requests handled by the API.",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "smeta_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

AUDIT_RUNS_TOTAL = Counter(
    "smeta_audit_runs_total",
    "Audit runs by terminal status.",
    ["status"],
)
AUDIT_POSITIONS_TOTAL = Counter(
    "smeta_audit_positions_total",
    "Audit positions by terminal status.",
    ["status"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
