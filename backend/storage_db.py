"""Postgres repository for projects, documents, runs, and position results.

Metadata lives in PostgreSQL (migrated with Alembic); bytes live in the blob
store and are referenced by ``blob_key``. History is append-only: reruns add
rows, re-uploads add versions. Documents are UUID identities; versions carry
their own source filename and extracted metadata. Run PDF pins are relational
(``run_pdf_versions``). VOR designations verify against PDF text at audit
time; explicit VOR-version bindings are snapshotted in run configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select, or_, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from storage_blobs import BlobStore
from storage_models import (
    Base,
    Document,
    DocumentVersion,
    PositionJob,
    PositionResult,
    Project,
    Run,
    RunArtifact,
    RunPdfVersion,
    TaskOutbox,
    HumanBasis,
    PositionResultVersion,
    ReviewArtifact,
    NormativeReview,
)


@dataclass(frozen=True, slots=True)
class ProjectRow:
    id: UUID
    name: str
    archived: bool = False
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DocumentRow:
    id: UUID
    project_id: UUID
    kind: str
    title: str
    archived: bool
    current_version_id: UUID | None


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
    metadata: dict[str, Any] | None = None
    metadata_overrides: dict[str, Any] | None = None
    format_status: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DocumentVersionRow:
    id: UUID
    document_id: UUID
    version_no: int
    sha256: str
    blob_key: str
    size: int
    filename: str = ""
    metadata: dict[str, Any] | None = None
    metadata_overrides: dict[str, Any] | None = None
    format_status: dict[str, Any] | None = None

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


def _alembic_config(database_url: str):
    from alembic.config import Config

    root = Path(__file__).resolve().parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("db.url_override", database_url)
    return config


def migrate(database_url: str, revision: str = "head") -> None:
    """Apply Alembic migrations; the single schema-change entry point."""
    from alembic import command

    command.upgrade(_alembic_config(database_url), revision)


def downgrade(database_url: str, revision: str = "base") -> None:
    """Revert Alembic migrations; used by migration tests only."""
    from alembic import command

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
        # A downgrade cannot reconstruct filename uniqueness after independent
        # same-named documents have been created. Only test data is reset here.
        tables = [*Base.metadata.tables, "alembic_version"]
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                "DROP TABLE IF EXISTS " + ", ".join(tables) + " CASCADE"
            )
        migrate(self._database_url)

    def list_human_basis(self, project_id, vor_version_id, position_key, pdf_version_ids, *, include_revoked=False):
        with Session(self._engine) as session:
            rows = session.scalars(select(HumanBasis).where(
                HumanBasis.project_id == project_id,
                HumanBasis.vor_version_id == vor_version_id,
                HumanBasis.position_key == position_key,
                HumanBasis.pdf_version_id.in_(pdf_version_ids),
                True if include_revoked else HumanBasis.revoked_at.is_(None),
            ).order_by(HumanBasis.created_at, HumanBasis.id)).all()
            return [{"id": str(row.id), "run_id": str(row.run_id), "position_index": row.position_index,
                     "pdf_version_id": str(row.pdf_version_id), "actor": row.actor,
                     "created_at": row.created_at, "revoked_at": row.revoked_at, "revoked_by": row.revoked_by, **row.payload} for row in rows]

    def save_human_basis(self, *, project_id, run_id, position_index, vor_version_id,
                         pdf_version_id, position_key, fingerprint, payload, actor):
        import hashlib
        with Session(self._engine) as session, session.begin():
            previous = session.scalar(select(HumanBasis).where(HumanBasis.fingerprint == fingerprint))
            while previous is not None and previous.revoked_at is not None:
                fingerprint = hashlib.sha256((fingerprint + str(previous.revoked_at)).encode()).hexdigest()
                previous = session.scalar(select(HumanBasis).where(HumanBasis.fingerprint == fingerprint))
            session.execute(insert(HumanBasis).values(
                id=uuid4(), project_id=project_id, run_id=run_id, position_index=position_index,
                vor_version_id=vor_version_id, pdf_version_id=pdf_version_id,
                position_key=position_key, fingerprint=fingerprint, payload=payload, actor=actor,
            ).on_conflict_do_nothing(index_elements=["fingerprint"]))
            row = session.scalar(select(HumanBasis).where(HumanBasis.fingerprint == fingerprint))
            return str(row.id)

    def revoke_human_basis(self, basis_id, project_id, vor_version_id, position_key, actor):
        with Session(self._engine) as session, session.begin():
            row = session.scalar(select(HumanBasis).where(
                HumanBasis.id == basis_id, HumanBasis.project_id == project_id,
                HumanBasis.vor_version_id == vor_version_id, HumanBasis.position_key == position_key,
            ).with_for_update())
            if row is None:
                raise KeyError(str(basis_id))
            row.revoked_at = datetime.now(timezone.utc)
            row.revoked_by = actor

    def get_or_create_project(self, name: str) -> ProjectRow:
        with Session(self._engine) as session:
            project = session.scalar(select(Project).where(Project.name == name))
            if project is None:
                project = Project(name=name)
                session.add(project)
                session.commit()
                session.refresh(project)
            return ProjectRow(id=project.id, name=project.name,
                              archived=bool(project.archived),
                              created_at=project.created_at)

    def create_project(self, name: str) -> ProjectRow:
        # Independent UUID per call: name reuse never merges projects.
        with Session(self._engine) as session:
            project = Project(name=name)
            session.add(project)
            session.commit()
            session.refresh(project)
            return ProjectRow(id=project.id, name=project.name,
                              archived=bool(project.archived),
                              created_at=project.created_at)

    def update_project(
        self, project_id: UUID, *, name: str | None = None,
        archived: bool | None = None,
    ) -> tuple[ProjectRow, dict[str, Any]]:
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            if name is not None:
                project.name = name
            if archived is not None:
                project.archived = bool(archived)
            session.commit()
            session.refresh(project)
            row = ProjectRow(id=project.id, name=project.name,
                             archived=bool(project.archived),
                             created_at=project.created_at)
            return row, {"id": str(row.id), "name": row.name,
                         "archived": row.archived, "created_at": row.created_at}

    def list_projects(self, *, archived: bool = False) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(Project).where(Project.archived == bool(archived)).order_by(
                    Project.created_at.desc(), Project.id)
            ).all()
            return [{"id": str(p.id), "name": p.name,
                     "archived": bool(p.archived), "created_at": p.created_at}
                    for p in rows]

    def load_project(self, project_id: UUID) -> ProjectRow:
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            return ProjectRow(id=project.id, name=project.name,
                              archived=bool(project.archived),
                              created_at=project.created_at)

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
                     "document_id": str(d.id), "filename": v.filename, "kind": d.kind,
                     "number": v.version_no, "sha256": v.sha256, "size": v.size,
                     "created_at": v.created_at,
                     "metadata": dict(v.metadata_ or {}),
                     "metadata_overrides": dict(v.metadata_overrides or {}),
                     "title": d.title or v.filename}
                    for d, v in rows]

    def version_project_id(self, version_id: UUID) -> UUID:
        with Session(self._engine) as session:
            value = session.scalar(select(Document.project_id).join(
                DocumentVersion, DocumentVersion.document_id == Document.id
            ).where(DocumentVersion.id == version_id))
            if value is None:
                raise KeyError(str(version_id))
            return value

    def _document_title(self, metadata: dict[str, Any],
                        overrides: dict[str, Any] | None, filename: str) -> str:
        merged = dict(metadata or {})
        for key, value in dict(overrides or {}).items():
            if str(value or "").strip():
                merged[key] = value
            else:
                merged.pop(key, None)
        code = str(merged.get("document_code") or "").strip()
        title = str(merged.get("raw_title") or "").strip()
        combined = f"{code} {title}".strip()
        return combined or str(filename or "")

    def _present_version(self, session, version: DocumentVersion,
                         document: Document) -> dict[str, Any]:
        metadata = dict(version.metadata_ or {})
        overrides = dict(version.metadata_overrides or {})
        return {
            "id": str(version.id), "project_id": str(document.project_id),
            "document_id": str(document.id), "filename": version.filename,
            "kind": document.kind, "number": version.version_no,
            "sha256": version.sha256, "size": version.size,
            "created_at": version.created_at, "metadata": metadata,
            "metadata_overrides": overrides,
            "format_status": dict(version.format_status or {}),
            "title": document.title or version.filename,
        }

    def _present_document(self, session, document: Document,
                          *, include_runs: bool = True) -> dict[str, Any]:
        versions = session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id
            ).order_by(DocumentVersion.version_no.desc())
        ).all()
        presented = [self._present_version(session, v, document) for v in versions]
        payload: dict[str, Any] = {
            "id": str(document.id), "project_id": str(document.project_id),
            "kind": document.kind,
            "title": document.title or self._document_title(
                presented[0]["metadata"] if presented else {},
                presented[0]["metadata_overrides"] if presented else {}, ""),
            "archived": bool(document.archived),
            "current_version_id": (str(document.current_version_id)
                                   if document.current_version_id else None),
            "versions": presented,
            "last_run": None, "pd_updated": False,
        }
        if include_runs:
            payload["last_run"] = self._document_last_run(session, document)
            payload["pd_updated"] = self._document_pd_updated(
                session, document, presented, payload["last_run"])
        return payload

    def _document_last_run(self, session, document: Document) -> dict[str, Any] | None:
        version_ids = set(session.scalars(select(DocumentVersion.id).where(
            DocumentVersion.document_id == document.id)).all())
        if not version_ids:
            return None
        run = session.scalar(
            select(Run).where(Run.vor_version_id.in_(version_ids)).order_by(
                Run.created_at.desc(), Run.id.desc()))
        if run is None:
            return None
        return {"run_id": str(run.id), "created_at": run.created_at}

    def _document_pd_updated(self, session, document: Document,
                             presented: list[dict[str, Any]],
                             last_run: dict[str, Any] | None) -> bool:
        # Badge only; verdicts stay immutable. Compares pinned PDF versions of
        # the last run against current PDF revisions of mapped documents.
        if document.kind != "vor" or not last_run:
            return False
        try:
            from uuid import UUID as _UUID
            run = session.get(Run, _UUID(str(last_run["run_id"])))
        except Exception:
            return False
        if run is None:
            return False
        pinned = {row.version_id for row in session.scalars(
            select(RunPdfVersion).where(RunPdfVersion.run_id == run.id)).all()}
        if not pinned:
            mapping = dict((run.config or {}).get("pdf_versions") or {})
            try:
                from uuid import UUID as _UUID
                pinned = {_UUID(str(value)) for value in mapping.values()}
            except Exception:
                pinned = set()
        if not pinned:
            return False
        selected = pinned
        current_ids: set[UUID] = set()
        for pdf_version_id in selected:
            version = session.get(DocumentVersion, pdf_version_id)
            if version is None:
                return True
            pdf_doc = session.get(Document, version.document_id)
            if pdf_doc is None:
                return True
            if pdf_doc.current_version_id is None:
                continue
            current_ids.add(pdf_doc.current_version_id)
        return set(selected) != set(current_ids)


    def catalog(self, project_id: UUID,
                *, archived: bool = False) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            documents = session.scalars(select(Document).where(
                Document.project_id == project_id,
                Document.archived == bool(archived)).order_by(
                Document.created_at, Document.id)).all()
            return [self._present_document(session, document) for document in documents]

    def load_document(self, document_id: UUID) -> dict[str, Any]:
        with Session(self._engine) as session:
            document = session.get(Document, document_id)
            if document is None:
                raise KeyError(str(document_id))
            return self._present_document(session, document)

    def update_document(
        self, document_id: UUID, *, title: str | None = None,
        archived: bool | None = None, current_version_id: UUID | None = ...,
    ) -> dict[str, Any]:
        with Session(self._engine) as session:
            document = session.get(Document, document_id)
            if document is None:
                raise KeyError(str(document_id))
            if title is not None:
                cleaned = str(title).strip()
                if not cleaned:
                    raise ValueError("title must not be blank")
                document.title = cleaned
            if archived is not None:
                document.archived = bool(archived)
            if current_version_id is not ...:
                if current_version_id is None:
                    document.current_version_id = None
                else:
                    version = session.get(DocumentVersion, current_version_id)
                    if version is None or version.document_id != document.id:
                        raise KeyError(str(current_version_id))
                    document.current_version_id = version.id
            session.commit()
            session.refresh(document)
            return self._present_document(session, document)

    def update_version_metadata(self, version_id: UUID,
                                fields: dict[str, str]) -> dict[str, Any]:
        allowed = {"construction_title", "raw_title", "document_code", "pd_reference"}
        cleaned: dict[str, str] = {}
        for key, value in dict(fields or {}).items():
            if key not in allowed:
                raise ValueError(f"unknown metadata field: {key}")
            text = str(value or "")
            if len(text) > 2048:
                raise ValueError(f"metadata field too long: {key}")
            cleaned[key] = text
        with Session(self._engine) as session:
            version = session.get(DocumentVersion, version_id)
            if version is None:
                raise KeyError(str(version_id))
            document = session.get(Document, version.document_id)
            if document is None:
                raise KeyError(str(version_id))
            overrides = dict(version.metadata_overrides or {})
            overrides.update(cleaned)
            version.metadata_overrides = overrides
            session.commit()
            session.refresh(version)
            return self._present_version(session, version, document)

    def record_format_status(self, version_id: UUID,
                             status: dict[str, Any]) -> None:
        """Persist the latest formatting verdict for one revision."""
        with Session(self._engine) as session:
            version = session.get(DocumentVersion, version_id)
            if version is None:
                raise KeyError(str(version_id))
            version.format_status = dict(status or {})
            session.commit()

    def persist_vor_run(
        self,
        *,
        vor_name: str,
        vor_source: Any,
        pdf_pool: dict[str, Any],
        tasks: list[dict[str, Any]],
        tracking: list[dict[str, Any]],
        results: list[dict[str, Any]],
        word_bytes: bytes,
        model_name: str,
        project_name: str = "default",
        pdf_sources: dict[str, Any] | None = None,
        provider: str = "Yandex Cloud (Qwen)",
    ) -> UUID:
        """Persist one VOR audit as a new run; returns the run id.

        Each upload gets an independent document identity; blobs dedupe by
        content address. VOR bytes are read without disturbing the caller's
        stream position.
        """
        project = self.get_or_create_project(project_name)
        vor_version = self.add_document_version(
            project.id, _read_source_bytes(vor_source), filename=vor_name, kind="vor",
            new_document=True,
        )
        pdf_versions: dict[str, DocumentVersionRow] = {}
        for key, doc in (pdf_pool or {}).items():
            source = (pdf_sources or {}).get(key)
            if source is None:
                pdf_bytes = bytes(doc.tobytes())
            else:
                pdf_bytes = _read_source_bytes(source)
            pdf_versions[key] = self.add_document_version(
                project.id, pdf_bytes, filename=key, kind="pdf",
                new_document=True,
            )
        run = self.create_run(
            project.id,
            vor_version.id,
            {
                "model": model_name,
                "provider": provider,
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
        word_bytes: bytes,
        pdf_versions: dict[str, DocumentVersionRow],
    ) -> None:
        """Write position results and the Word artifact into an open run.

        ``run_audit(..., run_id=...)`` reuses this for API-created runs so the
        stored payload shape stays identical across all entry points.
        """
        for task, track, result in zip(tasks, tracking, results):
            from audit_models import serialize_position_result
            payload = serialize_position_result(
                task, result, track, pdf_versions)
            self.save_position_result(run_id, int(task.get("idx", 0)), payload)
        self.save_run_report(run_id, "word", bytes(word_bytes))

    def load_version(self, version_id: UUID) -> VersionRef:
        """Load one document version with its per-version filename and kind."""
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
                filename=version.filename,
                kind=document.kind,
                version_no=version.version_no,
                sha256=version.sha256,
                blob_key=version.blob_key,
                size=version.size,
                metadata=dict(version.metadata_ or {}),
                metadata_overrides=dict(version.metadata_overrides or {}),
                format_status=dict(version.format_status or {}),
            )

    def _extract_metadata(self, data: bytes, filename: str, kind: str) -> dict[str, Any]:
        try:
            from document_metadata import extract_document_metadata
        except Exception:
            return {}
        try:
            result = extract_document_metadata(bytes(data), str(filename), str(kind))
        except Exception:
            return {}
        return dict(result or {})

    def _append_version(self, session, document: Document, data: bytes,
                        *, filename: str,
                        metadata: dict[str, Any] | None) -> DocumentVersionRow:
        from sqlalchemy import select as _select
        blob = self._blobs.put(bytes(data))
        existing = session.scalar(
            _select(DocumentVersion).where(
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
                filename=existing.filename,
                metadata=dict(existing.metadata_ or {}),
                metadata_overrides=dict(existing.metadata_overrides or {}),
            )
        version_no = (
            session.scalar(
                _select(
                    func.coalesce(func.max(DocumentVersion.version_no), 0)
                ).where(DocumentVersion.document_id == document.id)
            )
            or 0
        ) + 1
        extracted = (dict(metadata) if metadata is not None
                     else self._extract_metadata(bytes(data), filename, document.kind))
        version = DocumentVersion(
            document_id=document.id,
            filename=filename,
            version_no=version_no,
            sha256=blob.sha256,
            blob_key=blob.key,
            size=blob.size,
            metadata_=extracted,
            metadata_overrides={},
        )
        session.add(version)
        session.flush()
        # Auto-current rule: first PDF version becomes current; every new VOR
        # revision becomes current. Explicit PATCH may move it afterwards.
        if document.current_version_id is None or document.kind == "vor":
            document.current_version_id = version.id
        if not document.title:
            document.title = self._document_title(extracted, {}, filename)
        return DocumentVersionRow(
            id=version.id,
            document_id=document.id,
            version_no=version.version_no,
            sha256=version.sha256,
            blob_key=version.blob_key,
            size=version.size,
            filename=version.filename,
            metadata=dict(version.metadata_ or {}),
            metadata_overrides={},
        )

    def add_document_version(
        self, project_id: UUID, data: bytes, *, filename: str, kind: str,
        document_id: UUID | None = None, new_document: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> DocumentVersionRow:
        """Append a version. Default creates an independent document identity.

        ``document_id`` appends to that exact document (project/kind/archive
        validated); ``new_document=True`` forces a separate identity even for
        the same bytes. Same bytes in one document dedupe; blobs are shared.
        """
        payload = bytes(data)
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            if project.archived:
                raise ValueError("project is archived")
            document: Document | None = None
            if document_id is not None:
                document = session.get(Document, document_id, with_for_update=True)
                if document is None:
                    raise KeyError(str(document_id))
                if document.project_id != project_id:
                    raise ValueError("document belongs to another project")
                if document.kind != kind:
                    raise ValueError("document kind mismatch")
                if bool(document.archived):
                    raise ValueError("document is archived")
            if document is None:
                # New-document default: never auto-merge by filename. Internal
                # legacy callers pass explicit new_document=True as well; the
                # HTTP layer always sends document_id or new_document=true.
                if not new_document:
                    raise ValueError("explicit document_id or new_document required")
                document = Document(project_id=project_id, kind=kind, title="")
                session.add(document)
                session.flush()
            row = self._append_version(session, document, payload,
                                       filename=filename, metadata=metadata)
            session.commit()
            return row


    def delete_version(self, version_id: UUID) -> bool:
        """Delete one unused revision. Blobs are content-addressed and stay."""
        with Session(self._engine) as session:
            version = session.get(DocumentVersion, version_id)
            if version is None:
                raise KeyError(str(version_id))
            document = session.get(Document, version.document_id)
            if document is None:
                raise KeyError(str(version_id))
            usage = session.scalar(
                select(func.count()).select_from(Run).where(
                    Run.vor_version_id == version.id))
            if usage:
                raise LookupError("version is used by runs")
            pinned = session.scalar(
                select(func.count()).select_from(RunPdfVersion).where(
                    RunPdfVersion.version_id == version.id))
            if pinned:
                raise LookupError("version is pinned by run snapshots")
            if document.current_version_id == version.id:
                replacement = session.scalars(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.id != version.id,
                    ).order_by(DocumentVersion.version_no.desc())).first()
                document.current_version_id = (
                    replacement.id if replacement is not None else None)
            session.delete(version)
            session.commit()
            if not document.title:
                current = None
                if document.current_version_id is not None:
                    current = session.get(DocumentVersion,
                                          document.current_version_id)
                if current is not None:
                    document.title = self._document_title(
                        dict(current.metadata_ or {}),
                        dict(current.metadata_overrides or {}), current.filename)
                    session.commit()
            return True

    def delete_document(self, document_id: UUID) -> bool:
        """Hard-delete only when no version is referenced anywhere."""
        with Session(self._engine) as session:
            document = session.get(Document, document_id)
            if document is None:
                raise KeyError(str(document_id))
            versions = session.scalars(select(DocumentVersion).where(
                DocumentVersion.document_id == document.id)).all()
            for version in versions:
                usage = session.scalar(
                    select(func.count()).select_from(Run).where(
                        Run.vor_version_id == version.id))
                if usage:
                    raise LookupError("document is used by runs")
                pinned = session.scalar(
                    select(func.count()).select_from(RunPdfVersion).where(
                        RunPdfVersion.version_id == version.id))
                if pinned:
                    raise LookupError("document is pinned by run snapshots")
            for version in versions:
                session.delete(version)
            session.delete(document)
            session.commit()
            return True

    def delete_project(self, project_id: UUID) -> bool:
        """Hard-delete a project with every owned row.

        Runs, position jobs/results, artifacts and PDF pins go first
        (children cascade from runs); versions, documents and the project
        follow. Blobs are content-addressed and stay in the store.
        """
        with Session(self._engine) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise KeyError(str(project_id))
            documents = session.scalars(select(Document).where(
                Document.project_id == project.id)).all()
            runs = session.scalars(select(Run).where(
                Run.project_id == project.id)).all()
            for run in runs:
                for pin in session.scalars(select(RunPdfVersion).where(
                        RunPdfVersion.run_id == run.id)).all():
                    session.delete(pin)
                session.delete(run)
            session.flush()
            for document in documents:
                for version in session.scalars(select(DocumentVersion).where(
                        DocumentVersion.document_id == document.id)).all():
                    session.delete(version)
                session.delete(document)
            session.delete(project)
            session.commit()
            return True


    def create_run(
        self, project_id: UUID, vor_version_id: UUID, config: dict[str, Any],
        pdf_pins: dict[str, UUID] | None = None, *, background: bool = False,
    ) -> RunRow:
        with Session(self._engine) as session:
            vor = session.get(DocumentVersion, vor_version_id)
            if vor is None:
                raise KeyError(str(vor_version_id))
            document = session.get(Document, vor.document_id)
            if document is None or document.kind != "vor":
                raise ValueError("not a VOR version")
            if document.project_id != project_id:
                raise ValueError("version belongs to another project")
            run = Run(
                project_id=project_id,
                vor_version_id=vor_version_id,
                config=dict(config),
                status="running",
            )
            session.add(run)
            session.flush()
            pdf_blob_keys = {}
            for source_key, version_id in dict(pdf_pins or {}).items():
                pdf_version = session.get(DocumentVersion, version_id)
                if pdf_version is None:
                    raise KeyError(str(version_id))
                pdf_doc = session.get(Document, pdf_version.document_id)
                if pdf_doc is None or pdf_doc.kind != "pdf":
                    raise ValueError("pinned version is not a PDF")
                if pdf_doc.project_id != project_id:
                    raise ValueError("pinned version belongs to another project")
                pdf_blob_keys[source_key] = pdf_version.blob_key
                session.add(RunPdfVersion(run_id=run.id, source_key=str(source_key),
                                          version_id=pdf_version.id))
            if background:
                run.config = {**dict(run.config), "normative_after_match": os.environ.get("NORMATIVE_AUTO_ENABLED", "0") == "1"}
                self._enqueue(session, run.id, "prepare", "audit_tasks.prepare_run", {
                    "run_id": str(run.id), "project": str(config.get("project", "")),
                    "vor_filename": vor.filename, "vor_blob_key": vor.blob_key,
                    "pdf_blob_keys": pdf_blob_keys,
                    "request_id": str(config.get("request_id", "")),
                })
            session.commit()
            session.refresh(run)
            return RunRow(
                id=run.id,
                project_id=run.project_id,
                vor_version_id=run.vor_version_id,
                status=run.status,
                created_at=run.created_at,
            )

    def run_pinned_versions(self, run_id: UUID) -> dict[str, UUID]:
        with Session(self._engine) as session:
            return {str(row.source_key): row.version_id for row in session.scalars(
                select(RunPdfVersion).where(RunPdfVersion.run_id == run_id)).all()}

    def update_run_config(self, run_id: UUID, patch: dict[str, Any]) -> None:
        """Merge ``patch`` into the stored run config (metrics, timings)."""
        with Session(self._engine) as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
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
            if run.status != "running" or config.get("finalize_chained") or not config.get("prepare_complete"):
                session.commit()
                return False
            counts = dict(session.execute(
                select(PositionJob.status, func.count()).where(PositionJob.run_id == run_id)
                .group_by(PositionJob.status)
            ).all())
            expected = config.get("expected_positions", len(config.get("position_inputs") or []))
            if sum(counts.values()) != expected or any(
                count and status not in {"done", "failed"} for status, count in counts.items()
            ):
                return False
            unfinished = session.scalar(select(func.count()).select_from(TaskOutbox).where(
                TaskOutbox.run_id == run_id, TaskOutbox.task_name == "audit_tasks.audit_position",
                TaskOutbox.status != "done",
            ))
            if unfinished:
                return False
            config["finalize_chained"] = True
            run.config = config
            self._enqueue(session, run_id, "finalize", "audit_tasks.finalize_run", {
                "run_id": str(run_id), "request_id": str(config.get("request_id", "")),
            })
            session.commit()
            return True

    @staticmethod
    def _enqueue(session, run_id, task_key, task_name, payload):
        session.execute(insert(TaskOutbox).values(
            id=uuid4(), run_id=run_id, task_key=task_key, task_name=task_name,
            payload=dict(payload), status="pending", attempts=0,
        ).on_conflict_do_nothing(index_elements=["run_id", "task_key"]))

    def enqueue_task(self, task_name: str, payload: dict[str, Any]) -> None:
        run_id = UUID(str(payload["run_id"]))
        key = {"audit_tasks.prepare_run": "prepare", "audit_tasks.finalize_run": "finalize"}.get(
            task_name, f"{'normative' if task_name == 'audit_tasks.normative_position' else 'audit'}:{payload.get('position_index')}")
        with Session(self._engine) as session:
            self._enqueue(session, run_id, key, task_name, payload)
            session.commit()

    def run_status(self, run_id: UUID) -> str:
        with Session(self._engine) as session:
            status = session.scalar(select(Run.status).where(Run.id == run_id))
            if status is None:
                raise KeyError(str(run_id))
            return str(status)

    def resume_run(self, run_id: UUID, *, allow_running: bool = True) -> None:
        """Explicitly resume an interrupted audit, preserving completed results.

        Operators must stop old workers before adopting a pre-outbox run.
        Active durable leases are never stolen by this operation.
        """
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None:
                raise KeyError(str(run_id))
            allowed = {"failed", "cancelled"} | ({"running"} if allow_running else set())
            if run.status not in allowed:
                raise ValueError("Only interrupted audits can be resumed")
            active = session.scalar(select(func.count()).select_from(TaskOutbox).where(
                TaskOutbox.run_id == run_id, TaskOutbox.status.in_(["running", "dispatched"]),
                TaskOutbox.lease_until > now,
            ))
            if active:
                raise ValueError("An execution lease is still active; stop workers and wait for it to expire")
            session.execute(delete(TaskOutbox).where(TaskOutbox.run_id == run_id))
            config = dict(run.config or {})
            config.update(prepare_complete=False, finalize_chained=False)
            for field in ("prepare_seconds", "prepare_elapsed_seconds", "execution_error", "failed_task"):
                config.pop(field, None)
            run.config = config
            run.status = "running"
            run.finished_at = None
            vor = session.get(DocumentVersion, run.vor_version_id)
            pdf_keys = {
                pin.source_key: session.get(DocumentVersion, pin.version_id).blob_key
                for pin in session.scalars(select(RunPdfVersion).where(RunPdfVersion.run_id == run_id))
            }
            self._enqueue(session, run_id, "prepare", "audit_tasks.prepare_run", {
                "run_id": str(run_id), "project": str(config.get("project", "")),
                "vor_filename": vor.filename, "vor_blob_key": vor.blob_key,
                "pdf_blob_keys": pdf_keys, "request_id": str(config.get("request_id", "")),
            })
            for job in session.scalars(select(PositionJob).where(PositionJob.run_id == run_id)):
                if job.status == "done":
                    continue
                job.status, job.worker, job.error = "queued", None, None
                if job.queue == "llm":
                    self._enqueue(session, run_id, f"audit:{job.position_index}", "audit_tasks.audit_position", {
                        "run_id": str(run_id), "position_index": job.position_index,
                        "request_id": str(config.get("request_id", "")),
                    })
            session.commit()

    def reserve_deliveries(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Recover expired execution leases even when the worker hostname is alive."""
        now = datetime.now(timezone.utc)
        deliveries = []
        with Session(self._engine) as session:
            in_flight = session.scalar(select(func.count()).select_from(TaskOutbox).where(
                TaskOutbox.task_name != "audit_tasks.normative_position",
                TaskOutbox.status.in_(["dispatched", "running"]))) or 0
            normative_in_flight = session.scalar(select(func.count()).select_from(TaskOutbox).where(
                TaskOutbox.task_name == "audit_tasks.normative_position",
                TaskOutbox.status.in_(["dispatched", "running"]))) or 0
            eligible = select(TaskOutbox).where(
                TaskOutbox.status.in_(["pending", "dispatched", "running"]),
                TaskOutbox.available_at <= now,
                or_(TaskOutbox.lease_until.is_(None), TaskOutbox.lease_until < now),
            ).order_by(TaskOutbox.status == "pending", TaskOutbox.available_at)
            rows = list(session.scalars(eligible.where(TaskOutbox.task_name != "audit_tasks.normative_position")
                .limit(limit).with_for_update(skip_locked=True)).all())
            rows += list(session.scalars(eligible.where(TaskOutbox.task_name == "audit_tasks.normative_position")
                .limit(limit).with_for_update(skip_locked=True)).all())
            for row in rows:
                normative = row.task_name == "audit_tasks.normative_position"
                if row.status == "pending" and (normative_in_flight >= 2 if normative else in_flight >= 32):
                    continue
                run = session.get(Run, row.run_id)
                if run is None or run.status not in ({"running", "done"} if normative else {"running"}):
                    row.status = "cancelled"
                    continue
                if row.attempts >= (1 if normative else 3):
                    self._exhaust_task(session, row, run, "Execution lease expired; automatic retry limit reached")
                    continue
                if row.status == "pending":
                    if normative:
                        normative_in_flight += 1
                    else:
                        in_flight += 1
                if row.status != "dispatched":
                    row.delivery_token = uuid4()
                row.status = "dispatched"
                row.lease_until = now + timedelta(seconds=60)
                deliveries.append({"id": str(row.id), "token": str(row.delivery_token),
                                   "name": row.task_name, "payload": dict(row.payload)})
            session.commit()
        return deliveries

    def claim_delivery(self, task_id: UUID, token: UUID, *, timeout: int) -> bool:
        with Session(self._engine) as session:
            hint = session.get(TaskOutbox, task_id)
            if hint is None:
                return False
            run = session.scalar(select(Run).where(Run.id == hint.run_id).with_for_update())
            row = session.scalar(select(TaskOutbox).where(TaskOutbox.id == task_id)
                                 .with_for_update().execution_options(populate_existing=True))
            if row is None or row.status != "dispatched" or row.delivery_token != token:
                return False
            normative = row.task_name == "audit_tasks.normative_position"
            if run is None or run.status not in ({"running", "done"} if normative else {"running"}):
                row.status = "cancelled"
                session.commit()
                return False
            row.status = "running"
            row.attempts += 1
            row.lease_until = datetime.now(timezone.utc) + timedelta(seconds=timeout + 60)
            session.commit()
            return True

    @staticmethod
    def _exhaust_task(session, row, run, error):
        row.status = "failed"
        row.error = error[:1000]
        if row.task_name == "audit_tasks.normative_position":
            return  # An optional second layer never invalidates the matching run.
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.config = {**dict(run.config or {}), "execution_error": error[:1000], "failed_task": row.task_key}

    def settle_delivery(self, task_id: UUID, token: UUID, *, error: str | None = None,
                        continuation: bool = False) -> None:
        with Session(self._engine) as session:
            row = session.scalar(select(TaskOutbox).where(TaskOutbox.id == task_id).with_for_update())
            if row is None or row.delivery_token != token or row.status != "running":
                return
            row.lease_until = None
            row.error = error[:1000] if error else None
            if error and row.attempts >= (1 if row.task_name == "audit_tasks.normative_position" else 3):
                self._exhaust_task(session, row, session.get(Run, row.run_id), error)
            elif error or continuation:
                row.status = "pending"
                row.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(30, 2 ** row.attempts) if error else 0)
                if continuation:
                    row.attempts = 0
            else:
                row.status = "done"
            session.commit()

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

    def result_versions(self, run_id, index, *, limit=20):
        with Session(self._engine) as session:
            rows = session.scalars(select(PositionResultVersion).where(PositionResultVersion.run_id == run_id, PositionResultVersion.position_index == index).order_by(PositionResultVersion.created_at.desc(), PositionResultVersion.id.desc()).limit(limit)).all()
            return [dict(id=str(r.id), created_at=r.created_at, result=dict(r.result)) for r in rows]

    def save_review_artifact(self, run_id, index, kind, payload, actor):
        import hashlib
        import json
        fingerprint = hashlib.sha256(json.dumps([str(run_id), index, kind, payload, actor], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with Session(self._engine) as session:
            statement = insert(ReviewArtifact).values(id=uuid4(), run_id=run_id, position_index=index, kind=kind, payload=payload, actor=actor, fingerprint=fingerprint).on_conflict_do_nothing(index_elements=[ReviewArtifact.fingerprint])
            session.execute(statement)
            session.commit()
            row = session.scalar(select(ReviewArtifact).where(ReviewArtifact.fingerprint == fingerprint))
            return dict(id=str(row.id), kind=row.kind, actor=row.actor, created_at=row.created_at, **row.payload)

    def cached_normative_page(self, run_id, key, build):
        """Share frozen page extraction between positions; serialize only this page."""
        import hashlib
        fingerprint = hashlib.sha256(f"normative-page:{run_id}:{key}".encode()).hexdigest()
        with Session(self._engine) as session:
            session.execute(select(func.pg_advisory_xact_lock(int(fingerprint[:15], 16))))
            row = session.scalar(select(ReviewArtifact).where(ReviewArtifact.fingerprint == fingerprint))
            if row:
                return dict(row.payload)
            payload = build()
            if payload.get("origin") != "ocr_failed":
                session.add(ReviewArtifact(id=uuid4(), run_id=run_id, position_index=-1, kind="normative_page",
                    fingerprint=fingerprint, actor="normative-page-cache", payload=payload))
            session.commit()
            return payload

    def normative_stage(self, run_id, index):
        with Session(self._engine) as session:
            row = session.scalar(select(TaskOutbox).where(TaskOutbox.run_id == run_id,
                TaskOutbox.task_key == f"normative:{index}"))
            if not row:
                return None
            return dict(status=row.status, attempts=row.attempts,
                message="Нормативный этап прерван; автоматический платный повтор отключён." if row.status == "failed" else "")

    def schedule_local_normative_refresh(self, run_id, revision):
        """Versioned local-only upgrades reuse position slots; no new model calls."""
        now = datetime.now(timezone.utc)
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None or run.status not in {'running', 'done'} or not (run.config or {}).get('normative_after_match'):
                return 0
            # Serialize schedulers, without locking the run against worker commits.
            lock = int.from_bytes(run_id.bytes[:7], 'big')
            session.execute(select(func.pg_advisory_xact_lock(lock)))
            latest = session.execute(select(NormativeReview.position_index,
                NormativeReview.payload['position_check']['local_rules_version'].as_integer()).where(
                    NormativeReview.run_id == run_id, NormativeReview.kind == 'check')
                .distinct(NormativeReview.position_index)
                .order_by(NormativeReview.position_index, NormativeReview.created_at.desc(), NormativeReview.id.desc())).all()
            tasks = {t.task_key: t for t in session.scalars(select(TaskOutbox).where(
                TaskOutbox.run_id == run_id, TaskOutbox.task_name == 'audit_tasks.normative_position').with_for_update(skip_locked=True))}
            scheduled = 0
            for index, version in latest:
                if (version or 0) >= revision:
                    continue
                key = f'normative:{index}'
                row = tasks.get(key)
                if row and ((row.payload or {}).get('local_rules_version', 0) >= revision
                        or row.status in {'pending', 'running', 'dispatched'}):
                    continue
                payload = dict(run_id=str(run_id), position_index=index, local_only=True, local_rules_version=revision)
                if row:
                    row.payload, row.status, row.attempts = payload, 'pending', 0
                    row.available_at, row.lease_until, row.delivery_token, row.error = now, None, None, None
                else:
                    self._enqueue(session, run_id, key, 'audit_tasks.normative_position', payload)
                scheduled += 1
            session.commit()
            return scheduled

    def normative_validation_context(self, run_id):
        """Batch-read latest validation fields, without full OCR/source snapshots."""
        with Session(self._engine) as session:
            result: dict[int, dict[str, Any]] = {r.payload['position_index']: {'stage': r.status}
                for r in session.scalars(select(TaskOutbox).where(
                    TaskOutbox.run_id == run_id, TaskOutbox.task_name == 'audit_tasks.normative_position'))
                if type(r.payload.get('position_index')) is int and r.payload['position_index'] >= 0}
            rows = session.execute(select(NormativeReview.position_index,
                NormativeReview.payload['position_key'].as_string(),
                NormativeReview.payload['position_check']['items'],
                NormativeReview.payload['position_check']['summary'].as_string(),
                NormativeReview.payload['position_check']['parameter_cards'],
                NormativeReview.payload['position_check']['parameter_schema_version'],
                NormativeReview.payload['position_check']['local_rules_version'].as_integer()).where(
                    NormativeReview.run_id == run_id, NormativeReview.kind == 'check')
                .distinct(NormativeReview.position_index)
                .order_by(NormativeReview.position_index, NormativeReview.created_at.desc(), NormativeReview.id.desc()))
            for index, key, items, summary, cards, schema, revision in rows:
                result.setdefault(index, {})['review'] = dict(position_key=key, items=items or [], summary=summary or '',
                    parameter_cards=cards or [], parameter_schema_version=schema or 0, local_rules_version=revision or 0)
            return result

    def review_artifacts(self, run_id, index):
        with Session(self._engine) as session:
            rows = session.scalars(select(ReviewArtifact).where(ReviewArtifact.run_id == run_id, ReviewArtifact.position_index == index).order_by(ReviewArtifact.created_at.desc(), ReviewArtifact.id.desc()).limit(30)).all()
            return [dict(id=str(r.id), kind=r.kind, actor=r.actor, created_at=r.created_at, **r.payload) for r in rows]

    def load_pdf_bindings(self, version_id: UUID) -> dict[str, str]:
        with Session(self._engine) as session:
            row = session.get(DocumentVersion, version_id)
            if row is None:
                raise KeyError(str(version_id))
            return dict(row.pdf_bindings or {})

    def save_normative_review(self, run_id, index, kind, payload, actor):
        import hashlib
        import json
        fingerprint = hashlib.sha256(json.dumps([str(run_id), index, kind, payload, actor], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with Session(self._engine) as session:
            session.execute(insert(NormativeReview).values(id=uuid4(), run_id=run_id, position_index=index,
                kind=kind, payload=payload, actor=actor, fingerprint=fingerprint)
                .on_conflict_do_nothing(index_elements=[NormativeReview.fingerprint]))
            session.commit()
            row = session.scalar(select(NormativeReview).where(NormativeReview.fingerprint == fingerprint))
            return dict(id=str(row.id), kind=row.kind, actor=row.actor, created_at=row.created_at, payload=dict(row.payload))

    def normative_reviews(self, run_id, index):
        with Session(self._engine) as session:
            rows = [session.scalar(select(NormativeReview).where(NormativeReview.run_id == run_id,
                NormativeReview.position_index == index, NormativeReview.kind == kind)
                .order_by(NormativeReview.created_at.desc(), NormativeReview.id.desc()).limit(1))
                for kind in ("search", "selection", "check")]
            rows = [r for r in rows if r is not None]
            return [dict(id=str(r.id), kind=r.kind, actor=r.actor, created_at=r.created_at, payload=dict(r.payload)) for r in rows]

    def normative_review(self, run_id, index, review_id):
        with Session(self._engine) as session:
            row = session.scalar(select(NormativeReview).where(NormativeReview.id == review_id,
                NormativeReview.run_id == run_id, NormativeReview.position_index == index))
            if row is None:
                raise KeyError("Нормативный поиск не найден для этой позиции")
            return dict(id=str(row.id), kind=row.kind, actor=row.actor, created_at=row.created_at, payload=dict(row.payload))

    def save_pdf_bindings(self, version_id: UUID, bindings: dict[str, str]) -> None:
        with Session(self._engine) as session:
            row = session.get(DocumentVersion, version_id)
            if row is None:
                raise KeyError(str(version_id))
            row.pdf_bindings = dict(bindings)
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
            run = session.get(Run, run_id)
            if status == "done" and run and run.config.get("normative_after_match"):
                self._enqueue(session, run_id, f"normative:{position_index}", "audit_tasks.normative_position", {
                    "run_id": str(run_id), "position_index": position_index,
                })
            session.commit()

    def requeue_position_job(
        self,
        run_id: UUID,
        position_index: int,
        *,
        queue: str,
        payload: dict[str, Any], durable: bool = False,
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
            if durable:
                self._enqueue(session, run_id, f"audit:{position_index}", "audit_tasks.audit_position", {
                    "run_id": str(run_id), "position_index": position_index,
                    "request_id": str(payload.get("request_id", "")),
                })
            session.commit()

    def list_position_jobs(self, run_id: UUID, *, include_results: bool = True) -> list[PositionJobRow]:
        """List all job rows of a run ordered by position index."""
        with Session(self._engine) as session:
            if not include_results:
                rows = session.execute(select(
                    PositionJob.id, PositionJob.run_id, PositionJob.position_index,
                    PositionJob.queue, PositionJob.status, PositionJob.attempts,
                    PositionJob.worker, PositionJob.payload,
                ).where(PositionJob.run_id == run_id).order_by(PositionJob.position_index)).mappings()
                return [PositionJobRow(**dict(row)) for row in rows]
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
            page, DocumentVersion.filename, jobs.c.queue, jobs.c.status.label("job_status"),
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
            if records:
                for run_key, task_status, count in session.execute(select(
                    TaskOutbox.run_id, TaskOutbox.status, func.count()).where(
                    TaskOutbox.run_id.in_(list(records)), TaskOutbox.task_name == "audit_tasks.normative_position"
                ).group_by(TaskOutbox.run_id, TaskOutbox.status)):
                    records[run_key].setdefault("normative_counts", {})[task_status] = count
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

    def load_run(self, run_id: UUID, *, include_results: bool = True) -> dict[str, Any]:
        with Session(self._engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(str(run_id))
            version = session.get(DocumentVersion, run.vor_version_id)
            results = session.scalars(
                select(PositionResult)
                .where(PositionResult.run_id == run_id)
                .order_by(PositionResult.position_index)
            ).all() if include_results else []
            return {
                "id": run.id,
                "project_id": run.project_id,
                "status": run.status,
                "config": dict(run.config or {}),
                "vor_version_id": run.vor_version_id,
                "vor_filename": version.filename if version else "",
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


def open_repository(database_url: str, blob_store: BlobStore, *, migrate_schema: bool = True) -> Repository:
    """Open the repository; migrations run only where Alembic is available."""
    if migrate_schema:
        migrate(database_url)
    return Repository(database_url, blob_store)
