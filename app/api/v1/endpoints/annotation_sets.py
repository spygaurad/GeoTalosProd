from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetListResponse,
    AnnotationSetRead,
    AnnotationSetUpdate,
)
from app.services.annotation_set_service import AnnotationSetService

router = APIRouter(prefix="/maps/{map_id}/annotation-sets", tags=["annotation-sets"])


@router.get("", response_model=AnnotationSetListResponse)
async def list_annotation_sets(
    map_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_sets(
        map_id=map_id, limit=limit, offset=offset, organization_id=org_id
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{set_id}", response_model=AnnotationSetRead)
async def get_annotation_set(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    return await service.get_set(set_id, organization_id=org_id, map_id=map_id)


@router.post("", response_model=AnnotationSetRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_set(
    map_id: UUID,
    payload: AnnotationSetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.map_id != map_id:
        raise HTTPException(status_code=400, detail="map_id in payload must match path")
    service = AnnotationSetService(db)
    annotation_set = await service.create_set(
        payload,
        organization_id=org_id,
        created_by_user_id=current_user.id,
    )
    await log_audit_event(
        action="annotation_sets.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(annotation_set.id),
        session=db,
    )
    return annotation_set


@router.patch("/{set_id}", response_model=AnnotationSetRead)
async def update_annotation_set(
    map_id: UUID,
    set_id: UUID,
    payload: AnnotationSetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.update_set(
        set_id, payload, organization_id=org_id, map_id=map_id
    )
    await log_audit_event(
        action="annotation_sets.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )
    return annotation_set


@router.delete("/{set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_set(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.delete_set(set_id, organization_id=org_id, map_id=map_id)
    await log_audit_event(
        action="annotation_sets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )
