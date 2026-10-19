# issue 22: CPU-only production image for API + Celery workers.
# One image, three commands: uvicorn (api), celery pdf worker, celery llm worker.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# Tesseract + tessdata for OCR (see packages.txt); pg_isready/redis-cli come
# from the db/redis images themselves, workers need only TCP reachability.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    curl \
  && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /srv/smeta

# Dependencies first: uv.lock pins every package, layer survives code edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY *.py ./

# Non-root at runtime; blobs/cache volumes are mounted writable.
RUN useradd --create-home --uid 10001 app \
  && mkdir -p /srv/smeta/.audit_blobs /srv/smeta/.audit_cache \
  && chown -R app:app /srv/smeta
USER app

ENV PATH="/srv/smeta/.venv/bin:$PATH" \
    BLOB_STORE_URL="file:///srv/smeta/.audit_blobs"

EXPOSE 8000 8501
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["uvicorn", "api:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
