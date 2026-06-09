import uuid

import sqlalchemy
from app.automation.registry import node, HandleDef


def _parse_bbox_4326(raw_bbox: list[float]) -> list[float]:
    if len(raw_bbox) != 4:
        raise ValueError("aoi_bbox must contain [minx, miny, maxx, maxy]")
    minx, miny, maxx, maxy = [float(v) for v in raw_bbox]
    if minx >= maxx or miny >= maxy:
        raise ValueError("aoi_bbox must have minx < maxx and miny < maxy")
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise ValueError("aoi_bbox must be within EPSG:4326 bounds")
    return [minx, miny, maxx, maxy]


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


def _serialize_dataset(dataset) -> dict:
    return {
        "id": str(dataset.id),
        "name": dataset.name,
        "stac_collection_id": dataset.stac_collection_id,
    }


def _serialize_dataset_item(item) -> dict:
    return {
        "id": str(item.id),
        "dataset_id": str(item.dataset_id),
        "stac_item_id": item.stac_item_id,
        "geometry": item.geometry,
    }


def _uuid_list(values: list | None) -> list[uuid.UUID]:
    return [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in (values or [])]


@node(
    type="select_data_source",
    category="data_source",
    label="Select Data Source",
    description=(
        "Unified data picker. Choose a Map, a Dataset, specific Dataset Items, "
        "and/or a saved AOI — each level below the first is optional. Outputs a "
        "consolidated data object with items, dataset info, and AOI context ready to "
        "wire into Run Inference, map overlays, or exports."
    ),
    # Three discrete outputs whose handle names match the keys returned by the
    # executor (the engine wires inputs via output_data[edge.sourceHandle]) and
    # whose types match downstream consumers:
    #   dataset    → overlay_dataset_on_map / analysis nodes (type "dataset")
    #   items      → Run Inference items input (type "dataset_items")
    #   selection  → Run Inference aoi input (type "map_selection"; carries aoi_bbox)
    # `dataset` is only populated when a single dataset is picked, so it's optional.
    outputs=[
        HandleDef(handle="dataset", type="dataset", label="Dataset", required=False),
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="selection", type="map_selection", label="AOI / Selection", required=False),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {
                "type": "string",
                "format": "uuid",
                "title": "Map",
                "description": "Optional. Scope to a map; required when picking a saved AOI.",
                "x-picker": "map",
            },
            "dataset_id": {
                "type": "string",
                "format": "uuid",
                "title": "Dataset",
                "description": "Optional. Leave empty to use every dataset attached to the map.",
                "x-picker": "dataset",
            },
            "item_ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "title": "Specific Items",
                "description": "Optional. Pick individual items; leave empty to use the whole dataset.",
                "x-picker": "dataset_items",
                "x-picker-depends-on": "dataset_id",
                "default": [],
            },
            "aoi_id": {
                "type": "string",
                "format": "uuid",
                "title": "Saved AOI",
                "description": "Optional. Filter items to a saved AOI's area on the chosen map.",
                "x-picker": "map_aoi",
                "x-picker-depends-on": "map_id",
            },
            "limit": {"type": "integer", "title": "Max Items", "default": 1000},
        },
    },
    icon="database",
    color="#3B82F6",
)
def execute_select_data_source(session, config, input_data, **kwargs):
    """Resolve a hierarchical Map / Dataset / Items / AOI selection.

    Supersedes select_dataset, select_dataset_items, select_map_datasets,
    select_map_dataset_items_in_aoi, and select_saved_map_aoi. Each level below
    the first is optional:

    * dataset only                  → all (or specific) items in that dataset
    * dataset + item_ids            → just those items
    * dataset + aoi                 → items in the dataset that fall in the AOI
    * map only                      → every dataset on the map and their items
    * map + aoi (no dataset)        → map items in the AOI (honours the AOI's
                                      saved item selection when present)

    Emits `dataset` (when a single dataset is chosen), `items`, and a
    `selection` map_selection carrying the AOI bbox.
    """
    from sqlalchemy import select

    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem
    from app.models.map import Map
    from app.models.map_aoi import MapAOI
    from app.models.map_layer import MapLayer

    map_id = config.get("map_id")
    dataset_id = config.get("dataset_id")
    item_ids = config.get("item_ids") or []
    aoi_id = config.get("aoi_id")
    limit = config.get("limit") or 1000

    if not dataset_id and not map_id:
        raise ValueError("Select at least a dataset or a map")

    map_id_uuid = uuid.UUID(map_id) if map_id else None
    if map_id_uuid and not session.get(Map, map_id_uuid):
        raise ValueError(f"Map {map_id} not found")

    # Resolve the AOI (bbox + persisted selection) when one is chosen.
    aoi = None
    bbox_4326 = None
    saved_cfg: dict = {}
    if aoi_id:
        if not map_id_uuid:
            raise ValueError("Pick a map before selecting a saved AOI")
        aoi = session.get(MapAOI, uuid.UUID(aoi_id))
        if not aoi or aoi.map_id != map_id_uuid or aoi.deleted_at is not None:
            raise ValueError(f"Saved AOI {aoi_id} not found on map {map_id}")
        bbox_4326 = _parse_bbox_4326(aoi.bbox_4326)
        saved_cfg = aoi.selection_config or {}

    # Resolve which datasets to draw items from.
    if dataset_id:
        dataset_id_uuid = uuid.UUID(dataset_id)
        dataset = session.get(Dataset, dataset_id_uuid)
        if not dataset or dataset.deleted_at is not None:
            raise ValueError(f"Dataset {dataset_id} not found")
        if map_id_uuid:
            on_map = session.execute(
                select(MapLayer.id).where(
                    MapLayer.map_id == map_id_uuid,
                    MapLayer.dataset_id == dataset_id_uuid,
                )
            ).scalar_one_or_none()
            if on_map is None:
                raise ValueError(f"Dataset {dataset_id} is not attached to map {map_id}")
        datasets = [dataset]
    else:
        datasets = session.execute(
            select(Dataset)
            .join(MapLayer, MapLayer.dataset_id == Dataset.id)
            .where(MapLayer.map_id == map_id_uuid, Dataset.deleted_at.is_(None))
            .distinct()
            .order_by(Dataset.created_at.desc())
        ).scalars().all()

    dataset_uuids = [d.id for d in datasets]

    # Pull candidate items.
    items_payload: list[dict] = []
    if dataset_uuids:
        stmt = select(DatasetItem).where(
            DatasetItem.dataset_id.in_(dataset_uuids),
            DatasetItem.is_active.is_(True),
        )
        if item_ids:
            stmt = stmt.where(DatasetItem.id.in_([uuid.UUID(str(i)) for i in item_ids]))
        elif not dataset_id and saved_cfg.get("dataset_item_ids"):
            # Honour the AOI's persisted item selection when nothing narrower
            # was chosen.
            stmt = stmt.where(DatasetItem.id.in_(_uuid_list(saved_cfg.get("dataset_item_ids"))))
        candidates = session.execute(stmt.limit(limit)).scalars().all()

        if bbox_4326 is not None:
            candidates = [it for it in candidates if _geometry_intersects_bbox(it.geometry, bbox_4326)]
        items_payload = [_serialize_dataset_item(it) for it in candidates]

    datasets_payload = [_serialize_dataset(d) for d in datasets]
    selection: dict = {
        "map_id": str(map_id_uuid) if map_id_uuid else None,
        "aoi_id": str(aoi.id) if aoi else None,
        "aoi_bbox": bbox_4326,
        "datasets": datasets_payload,
        "dataset_ids": [d["id"] for d in datasets_payload],
        "dataset_items": items_payload,
        "dataset_item_ids": [it["id"] for it in items_payload],
    }
    if aoi is not None:
        selection["geometry"] = aoi.geometry
        selection["render_config"] = aoi.render_config or {}
        selection["temporal_config"] = aoi.temporal_config or {}
        selection["analysis_config"] = aoi.analysis_config or {}

    result: dict = {"items": items_payload, "selection": selection}
    # A single explicitly-chosen dataset feeds the `dataset` handle used by
    # overlay / analysis nodes.
    if dataset_id and datasets:
        result["dataset"] = {"id": datasets_payload[0]["id"], "name": datasets[0].name}
    return result


@node(
    type="select_annotation_set",
    category="data_source",
    label="Select Annotation Set",
    description="Choose an existing annotation set by ID.",
    outputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "annotation_set_id": {
                "type": "string",
                "format": "uuid",
                "title": "Annotation Set",
                "x-picker": "annotation_set",
            }
        },
        "required": ["annotation_set_id"],
    },
    icon="tag",
)
def execute_select_annotation_set(session, config, input_data, **kwargs):
    from app.models.annotation_set import AnnotationSet
    aset = session.get(AnnotationSet, uuid.UUID(config["annotation_set_id"]))
    if not aset:
        raise ValueError(f"Annotation set {config['annotation_set_id']} not found")
    return {"annotation_set": {"id": str(aset.id), "name": aset.name}}


@node(
    type="stac_search",
    category="data_source",
    label="STAC Search",
    description="Query STAC catalog for items matching spatial/temporal/property filters.",
    outputs=[HandleDef(handle="items", type="dataset_items")],
    config_schema={
        "type": "object",
        "properties": {
            "collections": {"type": "array", "items": {"type": "string"}, "title": "Collections"},
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4, "title": "Bounding Box"},
            "datetime": {"type": "string", "title": "Datetime Range", "description": "e.g., 2024-01-01/2024-12-31"},
            "limit": {"type": "integer", "title": "Max Results", "default": 100},
        },
    },
    icon="search",
)
def execute_stac_search(session, config, input_data, **kwargs):
    """Search STAC catalog and return matching dataset items."""
    import httpx
    from app.config import settings
    from sqlalchemy import select
    from app.models.dataset_item import DatasetItem

    search_params = {}
    if config.get("collections"):
        search_params["collections"] = config["collections"]
    if config.get("bbox"):
        search_params["bbox"] = config["bbox"]
    if config.get("datetime"):
        search_params["datetime"] = config["datetime"]
    search_params["limit"] = config.get("limit", 100)

    # Query STAC API
    resp = httpx.post(
        f"{settings.STAC_API_URL}/search",
        json=search_params,
        timeout=30,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    stac_item_ids = [f["id"] for f in features]

    if not stac_item_ids:
        return {"items": []}

    # Resolve to local dataset_items
    stmt = select(DatasetItem.id, DatasetItem.stac_item_id).where(
        DatasetItem.stac_item_id.in_(stac_item_ids),
        DatasetItem.is_active.is_(True),
    )
    rows = session.execute(stmt).all()
    return {"items": [{"id": str(r.id), "stac_item_id": r.stac_item_id} for r in rows]}


@node(
    type="aoi_filter",
    category="data_source",
    label="AOI Filter",
    description="Filter items by geographic area of interest.",
    inputs=[HandleDef(handle="items", type="dataset_items")],
    outputs=[HandleDef(handle="items", type="dataset_items")],
    config_schema={
        "type": "object",
        "properties": {"geometry": {"type": "object", "title": "GeoJSON Geometry"}},
        "required": ["geometry"],
    },
    icon="map-pin",
    frontend_preview=True,
)
def execute_aoi_filter(session, config, input_data, **kwargs):
    """Filter dataset items that intersect the given GeoJSON geometry."""
    import json
    from sqlalchemy import select, text, func, cast
    from sqlalchemy.dialects.postgresql import JSONB
    from geoalchemy2 import Geometry
    from app.models.dataset_item import DatasetItem

    items = input_data.get("items", [])
    if not items:
        return {"items": []}

    aoi_geojson = json.dumps(config["geometry"])
    item_ids = [uuid.UUID(i["id"]) for i in items]

    # DatasetItem.geometry is JSONB (GeoJSON dict), so cast to geometry for spatial ops
    stmt = select(DatasetItem.id, DatasetItem.stac_item_id).where(
        DatasetItem.id.in_(item_ids),
        func.ST_Intersects(
            func.ST_GeomFromGeoJSON(cast(DatasetItem.geometry, sqlalchemy.Text)),
            func.ST_GeomFromGeoJSON(aoi_geojson),
        ),
    )
    rows = session.execute(stmt).all()
    return {"items": [{"id": str(r.id), "stac_item_id": r.stac_item_id} for r in rows]}


@node(
    type="search_map_aoi_resources",
    category="data_source",
    label="Search Map AOI Resources",
    description="Find map datasets, dataset items, vectors, and raster masks overlapping an AOI.",
    outputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="selection", type="map_selection", label="Map Selection"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Map", "x-picker": "map"},
            "aoi_bbox": {
                "type": "array",
                "title": "AOI Bounding Box",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
            },
        },
        "required": ["map_id", "aoi_bbox"],
    },
    icon="search",
    color="#3B82F6",
)
def execute_search_map_aoi_resources(session, config, input_data, **kwargs):
    from sqlalchemy import distinct, func, select

    from app.models.annotation import Annotation
    from app.models.annotation_set import AnnotationSet
    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem
    from app.models.map import Map

    map_id = uuid.UUID(config["map_id"])
    map_row = session.get(Map, map_id)
    if not map_row:
        raise ValueError(f"Map {config['map_id']} not found")
    bbox_4326 = _parse_bbox_4326(config["aoi_bbox"])

    all_items = session.execute(
        select(DatasetItem).where(
            DatasetItem.organization_id == map_row.project.organization_id,
            DatasetItem.is_active.is_(True),
        )
    ).scalars().all()
    dataset_items = [item for item in all_items if _geometry_intersects_bbox(item.geometry, bbox_4326)]
    dataset_ids = {item.dataset_id for item in dataset_items}

    datasets = []
    if dataset_ids:
        datasets = session.execute(
            select(Dataset)
            .where(
                Dataset.id.in_(dataset_ids),
                Dataset.organization_id == map_row.project.organization_id,
                Dataset.deleted_at.is_(None),
            )
            .order_by(Dataset.created_at.desc())
        ).scalars().all()

    raster_candidates = session.execute(
        select(AnnotationSet).where(
            AnnotationSet.organization_id == map_row.project.organization_id,
            AnnotationSet.deleted_at.is_(None),
            AnnotationSet.raster_config.is_not(None),
        )
    ).scalars().all()
    raster_mask_annotation_sets = [
        aset
        for aset in raster_candidates
        if _bbox_intersects((aset.raster_config or {}).get("bounds_4326") or [0, 0, 0, 0], bbox_4326)
    ]

    vector_set_ids = session.execute(
        select(distinct(Annotation.annotation_set_id))
        .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
        .where(
            Annotation.deleted_at.is_(None),
            AnnotationSet.organization_id == map_row.project.organization_id,
            AnnotationSet.deleted_at.is_(None),
            func.ST_Intersects(
                Annotation.geometry,
                func.ST_MakeEnvelope(
                    bbox_4326[0], bbox_4326[1], bbox_4326[2], bbox_4326[3], 4326
                ),
            ),
        )
    ).scalars().all()
    vector_annotation_sets = []
    if vector_set_ids:
        vector_annotation_sets = session.execute(
            select(AnnotationSet).where(
                AnnotationSet.id.in_(vector_set_ids),
                AnnotationSet.organization_id == map_row.project.organization_id,
                AnnotationSet.deleted_at.is_(None),
            )
        ).scalars().all()

    datasets_payload = [_serialize_dataset(row) for row in datasets]
    items_payload = [_serialize_dataset_item(row) for row in dataset_items]
    selection = {
        "map_id": str(map_id),
        "aoi_bbox": bbox_4326,
        "datasets": datasets_payload,
        "dataset_ids": [d["id"] for d in datasets_payload],
        "dataset_items": items_payload,
        "vector_annotation_sets": [
            {"id": str(aset.id), "name": aset.name} for aset in vector_annotation_sets
        ],
        "raster_mask_annotation_sets": [
            {"id": str(aset.id), "name": aset.name} for aset in raster_mask_annotation_sets
        ],
    }
    return {"items": items_payload, "selection": selection}


@node(
    type="load_saved_map_aoi_timeline",
    category="data_source",
    label="Load Saved AOI Timeline",
    description="Resolve saved AOI dataset items ordered by timestamp for analysis or playback.",
    outputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="selection", type="map_selection", label="Map Selection"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Map", "x-picker": "map"},
            "aoi_id": {
                "type": "string",
                "format": "uuid",
                "title": "Saved AOI",
                "x-picker": "map_aoi",
                "x-picker-depends-on": "map_id",
            },
        },
        "required": ["map_id", "aoi_id"],
    },
    icon="clock",
    color="#3B82F6",
)
def execute_load_saved_map_aoi_timeline(session, config, input_data, **kwargs):
    from sqlalchemy import select

    from app.models.dataset_item import DatasetItem
    from app.models.map_aoi import MapAOI

    map_id = uuid.UUID(config["map_id"])
    aoi_id = uuid.UUID(config["aoi_id"])
    aoi = session.get(MapAOI, aoi_id)
    if not aoi or aoi.map_id != map_id or aoi.deleted_at is not None:
        raise ValueError(f"Saved AOI {config['aoi_id']} not found on map {config['map_id']}")

    selection_cfg = aoi.selection_config or {}
    dataset_item_ids = _uuid_list(selection_cfg.get("dataset_item_ids"))
    dataset_ids = _uuid_list(selection_cfg.get("dataset_ids"))
    stmt = select(DatasetItem).where(DatasetItem.is_active.is_(True))
    if dataset_item_ids:
        stmt = stmt.where(DatasetItem.id.in_(dataset_item_ids))
    elif dataset_ids:
        stmt = stmt.where(DatasetItem.dataset_id.in_(dataset_ids))
    else:
        return {
            "items": [],
            "selection": {
                "map_id": str(map_id),
                "aoi_id": str(aoi.id),
                "aoi_bbox": aoi.bbox_4326,
                "render_config": aoi.render_config or {},
            },
        }

    items = session.execute(stmt).scalars().all()
    matched = [item for item in items if _geometry_intersects_bbox(item.geometry, aoi.bbox_4326)]
    matched.sort(key=lambda item: (item.item_datetime is None, item.item_datetime, item.created_at))
    items_payload = [_serialize_dataset_item(item) for item in matched]
    return {
        "items": items_payload,
        "selection": {
            "map_id": str(map_id),
            "aoi_id": str(aoi.id),
            "aoi_bbox": aoi.bbox_4326,
            "dataset_ids": [str(v) for v in selection_cfg.get("dataset_ids", [])],
            "dataset_item_ids": [str(v) for v in selection_cfg.get("dataset_item_ids", [])],
            "render_config": aoi.render_config or {},
            "temporal_config": aoi.temporal_config or {},
            "analysis_config": aoi.analysis_config or {},
        },
    }
