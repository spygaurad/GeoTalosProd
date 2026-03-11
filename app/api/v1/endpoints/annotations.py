from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation import AnnotationCreate, AnnotationListResponse, AnnotationRead, AnnotationUpdate
from app.services.annotation_service import AnnotationService

router = APIRouter(prefix="/annotations", tags=["annotations"])


@router.get("", response_model=AnnotationListResponse)
async def list_annotations(
    dataset_item_id: UUID | None = Query(default=None),
    stac_item_id: str | None = Query(default=None),
    track_id: UUID | None = Query(default=None),
    label: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    items, total = await service.list_annotations(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        dataset_item_id=dataset_item_id,
        stac_item_id=stac_item_id,
        track_id=track_id,
        label=label,
        status=status,
    )
    return AnnotationListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{annotation_id}", response_model=AnnotationRead)
async def get_annotation_by_id(
    annotation_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    return await service.get_annotation(annotation_id, organization_id=org_id)


@router.post("", response_model=AnnotationRead, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    payload: AnnotationCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    if payload.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = AnnotationService(db)
    annotation = await service.create_annotation(payload)
    log_audit_event(
        action="annotations.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation.id),
        extra={"dataset_item_id": str(payload.dataset_item_id) if payload.dataset_item_id else None},
    )
    return annotation


@router.patch("/{annotation_id}", response_model=AnnotationRead)
async def update_annotation_by_id(
    annotation_id: UUID,
    payload: AnnotationUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    annotation = await service.update_annotation(annotation_id, payload, organization_id=org_id)
    log_audit_event(
        action="annotations.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation_id),
        extra={"dataset_item_id": str(payload.dataset_item_id) if payload.dataset_item_id else None},
    )
    return annotation


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_by_id(
    annotation_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationService(db)
    await service.delete_annotation(annotation_id, organization_id=org_id)
    log_audit_event(
        action="annotations.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation",
        entity_id=str(annotation_id),
    )
