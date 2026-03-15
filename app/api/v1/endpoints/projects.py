from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectListResponse, ProjectRead, ProjectUpdate
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    organization_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    if organization_id is not None and organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = ProjectService(db)
    items, total = await service.list_projects(
        limit=limit,
        offset=offset,
        organization_id=org_id,
    )
    return ProjectListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project_by_id(
    project_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    return await service.get_project(project_id, organization_id=org_id)


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = ProjectService(db)
    project = await service.create_project(payload)
    await log_audit_event(
        action="projects.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project",
        entity_id=str(project.id),
        session=db,
    )
    return project


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project_by_id(
    project_id: UUID,
    payload: ProjectUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    project = await service.update_project(project_id, payload, organization_id=org_id)
    await log_audit_event(
        action="projects.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project",
        entity_id=str(project_id),
        session=db,
    )
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_by_id(
    project_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ProjectService(db)
    await service.delete_project(project_id, organization_id=org_id)
    await log_audit_event(
        action="projects.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project",
        entity_id=str(project_id),
        session=db,
    )
