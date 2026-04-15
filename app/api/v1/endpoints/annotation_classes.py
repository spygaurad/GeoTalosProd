from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation_class import (
    AnnotationClassCreate,
    AnnotationClassListResponse,
    AnnotationClassRead,
    AnnotationClassUpdate,
    ClassStyleUpsert,
)
from app.services.annotation_class_service import AnnotationClassService

schema_router = APIRouter(prefix="/annotation-schemas", tags=["annotation-classes"])
router = APIRouter(prefix="/annotation-classes", tags=["annotation-classes"])


@schema_router.get("/{schema_id}/classes", response_model=AnnotationClassListResponse)
async def list_annotation_classes(
    schema_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    items, total = await service.list_classes(
        schema_id=schema_id,
        limit=limit,
        offset=offset,
        organization_id=org_id,
    )
    return AnnotationClassListResponse(items=items, total=total, limit=limit, offset=offset)


@schema_router.post(
    "/{schema_id}/classes",
    response_model=AnnotationClassRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_annotation_class(
    schema_id: UUID,
    payload: AnnotationClassCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    cls = await service.create_class(schema_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotation_classes.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_class",
        entity_id=str(cls.id),
        session=db,
    )
    return cls


@schema_router.patch(
    "/{schema_id}/classes/{class_id}/style",
    response_model=AnnotationClassRead,
    summary="Upsert the style for an annotation class",
    description=(
        "Creates a new Style record if the class has no style, "
        "or merges the provided definition into the existing Style."
    ),
)
async def upsert_annotation_class_style(
    schema_id: UUID,
    class_id: UUID,
    payload: ClassStyleUpsert,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    cls = await service.upsert_style(
        schema_id=schema_id,
        class_id=class_id,
        payload=payload,
        organization_id=org_id,
    )
    await log_audit_event(
        action="annotation_classes.style_upsert",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_class",
        entity_id=str(class_id),
        session=db,
    )
    return cls


@router.get("/{class_id}", response_model=AnnotationClassRead)
async def get_annotation_class(
    class_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    return await service.get_class(class_id, organization_id=org_id)


@router.patch("/{class_id}", response_model=AnnotationClassRead)
async def update_annotation_class(
    class_id: UUID,
    payload: AnnotationClassUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    cls = await service.update_class(class_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotation_classes.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_class",
        entity_id=str(class_id),
        session=db,
    )
    return cls


@router.delete("/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_class(
    class_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationClassService(db)
    await service.delete_class(class_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_classes.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_class",
        entity_id=str(class_id),
        session=db,
    )
