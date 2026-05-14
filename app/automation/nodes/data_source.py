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
    type="select_dataset",
    category="data_source",
    label="Select Dataset",
    description="Choose a dataset by ID.",
    outputs=[HandleDef(handle="dataset", type="dataset")],
    config_schema={
        "type": "object",
        "properties": {"dataset_id": {"type": "string", "format": "uuid", "title": "Dataset", "x-picker": "dataset"}},
        "required": ["dataset_id"],
    },
    icon="database",
)
def execute_select_dataset(session, config, input_data, **kwargs):
    from app.models.dataset import Dataset
    dataset = session.get(Dataset, uuid.UUID(config["dataset_id"]))
    if not dataset:
        raise ValueError(f"Dataset {config['dataset_id']} not found")
    return {"dataset": {"id": str(dataset.id), "name": dataset.name}}


@node(
    type="select_dataset_items",
    category="data_source",
    label="Select Dataset Items",
    description="Choose items from a dataset (optional filters).",
    outputs=[HandleDef(handle="items", type="dataset_items")],
    config_schema={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "format": "uuid", "title": "Dataset", "x-picker": "dataset"},
            "filter": {"type": "object", "title": "Filter (JSON)", "default": {}},
            "limit": {"type": "integer", "title": "Max Items", "default": 1000},
        },
        "required": ["dataset_id"],
    },
    icon="database",
)
def execute_select_dataset_items(session, config, input_data, **kwargs):
    from sqlalchemy import select
    from app.models.dataset_item import DatasetItem
    stmt = select(DatasetItem.id, DatasetItem.stac_item_id).where(
        DatasetItem.dataset_id == uuid.UUID(config["dataset_id"])
    )
    if config.get("limit"):
        stmt = stmt.limit(config["limit"])
    items = session.execute(stmt).all()
    return {"items": [{"id": str(i.id), "stac_item_id": i.stac_item_id} for i in items]}


@node(
    type="select_annotation_set",
    category="data_source",
    label="Select Annotation Set",
    description="Choose an existing annotation set by ID.",
    outputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {"annotation_set_id": {"type": "string", "format": "uuid", "title": "Annotation Set"}},
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
    type="select_map_datasets",
    category="data_source",
    label="Select Map Datasets",
    description="Load all datasets currently attached to a map.",
    outputs=[HandleDef(handle="selection", type="map_selection", label="Map Selection")],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Map", "x-picker": "map"},
        },
        "required": ["map_id"],
    },
    icon="layers",
    color="#3B82F6",
)
def execute_select_map_datasets(session, config, input_data, **kwargs):
    from sqlalchemy import select

    from app.models.dataset import Dataset
    from app.models.map import Map
    from app.models.map_layer import MapLayer

    map_id = uuid.UUID(config["map_id"])
    map_row = session.get(Map, map_id)
    if not map_row:
        raise ValueError(f"Map {config['map_id']} not found")

    datasets = session.execute(
        select(Dataset)
        .join(MapLayer, MapLayer.dataset_id == Dataset.id)
        .where(
            MapLayer.map_id == map_id,
            Dataset.deleted_at.is_(None),
        )
        .distinct()
        .order_by(Dataset.created_at.desc())
    ).scalars().all()
    datasets_payload = [_serialize_dataset(row) for row in datasets]
    return {
        "selection": {
            "map_id": str(map_id),
            "datasets": datasets_payload,
            "dataset_ids": [d["id"] for d in datasets_payload],
        }
    }


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
    from app.models.map_annotation_set import MapAnnotationSet
    from app.models.map_layer import MapLayer

    map_id = uuid.UUID(config["map_id"])
    map_row = session.get(Map, map_id)
    if not map_row:
        raise ValueError(f"Map {config['map_id']} not found")
    bbox_4326 = _parse_bbox_4326(config["aoi_bbox"])

    dataset_ids = set(
        session.execute(
            select(MapLayer.dataset_id).where(
                MapLayer.map_id == map_id,
                MapLayer.dataset_id.is_not(None),
            )
        ).scalars().all()
    )

    datasets = []
    dataset_items = []
    if dataset_ids:
        datasets = session.execute(
            select(Dataset)
            .where(
                Dataset.id.in_(dataset_ids),
                Dataset.deleted_at.is_(None),
            )
            .order_by(Dataset.created_at.desc())
        ).scalars().all()
        all_items = session.execute(
            select(DatasetItem).where(
                DatasetItem.dataset_id.in_(dataset_ids),
                DatasetItem.is_active.is_(True),
            )
        ).scalars().all()
        dataset_items = [item for item in all_items if _geometry_intersects_bbox(item.geometry, bbox_4326)]

    mounted_set_ids = set(
        session.execute(
            select(MapAnnotationSet.annotation_set_id).where(MapAnnotationSet.map_id == map_id)
        ).scalars().all()
    )
    layer_set_ids = set(
        session.execute(
            select(MapLayer.annotation_set_id).where(
                MapLayer.map_id == map_id,
                MapLayer.annotation_set_id.is_not(None),
            )
        ).scalars().all()
    )
    annotation_set_ids = mounted_set_ids | layer_set_ids

    vector_annotation_sets = []
    raster_mask_annotation_sets = []
    if annotation_set_ids:
        raster_candidates = session.execute(
            select(AnnotationSet).where(
                AnnotationSet.id.in_(annotation_set_ids),
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
                Annotation.annotation_set_id.in_(annotation_set_ids),
                Annotation.deleted_at.is_(None),
                func.ST_Intersects(
                    Annotation.geometry,
                    func.ST_MakeEnvelope(
                        bbox_4326[0], bbox_4326[1], bbox_4326[2], bbox_4326[3], 4326
                    ),
                ),
            )
        ).scalars().all()
        if vector_set_ids:
            vector_annotation_sets = session.execute(
                select(AnnotationSet).where(
                    AnnotationSet.id.in_(vector_set_ids),
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
    type="select_map_dataset_items_in_aoi",
    category="data_source",
    label="Select Map Dataset Items In AOI",
    description="Load dataset items from a map dataset that intersect the AOI.",
    outputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="selection", type="map_selection", label="Map Selection"),
    ],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Map", "x-picker": "map"},
            "dataset_id": {"type": "string", "format": "uuid", "title": "Dataset", "x-picker": "dataset"},
            "aoi_bbox": {
                "type": "array",
                "title": "AOI Bounding Box",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
            },
        },
        "required": ["map_id", "dataset_id", "aoi_bbox"],
    },
    icon="map-pin",
    color="#3B82F6",
)
def execute_select_map_dataset_items_in_aoi(session, config, input_data, **kwargs):
    from sqlalchemy import select

    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem
    from app.models.map import Map
    from app.models.map_layer import MapLayer

    map_id = uuid.UUID(config["map_id"])
    dataset_id = uuid.UUID(config["dataset_id"])
    bbox_4326 = _parse_bbox_4326(config["aoi_bbox"])

    map_row = session.get(Map, map_id)
    if not map_row:
        raise ValueError(f"Map {config['map_id']} not found")

    dataset_on_map = session.execute(
        select(MapLayer.id).where(
            MapLayer.map_id == map_id,
            MapLayer.dataset_id == dataset_id,
        )
    ).scalar_one_or_none()
    if dataset_on_map is None:
        raise ValueError(f"Dataset {config['dataset_id']} is not attached to map {config['map_id']}")

    dataset = session.get(Dataset, dataset_id)
    if not dataset or dataset.deleted_at is not None:
        raise ValueError(f"Dataset {config['dataset_id']} not found")

    items = session.execute(
        select(DatasetItem).where(
            DatasetItem.dataset_id == dataset_id,
            DatasetItem.is_active.is_(True),
        )
    ).scalars().all()
    matched = [item for item in items if _geometry_intersects_bbox(item.geometry, bbox_4326)]
    items_payload = [_serialize_dataset_item(item) for item in matched]
    return {
        "items": items_payload,
        "selection": {
            "map_id": str(map_id),
            "aoi_bbox": bbox_4326,
            "datasets": [_serialize_dataset(dataset)],
            "dataset_ids": [str(dataset_id)],
            "dataset_items": items_payload,
        },
    }


@node(
    type="select_saved_map_aoi",
    category="data_source",
    label="Select Saved Map AOI",
    description="Load a saved AOI with its persisted selection and rendering state.",
    outputs=[HandleDef(handle="selection", type="map_selection", label="Map Selection")],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Map", "x-picker": "map"},
            "aoi_id": {"type": "string", "format": "uuid", "title": "Saved AOI"},
        },
        "required": ["map_id", "aoi_id"],
    },
    icon="bookmark",
    color="#3B82F6",
)
def execute_select_saved_map_aoi(session, config, input_data, **kwargs):
    from app.models.map_aoi import MapAOI

    map_id = uuid.UUID(config["map_id"])
    aoi_id = uuid.UUID(config["aoi_id"])
    aoi = session.get(MapAOI, aoi_id)
    if not aoi or aoi.map_id != map_id or aoi.deleted_at is not None:
        raise ValueError(f"Saved AOI {config['aoi_id']} not found on map {config['map_id']}")

    selection_cfg = aoi.selection_config or {}
    return {
        "selection": {
            "map_id": str(map_id),
            "aoi_id": str(aoi.id),
            "aoi_bbox": aoi.bbox_4326,
            "geometry": aoi.geometry,
            "dataset_ids": [str(v) for v in selection_cfg.get("dataset_ids", [])],
            "dataset_item_ids": [str(v) for v in selection_cfg.get("dataset_item_ids", [])],
            "render_config": aoi.render_config or {},
            "temporal_config": aoi.temporal_config or {},
            "analysis_config": aoi.analysis_config or {},
        }
    }


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
            "aoi_id": {"type": "string", "format": "uuid", "title": "Saved AOI"},
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
