import uuid

import sqlalchemy
from app.automation.registry import node, HandleDef


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
