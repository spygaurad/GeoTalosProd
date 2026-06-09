import json
import uuid

from app.automation.registry import node, HandleDef


@node(
    type="overlay_on_map",
    category="map_overlay",
    label="Overlay on Map",
    description="Mount one or more annotation sets onto a map. Accepts a single set (e.g. from Select Annotation Set) or the multi-set output of Run Inference.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", required=False, label="Pass-through")],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Target Map", "x-picker": "map"},
        },
        "required": ["map_id"],
    },
    icon="layers",
    color="#06B6D4",
)
def execute_overlay_on_map(session, config, input_data, **kwargs):
    """Mount annotation set(s) on a map via the map_annotation_sets join table."""
    from sqlalchemy import select

    from app.models.map_annotation_set import MapAnnotationSet

    aset = input_data.get("annotation_set", {})

    # Accept either a single-set payload (`id`) or a multi-set payload
    # (`annotation_set_ids`, emitted by Run Inference — one set per item).
    aset_ids = list(aset.get("annotation_set_ids") or [])
    if not aset_ids and aset.get("id"):
        aset_ids = [aset["id"]]
    if not aset_ids:
        return {"annotation_set": aset}

    map_id = uuid.UUID(config["map_id"])
    mounted: list[str] = []
    for aset_id_str in aset_ids:
        aset_uuid = uuid.UUID(aset_id_str)
        existing = session.execute(
            select(MapAnnotationSet).where(
                MapAnnotationSet.map_id == map_id,
                MapAnnotationSet.annotation_set_id == aset_uuid,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MapAnnotationSet(
                    map_id=map_id,
                    annotation_set_id=aset_uuid,
                    visible=True,
                    opacity=1.0,
                    z_index=0,
                )
            )
        mounted.append(aset_id_str)

    session.flush()
    return {"annotation_set": {**aset, "map_id": str(map_id), "mounted_annotation_set_ids": mounted}}


# `overlay_inference_outputs_on_map` was deleted — `Overlay on Map` now handles
# both single and multi-set payloads, so the dedicated inference-mount node
# was redundant.


@node(
    type="overlay_dataset_on_map",
    category="map_overlay",
    label="Overlay Dataset on Map",
    description="Add a dataset as a raster layer on a map view.",
    inputs=[HandleDef(handle="dataset", type="dataset")],
    outputs=[HandleDef(handle="dataset", type="dataset", required=False, label="Pass-through")],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Target Map", "x-picker": "map"},
            "layer_name": {"type": "string", "title": "Layer Name"},
        },
        "required": ["map_id"],
    },
    icon="layers",
    color="#06B6D4",
)
def execute_overlay_dataset_on_map(session, config, input_data, **kwargs):
    """Create a MapLayer row pointing at the dataset."""
    from app.models.map_layer import MapLayer

    dataset = input_data.get("dataset", {})
    dataset_id = dataset.get("id")
    if not dataset_id:
        return {"dataset": dataset}

    layer = MapLayer(
        map_id=uuid.UUID(config["map_id"]),
        name=config.get("layer_name") or dataset.get("name") or "Dataset Layer",
        layer_type="raster",
        source_type="dataset",
        dataset_id=uuid.UUID(dataset_id),
        visible=True,
        opacity=1.0,
    )
    session.add(layer)
    session.flush()

    return {"dataset": {**dataset, "map_layer_id": str(layer.id)}}


# `style_assignment` was removed — annotation appearance is driven globally by
# each AnnotationClass's bound Style (AnnotationLayer renders from
# `annotationClass.style.definition`), authored in the schema/class editor. The
# old node wrote per-mount `MapAnnotationSet.style_id`/`style_override`, which
# nothing on the render path reads. Styling is a schema concern, not a pipeline step.


@node(
    type="before_after_comparison",
    category="map_overlay",
    label="Before/After Comparison",
    description="Generate a side-by-side or swipe comparison view.",
    inputs=[
        HandleDef(handle="before", type="dataset_items", label="Before"),
        HandleDef(handle="after", type="dataset_items", label="After"),
    ],
    outputs=[HandleDef(handle="comparison", type="quality_metrics")],
    config_schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["swipe", "side_by_side", "overlay"], "default": "swipe"},
        },
    },
    icon="columns",
    color="#06B6D4",
    status="placeholder",
)
def execute_before_after_comparison(session, config, input_data, **kwargs):
    return {"comparison": {"mode": config.get("mode", "swipe")}}


@node(
    type="export_annotations",
    category="map_overlay",
    label="Export Annotations",
    description="Export annotation set as GeoJSON to S3.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="export_url", type="string")],
    config_schema={
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["geojson"], "default": "geojson"},
        },
    },
    icon="download",
    color="#06B6D4",
)
def execute_export_annotations(session, config, input_data, **kwargs):
    """Export annotations as GeoJSON and upload to S3."""
    import tempfile
    import os
    from sqlalchemy import select, func
    from geoalchemy2.shape import to_shape
    from app.models.annotation import Annotation
    from app.models.annotation_class import AnnotationClass
    from app.services import storage_service

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"export_url": ""}

    stmt = (
        select(
            Annotation.id,
            func.ST_AsGeoJSON(Annotation.geometry).label("geojson"),
            Annotation.confidence,
            Annotation.properties,
            AnnotationClass.name.label("class_name"),
        )
        .join(AnnotationClass, Annotation.class_id == AnnotationClass.id)
        .where(
            Annotation.annotation_set_id == uuid.UUID(aset_id),
            Annotation.deleted_at.is_(None),
        )
    )
    rows = session.execute(stmt).all()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "id": str(r.id),
            "geometry": json.loads(r.geojson),
            "properties": {
                "class": r.class_name,
                "confidence": r.confidence,
                **(r.properties or {}),
            },
        })

    geojson = json.dumps({"type": "FeatureCollection", "features": features})

    org_id = uuid.UUID(kwargs["organization_id"])
    s3_key = f"exports/automation/{kwargs['run_id']}/annotations.geojson"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
        f.write(geojson)
        tmp_path = f.name

    try:
        storage_service.upload_from_path(org_id, s3_key, tmp_path, content_type="application/geo+json")
    finally:
        os.unlink(tmp_path)

    return {"export_url": f"s3://{storage_service.bucket_name(org_id)}/{s3_key}"}


@node(
    type="export_dataset_items",
    category="map_overlay",
    label="Export Dataset Items",
    description="Export dataset items metadata as GeoJSON to S3.",
    inputs=[HandleDef(handle="items", type="dataset_items")],
    outputs=[HandleDef(handle="export_url", type="string")],
    config_schema={
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["geojson"], "default": "geojson"},
        },
    },
    icon="download",
    color="#06B6D4",
)
def execute_export_dataset_items(session, config, input_data, **kwargs):
    """Export dataset items as GeoJSON to S3."""
    import tempfile
    import os
    from sqlalchemy import select
    from app.models.dataset_item import DatasetItem
    from app.services import storage_service

    items = input_data.get("items", [])
    if not items:
        return {"export_url": ""}

    item_ids = [uuid.UUID(i["id"]) for i in items]
    stmt = select(
        DatasetItem.id,
        DatasetItem.stac_item_id,
        DatasetItem.filename,
        DatasetItem.geometry,
        DatasetItem.item_datetime,
    ).where(DatasetItem.id.in_(item_ids))
    rows = session.execute(stmt).all()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "id": str(r.id),
            "geometry": r.geometry,  # already GeoJSON dict (JSONB)
            "properties": {
                "stac_item_id": r.stac_item_id,
                "filename": r.filename,
                "datetime": r.item_datetime.isoformat() if r.item_datetime else None,
            },
        })

    geojson_str = json.dumps({"type": "FeatureCollection", "features": features})

    org_id = uuid.UUID(kwargs["organization_id"])
    s3_key = f"exports/automation/{kwargs['run_id']}/dataset_items.geojson"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".geojson", delete=False) as f:
        f.write(geojson_str)
        tmp_path = f.name

    try:
        storage_service.upload_from_path(org_id, s3_key, tmp_path, content_type="application/geo+json")
    finally:
        os.unlink(tmp_path)

    return {"export_url": f"s3://{storage_service.bucket_name(org_id)}/{s3_key}"}


# `generate_report` was moved to `app/automation/nodes/output.py` and now
# accepts multiple annotation sets, producing a structured JSON report.
