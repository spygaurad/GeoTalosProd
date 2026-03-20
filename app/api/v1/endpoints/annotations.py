from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation import (
    AnnotationCreate,
    AnnotationListResponse,
    AnnotationRead,
    AnnotationUpdate,
)
from app.services.annotation_service import AnnotationService

router = APIRouter(prefix="/annotation-sets/{set_id}/annotations", tags=["annotations"])


@router.get("", response_model=AnnotationListResponse)
async def list_annotations(
    set_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    items, total = await service.list_annotations(
        set_id=set_id, limit=limit, offset=offset, organization_id=org_id
    )
    return AnnotationListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{annotation_id}", response_model=AnnotationRead)
async def get_annotation(
    set_id: UUID,
    annotation_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    return await service.get_annotation(annotation_id, organization_id=org_id, set_id=set_id)


@router.post("", response_model=AnnotationRead, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    set_id: UUID,
    payload: AnnotationCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    annotation = await service.create_annotation(set_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotations.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation.id),
        session=db,
    )
    return annotation


@router.patch("/{annotation_id}", response_model=AnnotationRead)
async def update_annotation(
    set_id: UUID,
    annotation_id: UUID,
    payload: AnnotationUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    annotation = await service.update_annotation(
        annotation_id, payload, organization_id=org_id, set_id=set_id
    )
    await log_audit_event(
        action="annotations.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation_id),
        session=db,
    )
    return annotation


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    set_id: UUID,
    annotation_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    await service.delete_annotation(annotation_id, organization_id=org_id, set_id=set_id)
    await log_audit_event(
        action="annotations.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation_id),
        session=db,
    )
