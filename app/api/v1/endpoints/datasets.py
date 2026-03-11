from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.dataset import DatasetCreate, DatasetListResponse, DatasetRead, DatasetUpdate
from app.services.dataset_service import DatasetService

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=DatasetListResponse)
async def list_datasets(
    organization_id: UUID | None = Query(default=None),
    project_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    if organization_id is not None and organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = DatasetService(db)
    items, total = await service.list_datasets(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        project_id=project_id,
        status=status,
    )
    return DatasetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{dataset_id}", response_model=DatasetRead)
async def get_dataset_by_id(
    dataset_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    return await service.get_dataset(dataset_id, organization_id=org_id)


@router.post("", response_model=DatasetRead, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    payload: DatasetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = DatasetService(db)
    dataset = await service.create_dataset(payload)
    log_audit_event(
        action="datasets.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset.id),
        extra={"project_id": str(payload.project_id) if payload.project_id else None},
    )
    return dataset


@router.patch("/{dataset_id}", response_model=DatasetRead)
async def update_dataset_by_id(
    dataset_id: UUID,
    payload: DatasetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    dataset = await service.update_dataset(dataset_id, payload, organization_id=org_id)
    log_audit_event(
        action="datasets.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset_id),
        extra={"project_id": str(payload.project_id) if payload.project_id else None},
    )
    return dataset


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset_by_id(
    dataset_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = DatasetService(db)
    await service.delete_dataset(dataset_id, organization_id=org_id)
    log_audit_event(
        action="datasets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="dataset",
        entity_id=str(dataset_id),
    )
