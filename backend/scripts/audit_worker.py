"""Isolate native PDF parsing from the HTTP interpreter (including its GIL)."""

import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from audit_models import AuditSettings


def stop_worker(process):
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        process.kill()
    process.wait(timeout=10)


def wait_for_progress(process, repository, run_id, timeout):
    """Bound inactivity, rather than the length of a healthy multi-position run."""
    changed = time.monotonic()
    last = None
    while process.poll() is None:
        records = repository.run_progress_records(run_id=run_id)
        record = records[0] if records else {}
        current = (
            record.get("pdf_preparation"),
            record.get("job_counts"),
            record.get("result_count"),
        )
        if current != last:
            last, changed = current, time.monotonic()
        if time.monotonic() - changed > timeout:
            stop_worker(process)
            repository.update_run_config(
                run_id,
                {
                    "execution_error": "Обработка остановлена: нет прогресса дольше установленного лимита времени.",
                },
            )
            raise TimeoutError("Audit worker made no progress within its time limit")
        try:
            process.wait(timeout=min(1, timeout))
        except subprocess.TimeoutExpired:
            pass
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, "audit_worker")


def run_isolated(settings: AuditSettings, run_id: UUID) -> None:
    from storage_db import Repository
    from storage_blobs import open_blob_store

    # Credentials travel over an anonymous pipe, never command arguments or files.
    with subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "scripts.audit_worker", str(run_id)],
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    ) as process:
        assert process.stdin is not None
        process.stdin.write(settings.model_dump_json())
        process.stdin.close()
        try:
            wait_for_progress(
                process,
                Repository(
                    settings.database_url, open_blob_store(settings.blob_store_url)
                ),
                run_id,
                max(1, settings.task_time_limit),
            )
        finally:
            stop_worker(process)


def main():
    from api import ApiContext, run_tasks_job
    from storage_blobs import open_blob_store
    from storage_db import DocumentVersionRow, Repository

    settings = AuditSettings.model_validate_json(sys.stdin.read())
    blobs = open_blob_store(settings.blob_store_url)
    repository = Repository(settings.database_url, blobs)
    run_id = UUID(sys.argv[1])
    record = repository.load_run(run_id)
    vor = repository.load_version(record["vor_version_id"])
    versions = {}
    for name, value in record["config"].get("pdf_versions", {}).items():
        ref = repository.load_version(UUID(value))
        versions[name] = DocumentVersionRow(
            id=ref.id,
            document_id=ref.document_id,
            version_no=ref.version_no,
            sha256=ref.sha256,
            blob_key=ref.blob_key,
            size=ref.size,
            filename=ref.filename,
        )
    context = ApiContext(settings=settings, repository=repository, blob_store=blobs)
    run_tasks_job(
        context,
        run_id,
        project=repository.load_project(record["project_id"]).name,
        vor_filename=vor.filename,
        vor_bytes=blobs.get(vor.blob_key),
        pdf_files={name: blobs.get(ref.blob_key) for name, ref in versions.items()},
        pdf_versions=versions,
        request_id=record["config"].get("request_id", ""),
    )


if __name__ == "__main__":
    main()
