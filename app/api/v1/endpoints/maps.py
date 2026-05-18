from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from shapely.geometry import box, shape
from sqlalchemy import distinct, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.api.v1.endpoints.jobs import _create_inference_job
from app.config import settings
from app.core.audit import log_audit_event
from app.core.deps import limit_param, offset_param
from app.models.annotation import Annotation
from app.models.annotation_set import AnnotationSet
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.map_layer import MapLayer
from app.models.user import User
from app.schemas.dataset import DatasetListResponse
from app.schemas.dataset_item import DatasetItemListResponse
from app.schemas.job import JobRead
from app.schemas.map import (
    MapAOIResourcesRead,
    MapCreate,
    MapInferenceCreate,
    MapListResponse,
    MapRead,
    MapUpdate,
)
from app.services.map_service import MapService
from app.services.project_service import ProjectService
from app.services import titiler_service

router = APIRouter(prefix="/maps", tags=["maps"])


def _parse_bbox(bbox: str) -> list[float]:
    try:
        parts = [float(v.strip()) for v in bbox.split(",")]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bbox must be comma-separated numbers") from exc
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be minx,miny,maxx,maxy")
    minx, miny, maxx, maxy = parts
    if minx >= maxx or miny >= maxy:
        raise HTTPException(status_code=400, detail="bbox must have minx < maxx and miny < maxy")
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise HTTPException(status_code=400, detail="bbox must be within EPSG:4326 bounds")
    return parts


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _geometry_intersects_bbox(geometry: dict | None, bbox_4326: list[float]) -> bool:
    if not geometry:
        return False
    try:
        return shape(geometry).intersects(box(*bbox_4326))
    except Exception:
        return False


async def _org_collection_ids(db: AsyncSession, org_id: UUID) -> set[str]:
    result = await db.execute(
        select(Dataset.stac_collection_id).where(
            Dataset.organization_id == org_id,
            Dataset.stac_collection_id.is_not(None),
            Dataset.deleted_at.is_(None),
        )
    )
    return {row for (row,) in result.all()}


@router.get("", response_model=MapListResponse)
async def list_maps(
    project_id: UUID | None = Query(default=None),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    items, total = await service.list_maps(
        limit=limit,
        offset=offset,
        organization_id=org_id,
        project_id=project_id,
    )
    return MapListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{map_id}", response_model=MapRead)
async def get_map_by_id(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    return await service.get_map(map_id, organization_id=org_id)


@router.post("", response_model=MapRead, status_code=status.HTTP_201_CREATED)
async def create_map(
    payload: MapCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    project_service = ProjectService(db)
    project = await project_service.get_project(payload.project_id, organization_id=org_id)
    if project.organization_id != org_id:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    service = MapService(db)
    map_row = await service.create_map(payload, created_by=current_user.id)
    await log_audit_event(
        action="maps.create",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_row.id),
        session=db,
    )
    return map_row


@router.patch("/{map_id}", response_model=MapRead)
async def update_map_by_id(
    map_id: UUID,
    payload: MapUpdate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    map_row = await service.update_map(map_id, payload, organization_id=org_id)
    await log_audit_event(
        action="maps.update",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_id),
        session=db,
    )
    return map_row


@router.delete("/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_map_by_id(
    map_id: UUID,
    org_id: UUID = Depends(require_org_role("org:admin")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    await service.delete_map(map_id, organization_id=org_id)
    await log_audit_event(
        action="maps.delete",
        actor_id=str(current_user.id),
        organization_id=str(org_id),
        entity="map",
        entity_id=str(map_id),
        session=db,
    )


@router.get("/{map_id}/datasets", response_model=DatasetListResponse)
async def list_map_datasets(
    map_id: UUID,
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    await service.get_map(map_id, organization_id=org_id)

    base_query = (
        select(Dataset)
        .join(MapLayer, MapLayer.dataset_id == Dataset.id)
        .where(
            MapLayer.map_id == map_id,
            Dataset.organization_id == org_id,
            Dataset.deleted_at.is_(None),
        )
        .distinct()
    )
    count_query = (
        select(distinct(Dataset.id))
        .join(MapLayer, MapLayer.dataset_id == Dataset.id)
        .where(
            MapLayer.map_id == map_id,
            Dataset.organization_id == org_id,
            Dataset.deleted_at.is_(None),
        )
    )

    rows = await db.scalars(
        base_query.order_by(Dataset.created_at.desc()).limit(limit).offset(offset)
    )
    total = len((await db.execute(count_query)).all())
    return DatasetListResponse(items=rows.all(), total=total, limit=limit, offset=offset)


@router.get("/{map_id}/datasets/{dataset_id}/items/in-aoi", response_model=DatasetItemListResponse)
async def list_map_dataset_items_in_aoi(
    map_id: UUID,
    dataset_id: UUID,
    bbox: str = Query(..., description="minx,miny,maxx,maxy in EPSG:4326"),
    limit: int = Depends(limit_param),
    offset: int = Depends(offset_param),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    bbox_4326 = _parse_bbox(bbox)
    service = MapService(db)
    await service.get_map(map_id, organization_id=org_id)

    dataset_ids = set(
        (
            await db.execute(
                select(MapLayer.dataset_id).where(
                    MapLayer.map_id == map_id,
                    MapLayer.dataset_id.is_not(None),
                )
            )
        ).scalars().all()
    )
    if dataset_id not in dataset_ids:
        raise HTTPException(status_code=404, detail="Dataset not found on this map")

    items = (
        await db.execute(
            select(DatasetItem).where(
                DatasetItem.dataset_id == dataset_id,
                DatasetItem.organization_id == org_id,
                DatasetItem.is_active.is_(True),
            )
        )
    ).scalars().all()
    matched = [item for item in items if _geometry_intersects_bbox(item.geometry, bbox_4326)]
    page = matched[offset : offset + limit]
    return DatasetItemListResponse(items=page, total=len(matched), limit=limit, offset=offset)


@router.get("/{map_id}/aoi/resources", response_model=MapAOIResourcesRead)
async def list_map_aoi_resources(
    map_id: UUID,
    bbox: str = Query(..., description="minx,miny,maxx,maxy in EPSG:4326"),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    bbox_4326 = _parse_bbox(bbox)
    service = MapService(db)
    await service.get_map(map_id, organization_id=org_id)
    allowed_collections = await _org_collection_ids(db, org_id)

    stac_features: list[dict] = []
    stac_collection_ids: list[str] = []
    if allowed_collections:
        search_body = {
            "collections": list(allowed_collections),
            "bbox": bbox_4326,
            "limit": 500,
        }
        async with httpx.AsyncClient(base_url=settings.STAC_API_URL, timeout=30.0) as client:
            try:
                resp = await client.post("/search", json=search_body)
            except httpx.RequestError as exc:
                raise HTTPException(status_code=503, detail="STAC service is unavailable") from exc
        if resp.status_code >= 500:
            raise HTTPException(status_code=502, detail="STAC service returned an error")
        stac_body = resp.json()
        stac_features = stac_body.get("features", []) or []
        stac_collection_ids = sorted(
            {
                str(feature.get("collection"))
                for feature in stac_features
                if feature.get("collection")
            }
        )

    local_items = (
        await db.execute(
            select(DatasetItem).where(
                DatasetItem.organization_id == org_id,
                DatasetItem.is_active.is_(True),
                DatasetItem.stac_item_id.in_([feature.get("id") for feature in stac_features])
                if stac_features
                else false(),
            )
        )
    ).scalars().all()
    local_items_by_stac_id = {item.stac_item_id: item for item in local_items}
    dataset_items = list(local_items_by_stac_id.values())
    dataset_ids = {item.dataset_id for item in dataset_items}

    datasets = []
    if dataset_ids:
        datasets = (
            await db.execute(
                select(Dataset).where(
                    Dataset.id.in_(dataset_ids),
                    Dataset.organization_id == org_id,
                    Dataset.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    elif stac_collection_ids:
        datasets = (
            await db.execute(
                select(Dataset).where(
                    Dataset.organization_id == org_id,
                    Dataset.deleted_at.is_(None),
                    Dataset.stac_collection_id.in_(stac_collection_ids),
                )
            )
        ).scalars().all()

    stac_items = []
    for feature in stac_features:
        stac_id = str(feature.get("id"))
        local_item = local_items_by_stac_id.get(stac_id)
        stac_items.append(
            {
                "id": stac_id,
                "collection_id": feature.get("collection"),
                "bbox": feature.get("bbox"),
                "geometry": feature.get("geometry"),
                "properties": feature.get("properties") or {},
                "dataset_item_id": str(local_item.id) if local_item else None,
                "dataset_id": str(local_item.dataset_id) if local_item else None,
                "s3_uri": local_item.s3_uri if local_item else None,
                "filename": local_item.filename if local_item else None,
            }
        )

    raster_candidates = (
        await db.execute(
            select(AnnotationSet).where(
                AnnotationSet.organization_id == org_id,
                AnnotationSet.deleted_at.is_(None),
                AnnotationSet.raster_config.is_not(None),
            )
        )
    ).scalars().all()
    raster_mask_annotation_sets = [
        aset
        for aset in raster_candidates
        if _bbox_intersects((aset.raster_config or {}).get("bounds_4326") or [0, 0, 0, 0], bbox_4326)
    ]

    vector_annotation_sets: list[AnnotationSet] = []
    vector_set_ids = (
        await db.execute(
            select(distinct(Annotation.annotation_set_id))
            .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
            .where(
                Annotation.deleted_at.is_(None),
                AnnotationSet.organization_id == org_id,
                AnnotationSet.deleted_at.is_(None),
                func.ST_Intersects(
                    Annotation.geometry,
                    func.ST_MakeEnvelope(
                        bbox_4326[0], bbox_4326[1], bbox_4326[2], bbox_4326[3], 4326
                    ),
                ),
            )
        )
    ).scalars().all()
    if vector_set_ids:
        vector_annotation_sets = (
            await db.execute(
                select(AnnotationSet).where(
                    AnnotationSet.id.in_(vector_set_ids),
                    AnnotationSet.organization_id == org_id,
                    AnnotationSet.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    return MapAOIResourcesRead(
        bbox=bbox_4326,
        datasets=datasets,
        dataset_items=dataset_items,
        stac_collection_ids=stac_collection_ids,
        stac_items=stac_items,
        vector_annotation_sets=vector_annotation_sets,
        raster_mask_annotation_sets=raster_mask_annotation_sets,
    )


@router.get("/{map_id}/datasets/{dataset_id}/preview")
async def preview_map_dataset_in_aoi(
    map_id: UUID,
    dataset_id: UUID,
    bbox: str = Query(..., description="minx,miny,maxx,maxy in EPSG:4326"),
    width: int = Query(default=512, ge=64, le=2048),
    height: int = Query(default=512, ge=64, le=2048),
    format: str = Query(default="png", pattern="^(png|jpeg|webp)$"),
    assets: str | None = Query(default=None),
    rescale: str | None = Query(default=None),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    bbox_4326 = _parse_bbox(bbox)
    service = MapService(db)
    await service.get_map(map_id, organization_id=org_id)

    dataset = await db.scalar(
        select(Dataset).join(MapLayer, MapLayer.dataset_id == Dataset.id).where(
            Dataset.id == dataset_id,
            Dataset.organization_id == org_id,
            Dataset.deleted_at.is_(None),
            MapLayer.map_id == map_id,
        )
    )
    if dataset is None or not dataset.stac_collection_id:
        raise HTTPException(status_code=404, detail="Dataset not found on this map")

    try:
        content = await titiler_service.get_collection_preview(
            dataset.stac_collection_id,
            assets=assets,
            bbox=bbox_4326,
            width=width,
            height=height,
            format=format,
            rescale=rescale,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(content=content, media_type=f"image/{format}")


@router.get("/{map_id}/datasets/{dataset_id}/items/{item_id}/preview")
async def preview_map_dataset_item_in_aoi(
    map_id: UUID,
    dataset_id: UUID,
    item_id: UUID,
    bbox: str = Query(..., description="minx,miny,maxx,maxy in EPSG:4326"),
    width: int = Query(default=512, ge=64, le=2048),
    height: int = Query(default=512, ge=64, le=2048),
    format: str = Query(default="png", pattern="^(png|jpeg|webp)$"),
    assets: str | None = Query(default=None),
    rescale: str | None = Query(default=None),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
):
    bbox_4326 = _parse_bbox(bbox)
    service = MapService(db)
    await service.get_map(map_id, organization_id=org_id)

    dataset = await db.scalar(
        select(Dataset).join(MapLayer, MapLayer.dataset_id == Dataset.id).where(
            Dataset.id == dataset_id,
            Dataset.organization_id == org_id,
            Dataset.deleted_at.is_(None),
            MapLayer.map_id == map_id,
        )
    )
    item = await db.scalar(
        select(DatasetItem).where(
            DatasetItem.id == item_id,
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    if dataset is None or item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found on this map")

    try:
        content = await titiler_service.get_item_bbox_preview(
            item.stac_collection_id,
            item.stac_item_id,
            bbox=bbox_4326,
            width=width,
            height=height,
            format=format,
            assets=assets,
            rescale=rescale,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return Response(content=content, media_type=f"image/{format}")


@router.post("/{map_id}/inference", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
async def create_map_inference_job(
    map_id: UUID,
    payload: MapInferenceCreate,
    org_id: UUID = Depends(require_org_role("org:member")),
    db: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    service = MapService(db)
    map_row = await service.get_map(map_id, organization_id=org_id)
    inference_payload = payload.to_inference_job(map_id=map_id)
    if inference_payload.project_id is None:
        inference_payload.project_id = map_row.project_id
    return await _create_inference_job(
        inference_payload,
        org_id=org_id,
        db=db,
        current_user=current_user,
    )
