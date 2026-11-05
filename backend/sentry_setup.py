"""Shared GlitchTip/Sentry initialisation for API and Celery workers."""

from __future__ import annotations

import os

from audit_observability import current


def _scrub(event, hint):  # type: ignore[no-untyped-def]
    request = (event.get("request") or {})
    headers = request.get("headers") or {}
    for key in list(headers):
        if any(marker in key.lower() for marker in ("authorization", "cookie", "token", "password", "secret")):
            headers[key] = "[redacted]"
    # Drop raw PDF/document bytes and large blobs from extra/context.
    for container in ("extra", "contexts"):
        value = event.get(container)
        if isinstance(value, dict):
            for key in list(value):
                if any(marker in str(key).lower() for marker in ("pdf", "bytes", "blob", "document")):
                    value[key] = "[redacted]"
    return event


def init_sentry(service: str) -> None:
    dsn = os.getenv("GLITCHTIP_DSN") or ""
    if not dsn.strip():
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.celery import CeleryIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        return
    sentry_sdk.init(
        dsn=dsn.strip(),
        environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        release=os.getenv("SENTRY_RELEASE", ""),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        send_default_pii=False,
        integrations=[
            FastApiIntegration(),
            CeleryIntegration(),
            SqlalchemyIntegration(),
        ],
        before_send=_scrub,
    )
    sentry_sdk.set_tag("service", service)
    # Propagate current audit context as tags when available.
    ctx = current()
    for key in ("request_id", "run_id", "project_id", "position_index"):
        if key in ctx and ctx[key] is not None:
            sentry_sdk.set_tag(key, str(ctx[key]))
