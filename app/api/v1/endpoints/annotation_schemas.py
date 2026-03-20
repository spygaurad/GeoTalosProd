from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation_schema import (
    AnnotationSchemaCreate,
    AnnotationSchemaListResponse,
    AnnotationSchemaRead,
    AnnotationSchemaUpdate,
)
from app.services.annotation_schema_service import AnnotationSchemaService

router = APIRouter(prefix="/annotation-schemas", tags=["annotation-schemas"])


@router.get("", response_model=AnnotationSchemaListResponse)
async def list_annotation_schemas(
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSchemaService(db)
    items, total = await service.list_schemas(limit=limit, offset=offset, organization_id=org_id)
    return AnnotationSchemaListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{schema_id}", response_model=AnnotationSchemaRead)
async def get_annotation_schema(
    schema_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSchemaService(db)
    return await service.get_schema(schema_id, organization_id=org_id)


@router.post("", response_model=AnnotationSchemaRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_schema(
    payload: AnnotationSchemaCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSchemaService(db)
    schema = await service.create_schema(payload, organization_id=org_id, created_by=current_user.id)
    await log_audit_event(
        action="annotation_schemas.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_schema",
        entity_id=str(schema.id),
        session=db,
    )
    return schema


@router.patch("/{schema_id}", response_model=AnnotationSchemaRead)
async def update_annotation_schema(
    schema_id: UUID,
    payload: AnnotationSchemaUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSchemaService(db)
    schema = await service.update_schema(schema_id, payload, organization_id=org_id)
    await log_audit_event(
        action="annotation_schemas.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_schema",
        entity_id=str(schema_id),
        session=db,
    )
    return schema


@router.delete("/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_schema(
    schema_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSchemaService(db)
    await service.delete_schema(schema_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_schemas.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_schema",
        entity_id=str(schema_id),
        session=db,
    )
