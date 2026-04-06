from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.user import User
from app.schemas.annotation_set import (
    AnnotationSetCreate,
    AnnotationSetListResponse,
    AnnotationSetLinkRequest,
    AnnotationSetMountListResponse,
    AnnotationSetMountRead,
    AnnotationSetMountRequest,
    AnnotationSetMountUpdate,
    AnnotationSetProjectLinkRead,
    AnnotationSetRead,
    AnnotationSetUpdate,
)
from app.services.annotation_set_service import AnnotationSetService

router = APIRouter(prefix="/annotation-sets", tags=["annotation-sets"])
project_router = APIRouter(prefix="/projects/{project_id}/annotation-sets", tags=["annotation-sets"])
map_router = APIRouter(prefix="/maps/{map_id}/annotation-sets", tags=["annotation-sets"])


@router.get("", response_model=AnnotationSetListResponse)
async def list_annotation_sets(
    source_type: str | None = Query(default=None),
    schema_id: UUID | None = Query(default=None),
    dataset_id: UUID | None = Query(default=None),
    model_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_sets(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        source_type=source_type,
        schema_id=schema_id,
        dataset_id=dataset_id,
        model_id=model_id,
    )
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{set_id}", response_model=AnnotationSetRead)
async def get_annotation_set(
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    return await service.get_set(set_id, organization_id=org_id)


@router.post("", response_model=AnnotationSetRead, status_code=status.HTTP_201_CREATED)
async def create_annotation_set(
    payload: AnnotationSetCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
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
    set_id: UUID,
    payload: AnnotationSetUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    annotation_set = await service.update_set(set_id, payload, organization_id=org_id)
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
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.delete_set(set_id, organization_id=org_id)
    await log_audit_event(
        action="annotation_sets.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="annotation_set",
        entity_id=str(set_id),
        session=db,
    )


@project_router.get("", response_model=AnnotationSetListResponse)
async def list_project_annotation_sets(
    project_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_project_sets(project_id, org_id, limit, offset)
    return AnnotationSetListResponse(items=items, total=total, limit=limit, offset=offset)


@project_router.post("/link", response_model=AnnotationSetProjectLinkRead)
async def link_annotation_set_to_project(
    project_id: UUID,
    payload: AnnotationSetLinkRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    link = await service.link_set_to_project(
        project_id=project_id,
        annotation_set_id=payload.annotation_set_id,
        organization_id=org_id,
        linked_by=current_user.id,
    )
    await log_audit_event(
        action="annotation_sets.project_link",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project_annotation_set",
        entity_id=f"{project_id}:{payload.annotation_set_id}",
        session=db,
    )
    return AnnotationSetProjectLinkRead.model_validate(link)


@project_router.delete("/{set_id}/unlink", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_annotation_set_from_project(
    project_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.unlink_set_from_project(project_id, set_id, org_id)
    await log_audit_event(
        action="annotation_sets.project_unlink",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="project_annotation_set",
        entity_id=f"{project_id}:{set_id}",
        session=db,
    )


@map_router.get("", response_model=AnnotationSetMountListResponse)
async def list_map_annotation_set_mounts(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    items, total = await service.list_map_mounts(map_id, org_id)
    return AnnotationSetMountListResponse(
        items=[AnnotationSetMountRead.model_validate(item) for item in items],
        total=total,
    )


@map_router.post("/mount", response_model=AnnotationSetMountRead)
async def mount_annotation_set_on_map(
    map_id: UUID,
    payload: AnnotationSetMountRequest,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    mount = await service.mount_set_on_map(map_id, payload, org_id)
    await log_audit_event(
        action="annotation_sets.map_mount",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_annotation_set",
        entity_id=f"{map_id}:{payload.annotation_set_id}",
        session=db,
    )
    return AnnotationSetMountRead.model_validate(mount)


@map_router.patch("/{set_id}", response_model=AnnotationSetMountRead)
async def update_map_annotation_set_mount(
    map_id: UUID,
    set_id: UUID,
    payload: AnnotationSetMountUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    mount = await service.update_map_mount(map_id, set_id, payload, org_id)
    await log_audit_event(
        action="annotation_sets.map_mount_update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_annotation_set",
        entity_id=f"{map_id}:{set_id}",
        session=db,
    )
    return AnnotationSetMountRead.model_validate(mount)


@map_router.delete("/{set_id}/unmount", status_code=status.HTTP_204_NO_CONTENT)
async def unmount_annotation_set_from_map(
    map_id: UUID,
    set_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = AnnotationSetService(db)
    await service.unmount_set_from_map(map_id, set_id, org_id)
    await log_audit_event(
        action="annotation_sets.map_unmount",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_annotation_set",
        entity_id=f"{map_id}:{set_id}",
        session=db,
    )
