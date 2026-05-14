from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.api.v1.endpoints.jobs import _create_inference_job
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.dataset_item import DatasetItem
from app.models.user import User
from app.schemas.job import JobRead
from app.schemas.map_aoi import (
    MapAOICreate,
    MapAOIInferenceCreate,
    MapAOIListResponse,
    MapAOIRead,
    MapAOIRenderConfig,
    MapAOISelectionConfig,
    MapAOITileJSONRequest,
    MapAOITimelineManifestRead,
    MapAOITimelineRead,
    MapAOIUpdate,
)
from app.services import titiler_service
from app.services.aoi_timeline_service import AOITimelineService
from app.services.map_aoi_service import MapAOIService

router = APIRouter(prefix="/maps/{map_id}/aois", tags=["map-aois"])


def _uuid_list(values: list | None) -> list[UUID]:
    return [v if isinstance(v, UUID) else UUID(str(v)) for v in (values or [])]


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _geometry_intersects_bbox(geometry: dict | None, bbox_4326: list[float]) -> bool:
    if not geometry:
        return False
    from shapely.geometry import box, shape

    try:
        return shape(geometry).intersects(box(*bbox_4326))
    except Exception:
        return False


async def _resolve_aoi_dataset_items(
    *,
    db: AsyncSession,
    org_id: UUID,
    aoi,
) -> list[DatasetItem]:
    selection = aoi.selection_config or {}
    dataset_item_ids = _uuid_list(selection.get("dataset_item_ids"))
    dataset_ids = _uuid_list(selection.get("dataset_ids"))

    query = select(DatasetItem).where(
        DatasetItem.organization_id == org_id,
        DatasetItem.is_active.is_(True),
    )
    if dataset_item_ids:
        query = query.where(DatasetItem.id.in_(dataset_item_ids))
    elif dataset_ids:
        query = query.where(DatasetItem.dataset_id.in_(dataset_ids))
    else:
        return []

    items = (await db.execute(query)).scalars().all()
    return [item for item in items if _geometry_intersects_bbox(item.geometry, aoi.bbox_4326)]


@router.get("", response_model=MapAOIListResponse)
async def list_map_aois(
    map_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapAOIService(db)
    items, total = await service.list_aois(
        map_id, organization_id=org_id, limit=limit, offset=offset
    )
    return MapAOIListResponse(items=items, total=total, limit=limit, offset=offset)


@router.post("", response_model=MapAOIRead, status_code=status.HTTP_201_CREATED)
async def create_map_aoi(
    map_id: UUID,
    payload: MapAOICreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapAOIService(db)
    aoi = await service.create_aoi(
        map_id,
        payload,
        organization_id=org_id,
        created_by=current_user.id,
    )
    await log_audit_event(
        action="map_aois.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_aoi",
        entity_id=str(aoi.id),
        session=db,
    )
    return aoi


@router.get("/{aoi_id}", response_model=MapAOIRead)
async def get_map_aoi(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    return await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)


@router.patch("/{aoi_id}", response_model=MapAOIRead)
async def update_map_aoi(
    map_id: UUID,
    aoi_id: UUID,
    payload: MapAOIUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).update_aoi(
        map_id, aoi_id, payload, organization_id=org_id
    )
    await log_audit_event(
        action="map_aois.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_aoi",
        entity_id=str(aoi_id),
        session=db,
    )
    return aoi


@router.delete("/{aoi_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_map_aoi(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    await MapAOIService(db).delete_aoi(map_id, aoi_id, organization_id=org_id)
    await log_audit_event(
        action="map_aois.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_aoi",
        entity_id=str(aoi_id),
        session=db,
    )


@router.get("/{aoi_id}/selection", response_model=MapAOISelectionConfig)
async def get_map_aoi_selection(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)
    return MapAOISelectionConfig.model_validate(aoi.selection_config or {})


@router.patch("/{aoi_id}/selection", response_model=MapAOIRead)
async def update_map_aoi_selection(
    map_id: UUID,
    aoi_id: UUID,
    payload: MapAOISelectionConfig,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).update_aoi(
        map_id,
        aoi_id,
        MapAOIUpdate(selection_config=payload),
        organization_id=org_id,
    )
    await log_audit_event(
        action="map_aois.selection_update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_aoi",
        entity_id=str(aoi_id),
        session=db,
    )
    return aoi


@router.get("/{aoi_id}/rendering", response_model=MapAOIRenderConfig)
async def get_map_aoi_rendering(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)
    return MapAOIRenderConfig.model_validate(aoi.render_config or {})


@router.patch("/{aoi_id}/rendering", response_model=MapAOIRead)
async def update_map_aoi_rendering(
    map_id: UUID,
    aoi_id: UUID,
    payload: MapAOIRenderConfig,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).update_aoi(
        map_id,
        aoi_id,
        MapAOIUpdate(render_config=payload),
        organization_id=org_id,
    )
    await log_audit_event(
        action="map_aois.rendering_update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map_aoi",
        entity_id=str(aoi_id),
        session=db,
    )
    return aoi


@router.get("/{aoi_id}/timeline", response_model=MapAOITimelineRead)
async def get_map_aoi_timeline(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)
    items = await _resolve_aoi_dataset_items(db=db, org_id=org_id, aoi=aoi)
    items.sort(key=lambda item: (item.item_datetime is None, item.item_datetime, item.created_at))
    return MapAOITimelineRead(aoi_id=aoi.id, bbox_4326=aoi.bbox_4326, dataset_items=items)


@router.post("/{aoi_id}/timeline/prepare", response_model=MapAOITimelineManifestRead)
async def prepare_map_aoi_timeline(
    map_id: UUID,
    aoi_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)
    items = await _resolve_aoi_dataset_items(db=db, org_id=org_id, aoi=aoi)
    items.sort(key=lambda item: (item.item_datetime is None, item.item_datetime, item.created_at))
    payload = AOITimelineService.build_manifest_payload(
        aoi_id=aoi.id,
        bbox_4326=aoi.bbox_4326,
        render_config=aoi.render_config,
        dataset_items=items,
    )
    key = AOITimelineService.manifest_key(
        aoi_id=aoi.id,
        bbox_4326=aoi.bbox_4326,
        render_config=aoi.render_config,
        dataset_item_ids=[item.id for item in items],
    )
    AOITimelineService.store_manifest(key, payload)
    return MapAOITimelineManifestRead(
        aoi_id=aoi.id,
        manifest_key=key,
        frame_count=payload["frame_count"],
        bbox_4326=aoi.bbox_4326,
        render_config=aoi.render_config,
        frames=payload["frames"],
    )


@router.post("/{aoi_id}/tilejson")
async def get_map_aoi_tilejson(
    map_id: UUID,
    aoi_id: UUID,
    payload: MapAOITileJSONRequest,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    aoi = await MapAOIService(db).get_aoi(map_id, aoi_id, organization_id=org_id)
    items = await _resolve_aoi_dataset_items(db=db, org_id=org_id, aoi=aoi)
    if not items:
        raise HTTPException(status_code=422, detail="AOI selection has no dataset items to tile")

    stac_item_ids = [item.stac_item_id for item in items]
    collection_ids = list({item.stac_collection_id for item in items})

    render_config = aoi.render_config or {}
    assets = payload.assets or render_config.get("assets")
    preset = payload.preset or render_config.get("preset")
    rescale = payload.rescale or render_config.get("rescale")
    asset_bidx = payload.asset_bidx or render_config.get("asset_bidx")

    searchid = await titiler_service.register_item_mosaic(stac_item_ids, collection_ids)
    tilejson = await titiler_service.get_mosaic_tilejson(
        searchid,
        assets=assets,
        preset=preset,
        rescale=rescale,
        asset_bidx=asset_bidx,
    )

    return {
        **tilejson,
        "aoi_id": str(aoi.id),
        "bbox_4326": aoi.bbox_4326,
        "item_ids": [str(item.id) for item in items],
    }


@router.post("/{aoi_id}/inference", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def create_map_aoi_inference_job(
    map_id: UUID,
    aoi_id: UUID,
    payload: MapAOIInferenceCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapAOIService(db)
    map_row = await service.get_map_for_org(map_id, org_id)
    aoi = await service.get_aoi(map_id, aoi_id, organization_id=org_id)
    selection = aoi.selection_config or {}

    if payload.dataset_item_ids:
        items = (
            await db.execute(
                select(DatasetItem).where(
                    DatasetItem.id.in_(payload.dataset_item_ids),
                    DatasetItem.organization_id == org_id,
                    DatasetItem.is_active.is_(True),
                )
            )
        ).scalars().all()
    elif payload.scope == "dataset":
        if payload.dataset_id is None:
            raise HTTPException(status_code=422, detail="dataset_id is required for dataset scope")
        items = (
            await db.execute(
                select(DatasetItem).where(
                    DatasetItem.dataset_id == payload.dataset_id,
                    DatasetItem.organization_id == org_id,
                    DatasetItem.is_active.is_(True),
                )
            )
        ).scalars().all()
    else:
        selected_item_ids = _uuid_list(selection.get("dataset_item_ids"))
        selected_dataset_ids = _uuid_list(selection.get("dataset_ids"))
        if not selected_item_ids and not selected_dataset_ids:
            raise HTTPException(status_code=422, detail="AOI selection has no datasets or items")
        items = await _resolve_aoi_dataset_items(db=db, org_id=org_id, aoi=aoi)

    if payload.scope == "aoi":
        items = [item for item in items if _geometry_intersects_bbox(item.geometry, aoi.bbox_4326)]

    if not items:
        raise HTTPException(status_code=422, detail="No dataset items available for this AOI inference run")

    inference_payload = payload.to_inference_job(
        dataset_item_ids=[item.id for item in items],
        map_id=map_id,
        project_id=map_row.project_id,
        aoi_bbox=aoi.bbox_4326 if payload.scope == "aoi" else None,
    )
    return await _create_inference_job(
        inference_payload,
        org_id=org_id,
        db=db,
        current_user=current_user,
    )
