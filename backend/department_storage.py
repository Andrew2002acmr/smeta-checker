"""Project department lifecycle and lightweight aggregate counters."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from departments import DEFAULT_DEPARTMENTS
from storage_models import Department, Document, DocumentVersion, Project, Run



def seed_departments(session: Session, project_id: UUID) -> None:
    for order, (name, icon) in enumerate(DEFAULT_DEPARTMENTS):
        session.add(Department(project_id=project_id, name=name, icon=icon, sort_order=order, is_system=True))


class DepartmentRepositoryMixin:
    _engine: Engine

    @staticmethod
    def _department_payload(session: Session, row: Department) -> dict[str, Any]:
        counts = dict(session.execute(select(Document.kind, func.count()).where(
            Document.department_id == row.id, Document.archived.is_(False),
        ).group_by(Document.kind)).all())
        runs, running = session.execute(select(func.count(Run.id), func.count(Run.id).filter(Run.status == "running"))
            .join(DocumentVersion, Run.vor_version_id == DocumentVersion.id)
            .join(Document, DocumentVersion.document_id == Document.id)
            .where(Document.department_id == row.id)).one()
        return dict(id=str(row.id), project_id=str(row.project_id), name=row.name, icon=row.icon,
                    is_system=bool(row.is_system), created_at=row.created_at,
                    vor_count=counts.get("vor", 0), pdf_count=counts.get("pdf", 0),
                    run_count=runs, running_count=running)

    def list_departments(self, project_id: UUID) -> list[dict[str, Any]]:
        with Session(self._engine) as session:
            rows = session.scalars(select(Department).where(
                Department.project_id == project_id,
            ).order_by(Department.sort_order, Department.created_at, Department.id)).all()
            return [self._department_payload(session, row) for row in rows]

    def load_department(self, project_id: UUID, department_id: UUID) -> dict[str, Any]:
        with Session(self._engine) as session:
            row = session.get(Department, department_id)
            if row is None or row.project_id != project_id:
                raise KeyError(str(department_id))
            return self._department_payload(session, row)

    def create_department(self, project_id: UUID, name: str) -> dict[str, Any]:
        with Session(self._engine) as session:
            project = session.get(Project, project_id, with_for_update=True)
            if project is None:
                raise KeyError(str(project_id))
            if project.archived:
                raise ValueError("Сначала восстановите проект из архива")
            name = name.strip()
            if not name:
                raise ValueError("Введите название отдела")
            if session.scalar(select(Department.id).where(Department.project_id == project_id, func.lower(Department.name) == name.lower())):
                raise ValueError("Отдел с таким названием уже есть в проекте")
            order = session.scalar(select(func.coalesce(func.max(Department.sort_order), -1)).where(Department.project_id == project_id))
            row = Department(project_id=project_id, name=name, icon="folder-open", sort_order=int(order) + 1, is_system=False)
            session.add(row)
            session.commit()
            return self._department_payload(session, row)

    def update_department(self, project_id: UUID, department_id: UUID, *, name: str) -> dict[str, Any]:
        with Session(self._engine) as session:
            project = session.get(Project, project_id, with_for_update=True)
            row = session.get(Department, department_id, with_for_update=True)
            if project is None or row is None or row.project_id != project_id:
                raise KeyError(str(department_id))
            if project.archived:
                raise ValueError("Сначала восстановите проект из архива")
            name = name.strip()
            if not name:
                raise ValueError("Введите название отдела")
            if session.scalar(select(Department.id).where(Department.project_id == project_id, Department.id != department_id, func.lower(Department.name) == name.lower())):
                raise ValueError("Отдел с таким названием уже есть в проекте")
            row.name = name
            session.commit()
            return self._department_payload(session, row)

    def delete_department(self, project_id: UUID, department_id: UUID) -> dict[str, Any]:
        with Session(self._engine) as session:
            row = session.get(Department, department_id, with_for_update=True)
            project = session.get(Project, project_id)
            if row is None or row.project_id != project_id or project is None:
                raise KeyError(str(department_id))
            if project.archived:
                raise ValueError("Сначала восстановите проект из архива")
            if row.is_system:
                raise ValueError("Системный отдел удалить нельзя")
            if session.scalar(select(Document.id).where(Document.department_id == row.id).limit(1)):
                raise ValueError("Можно удалить только пустой отдел. Сначала удалите его документы.")
            payload = self._department_payload(session, row)
            session.delete(row)
            session.commit()
            return payload

    def version_department_id(self, version_id: UUID) -> UUID:
        with Session(self._engine) as session:
            value = session.scalar(select(Document.department_id).join(DocumentVersion, DocumentVersion.document_id == Document.id).where(DocumentVersion.id == version_id))
            if value is None:
                raise KeyError(str(version_id))
            return value
