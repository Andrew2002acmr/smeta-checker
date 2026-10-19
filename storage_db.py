"""Postgres repository for projects, documents, runs, and position results.

Metadata lives in PostgreSQL (migrated with Alembic); bytes live in the blob
store and are referenced by ``blob_key``. History is append-only: reruns add
rows, re-uploads add versions, and ``reset_for_tests`` is the only delete path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from storage_blobs import BlobStore
from storage_models import (
    Document,
    DocumentVersion,
    PositionJob,
    PositionResult,
    Project,
    Run,
    RunArtifact,
)


@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class VersionRef:
    id: UUID
    document_id: UUID
    filename: str
    kind: str
    version_no: int
    sha256: str
    blob_key: str
    size: int


@dataclass(frozen=True, slots=True)
class DocumentVersionRow:
    id: UUID
    document_id: UUID
    version_no: int
    sha256: str
    blob_key: str
    size: int


@dataclass(frozen=True, slots=True)
class RunRow:
    id: UUID
    project_id: UUID
    vor_version_id: UUID
    status: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PositionJobRow:
    id: UUID
    run_id: UUID
    position_index: int
    queue: str
    status: str
    attempts: int
    worker: str | None
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


def _alembic_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("db.url_override", database_url)
    return config


def migrate(database_url: str, revision: str = "head") -> None:
    """Apply Alembic migrations; the single schema-change entry point."""
    command.upgrade(_alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    """Revert Alembic migrations; used by migration tests only."""
    command.downgrade(_alembic_config(database_url), revision)


def _read_source_bytes(source: Any) -> bytes:
    """Read VOR bytes without disturbing the caller's stream position."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, str):
        return Path(source).read_bytes()
    if hasattr(source, "getvalue"):
        return bytes(source.getvalue())
    position = source.tell() if hasattr(source, "tell") else None
    try:
        source.seek(0)
        return bytes(source.read())
    finally:
        if position is not None:
            source.seek(position)


class Repository:
    def __init__(self, database_url: str, blob_store: BlobStore) -> None:
        self._database_url = database_url
        self._engine = create_engine(database_url)
        self._blobs = blob_store

    def reset_for_tests(self) -> None:
        """Drop all storage tables and re-apply migrations; tests only."""
        if os.environ.get("ALLOW_TEST_RESET") != "1" and "smeta_test" not in str(
            self._engine.url
        ):
            raise RuntimeError("reset_for_tests is restricted to test databases")
        # NOTE: never str(engine.url) — SQLAlchemy masks the password as "***".
        downgrade(self._database_url)
        migrate(self._database_url)

    def get_or_create_project(self, name: str) -> ProjectRow:
        with Session(self._engine) as session:
            project = session.scalar(select(Project).where(Project.name == name))
            if project is None:
                project = Project(name=name)
                session.add(project)
                session.commit()
                session.refresh(project)
            return ProjectRow(id=project.id, name=project.name)

    def create_project(self, name: str) -> ProjectRow:
        with Session(self._engine) as session:
            project = Project(name=name)
            session.add(project)
            session.commit()
            session.refresh(project)
            return ProjectRow(id=project.id, name=project.name)

    def list_project_names(self) -> list[str]:
        with Session(self._engine) as session:
            return list(
                session.scalars(select(Project.name).order_by(Project.name)).all()
            )

    def list_projects(self) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            return [{"id": str(p.id), "name": p.name}
                    for p in session.scalars(select(Project).order_by(Project.created_at.desc(), Project.id))]

    def load_project(self, project_id: UUID) -> ProjectRow:
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            return ProjectRow(id=project.id, name=project.name)

    def list_versions(self, project_id: UUID) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            rows = session.execute(
                select(Document, DocumentVersion).join(
                    DocumentVersion, DocumentVersion.document_id == Document.id
                ).where(Document.project_id == project_id).order_by(
                    Document.created_at, Document.id, DocumentVersion.version_no.desc()
                )
            ).all()
            return [{"id": str(v.id), "project_id": str(d.project_id),
                     "document_id": str(d.id), "filename": d.filename, "kind": d.kind,
                     "number": v.version_no, "sha256": v.sha256, "size": v.size,
                     "created_at": v.created_at} for d, v in rows]

    def version_project_id(self, version_id: UUID) -> UUID:
        with Session(self._engine) as session:
            value = session.scalar(select(Document.project_id).join(
                DocumentVersion, DocumentVersion.document_id == Document.id
            ).where(DocumentVersion.id == version_id))
            if value is None:
                raise KeyError(str(version_id))
            return value

    def persist_vor_run(
        self,
        *,
        vor_name: str,
        vor_source: Any,
        pdf_pool: dict[str, Any],
        tasks: list[dict[str, Any]],
        tracking: list[dict[str, Any]],
        results: list[dict[str, Any]],
        excel_bytes: bytes,
        word_bytes: bytes,
        model_name: str,
        project_name: str = "default",
        pdf_sources: dict[str, Any] | None = None,
    ) -> UUID:
        """Persist one VOR audit as a new run; returns the run id.

        Documents are identified by sha256 of their source bytes, not by
        basename: PDFs resolve through the same pool keys the auditor used,
        VOR bytes are read without disturbing the caller's stream position.
        """
        project = self.get_or_create_project(project_name)
        vor_version = self.add_document_version(
            project.id, _read_source_bytes(vor_source), filename=vor_name, kind="vor"
        )
        pdf_versions: dict[str, DocumentVersionRow] = {}
        for key, doc in (pdf_pool or {}).items():
            source = (pdf_sources or {}).get(key)
            if source is None:
                pdf_bytes = bytes(doc.tobytes())
            else:
                pdf_bytes = _read_source_bytes(source)
            pdf_versions[key] = self.add_document_version(
                project.id, pdf_bytes, filename=key, kind="pdf"
            )
        run = self.create_run(
            project.id,
            vor_version.id,
            {
                "model": model_name,
                "pdf_versions": {
                    key: str(version.id) for key, version in pdf_versions.items()
                },
            },
        )
        self.save_vor_run_payload(
            run.id,
            tasks=tasks,
            tracking=tracking,
            results=results,
            excel_bytes=bytes(excel_bytes),
            word_bytes=bytes(word_bytes),
            pdf_versions=pdf_versions,
        )
        self.finish_run(run.id, "done")
        return run.id

    def save_vor_run_payload(
        self,
        run_id: UUID,
        *,
        tasks: list[dict[str, Any]],
        tracking: list[dict[str, Any]],
        results: list[dict[str, Any]],
        excel_bytes: bytes,
        word_bytes: bytes,
        pdf_versions: dict[str, DocumentVersionRow],
    ) -> None:
        """Write position results and Excel/Word artifacts into an open run.

        ``run_audit(..., run_id=...)`` reuses this for API-created runs so the
        stored payload shape never diverges from the Streamlit path.
        """
        for task, track, result in zip(tasks, tracking, results):
            payload = {
                "status": result.get("Статус аудита"),
                "work_name": task.get("w_name"),
                "work_found": bool(result.get("work_found")),
                "volume_confirmed": bool(result.get("volume_confirmed")),
                "unit_confirmed": bool(result.get("unit_confirmed")),
                "comment_confirmed": bool(result.get("comment_confirmed")),
                "found_volume": result.get("Фактический объем", "-"),
                "found_unit": track.get("found_unit", "-"),
                "found_page": track.get("found_page"),
                "volume_calculation": track.get("volume_calculation"),
                "found_pages": list(track.get("found_pages") or []),
                "vor_pages": list(track.get("vor_pages") or []),
                "evidence": [dict(item) for item in track.get("evidence") or []],
                "discrepancy_details": result.get("Детали расхождений", ""),
                "search_complete": bool(track.get("search_complete")),
                "requests": track.get("requests", 0),
                "contexts": track.get("contexts", 0),
                "budget_tokens": track.get("budget_tokens", result.get("budget_tokens")),
                "usage": dict(track.get("usage") or result.get("usage") or {}),
                "pdf_name": track.get("pdf_name", ""),
                "trace_id": track.get("trace_id"),
                "trace_url": track.get("trace_url"),
            }
            pdf_name = track.get("pdf_name") or ""
            if pdf_name in pdf_versions:
                payload["pdf_version_id"] = str(pdf_versions[pdf_name].id)
                payload["pdf_sha256"] = pdf_versions[pdf_name].sha256
            self.save_position_result(run_id, int(task.get("idx", 0)), payload)
        self.save_run_report(run_id, "excel", bytes(excel_bytes))
        self.save_run_report(run_id, "word", bytes(word_bytes))

    def load_version(self, version_id: UUID) -> VersionRef:
        """Load one document version with its document filename and kind."""
        with Session(self._engine) as session:
            version = session.get(DocumentVersion, version_id)
            if version is None:
                raise KeyError(str(version_id))
            document = session.get(Document, version.document_id)
            if document is None:
                raise KeyError(str(version_id))
            return VersionRef(
                id=version.id,
                document_id=document.id,
                filename=document.filename,
                kind=document.kind,
                version_no=version.version_no,
                sha256=version.sha256,
                blob_key=version.blob_key,
                size=version.size,
            )

    def add_document_version(
        self, project_id: UUID, data: bytes, *, filename: str, kind: str
    ) -> DocumentVersionRow:
        blob = self._blobs.put(bytes(data))
        with Session(self._engine) as session:
            document = session.scalar(
                select(Document).where(
                    Document.project_id == project_id,
                    Document.kind == kind,
                    Document.filename == filename,
                )
            )
            if document is None:
                document = Document(project_id=project_id, kind=kind, filename=filename)
                session.add(document)
                session.flush()
            existing = session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.sha256 == blob.sha256,
                )
            )
            if existing is not None:
                return DocumentVersionRow(
                    id=existing.id,
                    document_id=document.id,
                    version_no=existing.version_no,
                    sha256=existing.sha256,
                    blob_key=existing.blob_key,
                    size=existing.size,
                )
            version_no = (
                session.scalar(
                    select(
                        func.coalesce(func.max(DocumentVersion.version_no), 0)
                    ).where(DocumentVersion.document_id == document.id)
                )
                or 0
            ) + 1
            version = DocumentVersion(
                document_id=document.id,
                version_no=version_no,
                sha256=blob.sha256,
                blob_key=blob.key,
                size=blob.size,
            )
            session.add(version)
            session.commit()
            session.refresh(version)
            return DocumentVersionRow(
                id=version.id,
                document_id=document.id,
                version_no=version.version_no,
                sha256=version.sha256,
                blob_key=version.blob_key,
                size=version.size,
            )

    def create_run(
        self, project_id: UUID, vor_version_id: UUID, config: dict[str, Any]
    ) -> RunRow:
        with Session(self._engine) as session:
            run = Run(
                project_id=project_id,
                vor_version_id=vor_version_id,
                config=dict(config),
                status="running",
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return RunRow(
                id=run.id,
                project_id=run.project_id,
                vor_version_id=run.vor_version_id,
                status=run.status,
                created_at=run.created_at,
            )

    def update_run_config(self, run_id: UUID, patch: dict[str, Any]) -> None:
        """Merge ``patch`` into the stored run config (metrics, timings)."""
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(str(run_id))
            config = dict(run.config or {})
            config.update(dict(patch or {}))
            run.config = config
            session.commit()

    def claim_finalize(self, run_id: UUID) -> bool:
        """Atomically claim the finalize trigger; exactly one worker wins.

        Returns True only for the worker that flipped ``finalize_chained``
        from unset to True; losers get False and must not dispatch.
        """
        with Session(self._engine) as session:
            run = session.scalar(
                select(Run).where(Run.id == run_id).with_for_update()
            )
            if run is None:
                raise KeyError(str(run_id))
            config = dict(run.config or {})
            if config.get("finalize_chained"):
                session.commit()
                return False
            config["finalize_chained"] = True
            run.config = config
            session.commit()
            return True

    def save_position_result(
        self, run_id: UUID, position_index: int, result: dict[str, Any]
    ) -> None:
        payload = dict(result)
        with Session(self._engine) as session:
            existing = session.scalar(
                select(PositionResult).where(
                    PositionResult.run_id == run_id,
                    PositionResult.position_index == position_index,
                )
            )
            if existing is None:
                session.add(
                    PositionResult(
                        run_id=run_id,
                        position_index=position_index,
                        result=payload,
                    )
                )
            else:
                existing.result = payload
            session.commit()

    def save_run_report(self, run_id: UUID, kind: str, data: bytes) -> None:
        blob = self._blobs.put(bytes(data))
        with Session(self._engine) as session:
            existing = session.scalar(
                select(RunArtifact).where(
                    RunArtifact.run_id == run_id, RunArtifact.kind == kind
                )
            )
            if existing is None:
                session.add(
                    RunArtifact(
                        run_id=run_id, kind=kind, blob_key=blob.key, size=blob.size
                    )
                )
            else:
                existing.blob_key = blob.key
                existing.size = blob.size
            session.commit()

    def finish_run(
        self,
        run_id: UUID,
        status: str,
        *,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(str(run_id))
            run.status = status
            run.finished_at = func.now()
            if metrics is not None:
                config = dict(run.config or {})
                config["metrics"] = dict(metrics)
                run.config = config
            session.commit()

    @staticmethod
    def _job_row(job: PositionJob) -> PositionJobRow:
        return PositionJobRow(
            id=job.id,
            run_id=job.run_id,
            position_index=job.position_index,
            queue=job.queue,
            status=job.status,
            attempts=job.attempts,
            worker=job.worker,
            payload=dict(job.payload or {}),
            result=dict(job.result) if job.result is not None else None,
            error=job.error,
        )

    def create_position_job(
        self, run_id: UUID, position_index: int, *, queue: str, payload: dict[str, Any]
    ) -> PositionJobRow:
        """Queue one position; re-creating the same index returns the row."""
        with Session(self._engine) as session:
            existing = session.scalar(
                select(PositionJob).where(
                    PositionJob.run_id == run_id,
                    PositionJob.position_index == position_index,
                )
            )
            if existing is not None:
                return self._job_row(existing)
            job = PositionJob(
                run_id=run_id,
                position_index=position_index,
                queue=queue,
                status="queued",
                attempts=0,
                payload=dict(payload),
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            return self._job_row(job)

    def load_position_job(self, run_id: UUID, position_index: int) -> PositionJobRow:
        with Session(self._engine) as session:
            job = session.scalar(
                select(PositionJob).where(
                    PositionJob.run_id == run_id,
                    PositionJob.position_index == position_index,
                )
            )
            if job is None:
                raise KeyError(f"{run_id}:{position_index}")
            return self._job_row(job)

    def claim_position_job(
        self, run_id: UUID, position_index: int, *, worker: str
    ) -> PositionJobRow:
        """Claim one job: same worker re-claims, another worker gets LookupError.

        ``queued`` and ``failed`` rows move to ``running`` with attempts + 1;
        an already ``running`` row is returned only to its owner.
        """
        with Session(self._engine) as session:
            job = session.scalar(
                select(PositionJob)
                .where(
                    PositionJob.run_id == run_id,
                    PositionJob.position_index == position_index,
                )
                .with_for_update()
            )
            if job is None:
                raise KeyError(f"{run_id}:{position_index}")
            if job.status == "running" and job.worker != worker:
                raise LookupError(f"job {run_id}:{position_index} is claimed")
            if job.status in {"queued", "failed"}:
                job.status = "running"
                job.worker = worker
                job.attempts = int(job.attempts or 0) + 1
                job.error = None
            session.commit()
            session.refresh(job)
            return self._job_row(job)

    def finish_position_job(
        self,
        run_id: UUID,
        position_index: int,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"done", "failed"}:
            raise ValueError(f"unknown job status: {status!r}")
        with Session(self._engine) as session:
            job = session.scalar(
                select(PositionJob).where(
                    PositionJob.run_id == run_id,
                    PositionJob.position_index == position_index,
                )
            )
            if job is None:
                raise KeyError(f"{run_id}:{position_index}")
            job.status = status
            if result is not None:
                job.result = dict(result)
            job.error = error
            session.commit()

    def requeue_position_job(
        self,
        run_id: UUID,
        position_index: int,
        *,
        queue: str,
        payload: dict[str, Any],
    ) -> None:
        """Move a claimed job to another queue without consuming an attempt."""
        with Session(self._engine) as session:
            job = session.scalar(
                select(PositionJob).where(
                    PositionJob.run_id == run_id,
                    PositionJob.position_index == position_index,
                )
            )
            if job is None:
                raise KeyError(f"{run_id}:{position_index}")
            if job.status == "done":
                return
            job.queue = queue
            job.payload = dict(payload)
            job.status = "queued"
            job.worker = None
            job.error = None
            session.commit()

    def list_position_jobs(self, run_id: UUID) -> list[PositionJobRow]:
        """List all job rows of a run ordered by position index."""
        with Session(self._engine) as session:
            rows = session.scalars(
                select(PositionJob)
                .where(PositionJob.run_id == run_id)
                .order_by(PositionJob.position_index)
            ).all()
            return [self._job_row(job) for job in rows]

    def recover_stale_jobs(self, run_id: UUID, *, alive_workers: set[str]) -> list[int]:
        """Requeue ``running`` jobs whose worker is gone; returns indices."""
        recovered: list[int] = []
        with Session(self._engine) as session:
            jobs = session.scalars(
                select(PositionJob)
                .where(
                    PositionJob.run_id == run_id,
                    PositionJob.status == "running",
                )
                .with_for_update()
            ).all()
            for job in jobs:
                if job.worker in alive_workers:
                    continue
                job.status = "queued"
                job.worker = None
                recovered.append(job.position_index)
            session.commit()
        return sorted(recovered)

    def run_progress_records(
        self, *, project_id: UUID | None = None, run_id: UUID | None = None,
        offset: int = 0, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read only metadata and grouped counts, never job/result JSON payloads."""
        page = select(
            Run.id, Run.project_id, Run.vor_version_id, Run.status,
            Run.created_at, Run.finished_at,
            func.jsonb_array_length(Run.config["position_inputs"]).label("input_total"),
            Run.config["prepare_seconds"].as_float().label("prepare_seconds"),
            Run.config["pdf_preparation"].label("pdf_preparation"),
        )
        if project_id is not None:
            page = page.where(Run.project_id == project_id)
        if run_id is not None:
            page = page.where(Run.id == run_id)
        page = page.order_by(Run.created_at.desc(), Run.id.desc()).offset(offset).limit(limit).subquery()
        jobs = select(
            PositionJob.run_id, PositionJob.queue, PositionJob.status,
            func.count().label("count"),
        ).where(PositionJob.run_id.in_(select(page.c.id))).group_by(
            PositionJob.run_id, PositionJob.queue, PositionJob.status,
        ).subquery()
        results = select(
            PositionResult.run_id, func.count().label("count"),
        ).where(PositionResult.run_id.in_(select(page.c.id))).group_by(PositionResult.run_id).subquery()
        query = select(
            page, Document.filename, jobs.c.queue, jobs.c.status.label("job_status"),
            jobs.c.count.label("job_count"), results.c.count.label("result_count"),
        ).join(DocumentVersion, DocumentVersion.id == page.c.vor_version_id).join(
            Document, Document.id == DocumentVersion.document_id,
        ).outerjoin(jobs, jobs.c.run_id == page.c.id).outerjoin(
            results, results.c.run_id == page.c.id,
        ).order_by(page.c.created_at.desc(), page.c.id.desc())
        with Session(self._engine) as session:
            records: dict[UUID, dict[str, Any]] = {}
            for row in session.execute(query).mappings():
                record = records.setdefault(row["id"], {
                    **{key: row[key] for key in (
                        "id", "project_id", "vor_version_id", "status", "created_at", "finished_at",
                        "input_total", "prepare_seconds", "pdf_preparation",
                    )},
                    "vor_filename": row["filename"], "result_count": row["result_count"] or 0,
                    "job_counts": {}, "pdf_jobs": 0,
                })
                if row["job_status"]:
                    counts = record["job_counts"]
                    counts[row["job_status"]] = counts.get(row["job_status"], 0) + row["job_count"]
                    if row["queue"] == "pdf":
                        record["pdf_jobs"] += row["job_count"]
            return list(records.values())

    def list_runs(self, project_id: UUID) -> list[RunRow]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(Run)
                .where(Run.project_id == project_id)
                .order_by(Run.created_at.desc(), Run.id.desc())
            ).all()
            return [
                RunRow(
                    id=row.id,
                    project_id=row.project_id,
                    vor_version_id=row.vor_version_id,
                    status=row.status,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def load_run(self, run_id: UUID) -> dict[str, Any]:
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(str(run_id))
            version = session.get(DocumentVersion, run.vor_version_id)
            document = session.get(Document, version.document_id) if version else None
            results = session.scalars(
                select(PositionResult)
                .where(PositionResult.run_id == run_id)
                .order_by(PositionResult.position_index)
            ).all()
            return {
                "id": run.id,
                "project_id": run.project_id,
                "status": run.status,
                "config": dict(run.config or {}),
                "vor_version_id": run.vor_version_id,
                "vor_filename": document.filename if document else "",
                "vor_sha256": version.sha256 if version else "",
                "results": [dict(item.result) for item in results],
                "indexed_results": [{"position_index": item.position_index,
                                     "result": dict(item.result)} for item in results],
                "created_at": run.created_at,
                "finished_at": run.finished_at,
            }

    def load_run_report(self, run_id: UUID, kind: str) -> bytes:
        with Session(self._engine) as session:
            artifact = session.scalar(
                select(RunArtifact).where(
                    RunArtifact.run_id == run_id, RunArtifact.kind == kind
                )
            )
            if artifact is None:
                raise KeyError(f"{run_id}:{kind}")
            blob_key = artifact.blob_key
        return self._blobs.get(blob_key)


def open_repository(database_url: str, blob_store: BlobStore) -> Repository:
    """Open the repository; applies pending migrations before first use."""
    migrate(database_url)
    return Repository(database_url, blob_store)
