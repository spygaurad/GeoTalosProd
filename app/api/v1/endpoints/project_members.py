from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import limit_param, offset_param
from app.db.session import get_db
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
    db: AsyncSession = Depends(get_db),
):
    service = ProjectMemberService(db)
    items, total = await service.list_project_members(
        limit=limit,
        offset=offset,
        project_id=project_id,
        user_id=user_id,
    )
    return ProjectMemberListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{project_id}/{user_id}", response_model=ProjectMemberRead)
async def get_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    service = ProjectMemberService(db)
    return await service.get_project_member(project_id, user_id)


@router.post("", response_model=ProjectMemberRead, status_code=status.HTTP_201_CREATED)
async def create_project_member(
    payload: ProjectMemberCreate, db: AsyncSession = Depends(get_db)
):
    service = ProjectMemberService(db)
    return await service.create_project_member(payload)


@router.patch("/{project_id}/{user_id}", response_model=ProjectMemberRead)
async def update_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    payload: ProjectMemberUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = ProjectMemberService(db)
    return await service.update_project_member(project_id, user_id, payload)


@router.delete("/{project_id}/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_member_by_id(
    project_id: UUID,
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    service = ProjectMemberService(db)
    await service.delete_project_member(project_id, user_id)
