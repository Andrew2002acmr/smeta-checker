"""CPU-only KDFP extraction for Open WebUI; no database or model API calls."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import secrets
from tempfile import NamedTemporaryFile
import threading
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from pdf_table_export import prepare_pages, save_exports

MAX_UPLOAD_BYTES = 500*1024*1024


def get_app(*, token=None, cache_dir=None, max_bytes=MAX_UPLOAD_BYTES):
    expected = token if token is not None else os.environ.get("AUDIT_API_TOKEN", "")
    if not expected:
        raise ValueError("AUDIT_API_TOKEN is required for table extraction")
    directory = Path(cache_dir or os.environ.get("TABLE_EXTRACTION_CACHE", ".audit_cache/table-extraction"))
    directory.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    app = FastAPI(title="KDFP document extraction")

    def authorize(request):
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), ("Bearer "+expected).encode()):
            raise HTTPException(401, "Invalid extraction API token")

    def page_numbers(request):
        value = request.query_params.get("pages")
        if value is None:
            return None
        try:
            pages = [int(p) for p in value.split(",")]
        except ValueError as exc:
            raise HTTPException(422, "pages must contain original PDF page numbers") from exc
        if not pages or min(pages) < 1 or len(set(pages)) != len(pages):
            raise HTTPException(422, "pages must be positive and unique")
        return pages

    def extract(path, pages, filename, digest):
        import pymupdf
        with pymupdf.open(path) as document:
            if pages and max(pages) > len(document):
                raise HTTPException(422, "Requested page exceeds the original PDF")
        records = list(prepare_pages(path, pages, cache_dir=directory))
        source = dict(filename=filename, sha256=digest, mimetype="application/pdf")
        values = save_exports(records, source, directory/digest/"exports")
        errors = [dict(page=r["page"], errors=r["errors"]) for r in records if r["errors"]]
        if errors:
            raise HTTPException(422, dict(message="PDF preparation is incomplete; partial evidence was retained",
                                          pdf_sha256=digest, pages=errors))
        if not values["openwebui.json"]:
            raise HTTPException(422, "PDF contains no extracted text or table rows")
        return values

    async def receive(chunks, request, filename):
        if not lock.acquire(blocking=False):
            raise HTTPException(429, "Another PDF is being prepared; no duplicate task was started")
        path = None
        try:
            pages = page_numbers(request)
            digest = hashlib.sha256()
            size = 0
            with NamedTemporaryFile(dir=directory, suffix=".pdf", delete=False) as stream:
                path = Path(stream.name)
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(413, "PDF exceeds the upload limit")
                    digest.update(chunk)
                    stream.write(chunk)
            if not size:
                raise HTTPException(422, "Empty PDF upload")
            import pymupdf
            try:
                return await run_in_threadpool(extract, path, pages, filename, digest.hexdigest())
            except pymupdf.FileDataError as exc:
                raise HTTPException(422, "Invalid PDF upload") from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
            lock.release()

    @app.get("/healthz")
    def health():
        return {"status": True, "engine": "kdfp"}

    @app.put("/process")
    async def process(request: Request):
        authorize(request)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/pdf":
            raise HTTPException(415, "This extractor accepts PDF; other formats need their own loader")
        filename = Path(unquote(request.headers.get("x-filename", "document.pdf"))).name
        values = await receive(request.stream(), request, filename)
        return values["openwebui.json"]

    @app.post("/v1/convert/file")
    async def convert_file(request: Request):
        authorize(request)
        # Same file upload contract used by Open WebUI's built-in Docling loader.
        async with request.form(max_files=1) as form:
            files = form.getlist("files")
            if len(files) != 1 or not hasattr(files[0], "read"):
                raise HTTPException(422, "Exactly one PDF file is required")
            upload = files[0]
            if upload.content_type not in {"application/pdf", "application/octet-stream"}:
                raise HTTPException(415, "Only PDF extraction is supported")
            formats = set(form.getlist("to_formats")) or {"md", "json"}
            if not formats <= {"md", "json"}:
                raise HTTPException(422, "Supported output formats: md, json")
            async def chunks():
                while chunk := await upload.read(1024*1024):
                    yield chunk
            filename = Path(upload.filename or "document.pdf").name
            values = await receive(chunks(), request, filename)
        # Flat Markdown tables destroy spans. Even the text channel contains
        # self-contained JSON facts with repeated full physical header paths.
        text = "\n\n".join(d["page_content"] for d in values["openwebui.json"])
        return dict(status="success", document=dict(filename=filename,
            md_content=text if "md" in formats else None,
            json_content=values["docling.json"] if "json" in formats else None))

    return app
