"""Authenticated department lifecycle routes."""

from uuid import UUID

from fastapi import Depends, Query
from sqlalchemy.exc import IntegrityError

from workspace_models import DepartmentCreate, DepartmentDTO, DepartmentUpdate


def department_scope(context, project_id: UUID, raw: str | None) -> UUID | None:
    if not raw:
        return None
    from api import ApiError, _parse_uuid

    value = _parse_uuid(raw, field="department_id")
    try:
        context.repository.load_department(project_id, value)
    except KeyError as exc:
        raise ApiError(404, "department_not_found", "Отдел не найден в этом проекте") from exc
    return value


def register_department_routes(router, guarded, project):
    from api import ApiError

    def call(operation):
        try:
            return operation()
        except KeyError as exc:
            raise ApiError(404, "department_not_found", "Отдел не найден в этом проекте") from exc
        except ValueError as exc:
            raise ApiError(409, "department_conflict", str(exc)) from exc
        except IntegrityError as exc:
            raise ApiError(409, "department_conflict", "Отдел с таким названием уже существует") from exc

    base = "/api/v1/projects/{project_id}/departments"

    @router.get(base, response_model=list[DepartmentDTO])
    def departments(project_id: str, archived: bool = Query(False), context=Depends(guarded)):
        owner = project(context, project_id)
        return context.repository.list_departments(owner.id, archived=archived)

    @router.post(base, response_model=DepartmentDTO, status_code=201)
    def create_department(project_id: str, payload: DepartmentCreate, context=Depends(guarded)):
        owner = project(context, project_id)
        return call(lambda: context.repository.create_department(owner.id, payload.name))

    @router.get(base + "/{department_id}", response_model=DepartmentDTO)
    def department(project_id: str, department_id: str, context=Depends(guarded)):
        owner = project(context, project_id)
        value = department_scope(context, owner.id, department_id)
        return call(lambda: context.repository.load_department(owner.id, value))

    @router.patch(base + "/{department_id}", response_model=DepartmentDTO)
    def update_department(project_id: str, department_id: str, payload: DepartmentUpdate, context=Depends(guarded)):
        owner = project(context, project_id)
        value = department_scope(context, owner.id, department_id)
        return call(lambda: context.repository.update_department(owner.id, value, name=payload.name, archived=payload.archived))

    @router.delete(base + "/{department_id}", response_model=DepartmentDTO)
    def delete_department(project_id: str, department_id: str, context=Depends(guarded)):
        owner = project(context, project_id)
        value = department_scope(context, owner.id, department_id)
        return call(lambda: context.repository.delete_department(owner.id, value))
