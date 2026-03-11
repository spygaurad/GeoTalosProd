from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_role, get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.project_member import (
    ProjectMemberCreate,
    ProjectMemberListResponse,
    ProjectMemberRead,
    ProjectMemberUpdate,
)
from app.services.project_member_service import ProjectMemberService

router = APIRouter(prefix="/project-members", tags=["project-members"])


@router.get("", response_model=ProjectMemberListResponse)
async def list_project_members(
    project_id: UUID | None = Query(default=None),
    user_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    role: str = Depends(get_current_role),
):
    if role != "org:admin":
        if user_id is not None and user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        user_id = current_user.id
    service = ProjectMemberService(db)
    items, total = await service.list_project_members(
        limit=limit,
        offset=offset,
        project_id=project_id,
        user_id=user_id,
        organization_id=org_id,
    )
    return ProjectMemberListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{project_id}/{user_id}", response_model=ProjectMemberRead)
async def get_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = ProjectMemberService(db)
    return await service.get_project_member(project_id, user_id, organization_id=org_id)


@router.post("", response_model=ProjectMemberRead, status_code=status.HTTP_201_CREATED)
async def create_project_member(
    payload: ProjectMemberCreate,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ProjectMemberService(db)
    member = await service.create_project_member(payload)
    log_audit_event(
        action="project_members.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project_member",
        entity_id=f"{member.project_id}:{member.user_id}",
    )
    return member


@router.patch("/{project_id}/{user_id}", response_model=ProjectMemberRead)
async def update_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    payload: ProjectMemberUpdate,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ProjectMemberService(db)
    member = await service.update_project_member(
        project_id, user_id, payload, organization_id=org_id
    )
    log_audit_event(
        action="project_members.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project_member",
        entity_id=f"{project_id}:{user_id}",
    )
    return member


@router.delete("/{project_id}/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = ProjectMemberService(db)
    await service.delete_project_member(project_id, user_id, organization_id=org_id)
    log_audit_event(
        action="project_members.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project_member",
        entity_id=f"{project_id}:{user_id}",
    )
