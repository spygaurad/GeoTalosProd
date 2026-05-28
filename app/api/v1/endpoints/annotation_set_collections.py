from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation_set import AnnotationSetListResponse
from app.schemas.annotation_set_collection import (
    AnnotationSetCollectionCreate,
    AnnotationSetCollectionItemCreate,
    AnnotationSetCollectionListResponse,
    AnnotationSetCollectionRead,
    AnnotationSetCollectionUpdate,
)
from app.services.annotation_set_collection_service import AnnotationSetCollectionService

router = APIRouter(prefix="/annotation-set-collections", tags=["annotation-set-collections"])


@router.get("", response_model=AnnotationSetCollectionListResponse)
async def list_annotation_set_collections(
    schema_id: UUID | None = None,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetCollectionService(db)
    items, total = await service.list_collections(
        organization_id=org_id,
        limit=limit,
        offset=offset,
        schema_id=schema_id,
    )
    return AnnotationSetCollectionListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=AnnotationSetCollectionRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_set_collection(
    payload: AnnotationSetCollectionCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    collection = await AnnotationSetCollectionService(db).create_collection(
        payload,
        organization_id=org_id,
        created_by=current_user.id,
    )
    await log_audit_event(
        action="annotation_set_collections.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set_collection",
        entity_id=str(collection.id),
        session=db,
    )
    return collection


@router.get("/{collection_id}", response_model=AnnotationSetCollectionRead)
async def get_annotation_set_collection(
    collection_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    return await AnnotationSetCollectionService(db).get_collection(collection_id, organization_id=org_id)


@router.patch("/{collection_id}", response_model=AnnotationSetCollectionRead)
async def update_annotation_set_collection(
    collection_id: UUID,
    payload: AnnotationSetCollectionUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    collection = await AnnotationSetCollectionService(db).update_collection(
        collection_id,
        payload,
        organization_id=org_id,
    )
    await log_audit_event(
        action="annotation_set_collections.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set_collection",
        entity_id=str(collection_id),
        session=db,
    )
    return collection


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation_set_collection(
    collection_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await AnnotationSetCollectionService(db).delete_collection(collection_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_set_collections.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set_collection",
        entity_id=str(collection_id),
        session=db,
    )


@router.get("/{collection_id}/annotation-sets", response_model=AnnotationSetListResponse)
async def list_annotation_sets_in_collection(
    collection_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    items, total = await AnnotationSetCollectionService(db).list_collection_sets(
        collection_id,
        organization_id=org_id,
        limit=limit,
        offset=offset,
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "/{collection_id}/annotation-sets",
    status_code=status.HTTP_201_CREATED,
)
async def add_annotation_set_to_collection(
    collection_id: UUID,
    payload: AnnotationSetCollectionItemCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    link = await AnnotationSetCollectionService(db).add_set_to_collection(
        collection_id,
        payload.annotation_set_id,
        organization_id=org_id,
        linked_by=current_user.id,
    )
    await log_audit_event(
        action="annotation_set_collections.add_annotation_set",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set_collection",
        entity_id=str(collection_id),
        session=db,
    )
    return {
        "collection_id": str(link.collection_id),
        "annotation_set_id": str(link.annotation_set_id),
        "linked_at": link.linked_at,
        "linked_by": str(link.linked_by) if link.linked_by else None,
    }


@router.delete("/{collection_id}/annotation-sets/{annotation_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_annotation_set_from_collection(
    collection_id: UUID,
    annotation_set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await AnnotationSetCollectionService(db).remove_set_from_collection(
        collection_id,
        annotation_set_id,
        organization_id=org_id,
    )
    await log_audit_event(
        action="annotation_set_collections.remove_annotation_set",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set_collection",
        entity_id=str(collection_id),
        session=db,
    )
