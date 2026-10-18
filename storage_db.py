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
        self._engine = create_engine(database_url)
        self._blobs = blob_store

    def reset_for_tests(self) -> None:
        """Drop all storage tables and re-apply migrations; tests only."""
        if os.environ.get("ALLOW_TEST_RESET") != "1" and "smeta_test" not in str(
            self._engine.url
        ):
            raise RuntimeError("reset_for_tests is restricted to test databases")
        downgrade(str(self._engine.url))
        migrate(str(self._engine.url))

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
                "found_pages": list(track.get("found_pages") or []),
                "vor_pages": list(track.get("vor_pages") or []),
                "evidence": [dict(item) for item in track.get("evidence") or []],
                "discrepancy_details": result.get("Детали расхождений", ""),
                "search_complete": bool(track.get("search_complete")),
                "requests": track.get("requests", 0),
                "contexts": track.get("contexts", 0),
                "pdf_name": track.get("pdf_name", ""),
                "trace_id": track.get("trace_id"),
                "trace_url": track.get("trace_url"),
            }
            pdf_name = track.get("pdf_name") or ""
            if pdf_name in pdf_versions:
                payload["pdf_version_id"] = str(pdf_versions[pdf_name].id)
                payload["pdf_sha256"] = pdf_versions[pdf_name].sha256
            self.save_position_result(run.id, int(task.get("idx", 0)), payload)
        self.save_run_report(run.id, "excel", bytes(excel_bytes))
        self.save_run_report(run.id, "word", bytes(word_bytes))
        self.finish_run(run.id, "done")
        return run.id

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

    def finish_run(self, run_id: UUID, status: str) -> None:
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(str(run_id))
            run.status = status
            run.finished_at = func.now()
            session.commit()

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
