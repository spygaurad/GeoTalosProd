import json
import uuid

from app.automation.registry import node, HandleDef


@node(
    type="overlay_on_map",
    category="map_overlay",
    label="Overlay on Map",
    description="Add results as a layer on a map view.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", required=False, label="Pass-through")],
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
def execute_overlay_on_map(session, config, input_data, **kwargs):
    """Mount the annotation set on a map via the map_annotation_sets join table."""
    from app.models.map_annotation_set import MapAnnotationSet

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    if not aset_id:
        return {"annotation_set": aset}

    mount = MapAnnotationSet(
        map_id=uuid.UUID(config["map_id"]),
        annotation_set_id=uuid.UUID(aset_id),
        visible=True,
        opacity=1.0,
        z_index=0,
    )
    session.add(mount)
    session.flush()

    return {"annotation_set": {**aset, "map_id": config["map_id"]}}


@node(
    type="overlay_inference_outputs_on_map",
    category="map_overlay",
    label="Overlay Inference Outputs",
    description="Mount all annotation sets produced by an inference job onto a map.",
    inputs=[HandleDef(handle="predictions", type="raw_predictions", label="Predictions")],
    outputs=[HandleDef(handle="selection", type="map_selection", label="Map Selection")],
    config_schema={
        "type": "object",
        "properties": {
            "map_id": {"type": "string", "format": "uuid", "title": "Target Map", "x-picker": "map"},
            "visible": {"type": "boolean", "title": "Visible", "default": True},
            "opacity": {"type": "number", "title": "Opacity", "default": 1.0, "minimum": 0, "maximum": 1},
            "z_index": {"type": "integer", "title": "Z Index", "default": 0},
        },
        "required": ["map_id"],
    },
    icon="layers",
    color="#06B6D4",
)
def execute_overlay_inference_outputs_on_map(session, config, input_data, **kwargs):
    """Mount each annotation set from an inference job to the target map."""
    from sqlalchemy import select

    from app.models.map_annotation_set import MapAnnotationSet

    predictions = input_data.get("predictions", {})
    annotation_set_ids = predictions.get("annotation_set_ids") or []
    if not annotation_set_ids:
        return {
            "selection": {
                "map_id": config["map_id"],
                "mounted_annotation_set_ids": [],
            }
        }

    map_id = uuid.UUID(config["map_id"])
    visible = config.get("visible", True)
    opacity = float(config.get("opacity", 1.0))
    z_index = int(config.get("z_index", 0))
    mounted_ids: list[str] = []

    for aset_id_str in annotation_set_ids:
        aset_id = uuid.UUID(aset_id_str)
        existing = session.execute(
            select(MapAnnotationSet).where(
                MapAnnotationSet.map_id == map_id,
                MapAnnotationSet.annotation_set_id == aset_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                MapAnnotationSet(
                    map_id=map_id,
                    annotation_set_id=aset_id,
                    visible=visible,
                    opacity=opacity,
                    z_index=z_index,
                )
            )
        mounted_ids.append(aset_id_str)

    session.flush()
    return {
        "selection": {
            "map_id": str(map_id),
            "mounted_annotation_set_ids": mounted_ids,
        }
    }


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


@node(
    type="style_assignment",
    category="map_overlay",
    label="Style Assignment",
    description="Apply a visual style to an annotation set for map display.",
    inputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set")],
    config_schema={
        "type": "object",
        "properties": {
            "style_id": {"type": "string", "format": "uuid", "title": "Style", "x-picker": "style"},
            "color_by": {"type": "string", "enum": ["label", "confidence", "status"], "default": "label"},
        },
    },
    icon="palette",
    color="#06B6D4",
)
def execute_style_assignment(session, config, input_data, **kwargs):
    """Apply a style to all map mounts of the annotation set."""
    from app.models.map_annotation_set import MapAnnotationSet

    aset = input_data.get("annotation_set", {})
    aset_id = aset.get("id")
    style_id = config.get("style_id")
    if not aset_id or not style_id:
        return {"annotation_set": aset}

    from sqlalchemy import select
    stmt = select(MapAnnotationSet).where(
        MapAnnotationSet.annotation_set_id == uuid.UUID(aset_id),
    )
    mounts = session.execute(stmt).scalars().all()
    for mount in mounts:
        mount.style_id = uuid.UUID(style_id)
        if config.get("color_by"):
            mount.style_override = {"color_by": config["color_by"]}

    if mounts:
        session.flush()

    return {"annotation_set": {**aset, "style_id": style_id}}


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


@node(
    type="generate_report",
    category="map_overlay",
    label="Generate Report",
    description="Create a PDF/CSV summary of pipeline results.",
    inputs=[HandleDef(handle="data", type="any")],
    config_schema={
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["csv", "pdf"], "default": "csv"},
            "filename": {"type": "string", "default": "report"},
        },
    },
    icon="file-text",
    color="#06B6D4",
    status="placeholder",
)
def execute_generate_report(session, config, input_data, **kwargs):
    return {"report_url": f"/reports/{kwargs['run_id']}_{config.get('filename', 'report')}.{config.get('format', 'csv')}"}
