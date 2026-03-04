from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import limit_param, offset_param
from app.db.session import get_db
from app.schemas.project import ProjectCreate, ProjectListResponse, ProjectRead, ProjectUpdate
from app.services.project_service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    organization_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    db: AsyncSession = Depends(get_db),
):
    service = ProjectService(db)
    items, total = await service.list_projects(
        limit=limit,
        offset=offset,
        organization_id=organization_id,
    )
    return ProjectListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project_by_id(project_id: UUID, db: AsyncSession = Depends(get_db)):
    service = ProjectService(db)
    return await service.get_project(project_id)


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
    service = ProjectService(db)
    return await service.create_project(payload)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project_by_id(
    project_id: UUID,
    payload: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
):
    service = ProjectService(db)
    return await service.update_project(project_id, payload)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_by_id(project_id: UUID, db: AsyncSession = Depends(get_db)):
    service = ProjectService(db)
    await service.delete_project(project_id)
