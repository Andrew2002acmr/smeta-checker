# CPU-only production images: full (api + pdf worker) and llm-slim.
# llm-slim drops PDF/VOR/HTTP/OCR deps: -200MB RAM, -500MB disk.
# Base deps in [project], full-only in [dependency-groups.full].

FROM python:3.14-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/srv/smeta
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /srv/smeta
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-group full
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts
COPY *.py ./
RUN useradd --create-home --uid 10001 app \
  && mkdir -p /srv/smeta/.audit_blobs /srv/smeta/.audit_cache /srv/smeta/.normative-sqlite-cache \
  && chown -R app:app /srv/smeta
ENV PATH="/srv/smeta/.venv/bin:$PATH" \
    BLOB_STORE_URL="file:///srv/smeta/.audit_blobs"

# LLM worker: queue + db + model client + tracing only.
FROM base AS llm-slim
USER app
CMD ["celery", "-A", "audit_tasks:worker", "worker", "-Q", "llm"]

# API + PDF worker + migrate: everything incl. PDF/VOR/Word/OCR.
FROM base AS full
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    curl \
    libgl1 \
    libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --group full
RUN .venv/bin/python -m pdf_tables --warm
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["uvicorn", "api:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
