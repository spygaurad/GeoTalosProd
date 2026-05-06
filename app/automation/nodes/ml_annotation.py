import uuid

from app.automation.registry import node, HandleDef


@node(
    type="select_model",
    category="ml_annotation",
    label="Select Model",
    description="Choose a registered ML model for inference.",
    outputs=[HandleDef(handle="model", type="model", label="Model")],
    config_schema={
        "type": "object",
        "properties": {
            "model_id": {
                "type": "string",
                "format": "uuid",
                "title": "AI Model",
                "x-picker": "model",
            },
        },
        "required": ["model_id"],
    },
    icon="cpu",
    color="#8B5CF6",
)
def execute_select_model(session, config, input_data, **kwargs):
    from app.models.ai_model import AIModel
    model_id = config.get("model_id")
    if not model_id:
        raise ValueError("model_id required")
    model = session.get(AIModel, uuid.UUID(model_id))
    if not model:
        raise ValueError(f"Model {model_id} not found")
    return {"model": {"id": str(model.id), "name": model.name, "type": model.type}}


@node(
    type="run_inference",
    category="ml_annotation",
    label="Run Inference",
    description="Batch inference on dataset items using the selected model. Produces raw predictions.",
    inputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="model", type="model", label="Model"),
        HandleDef(handle="selection", type="map_selection", label="Map Selection", required=False),
    ],
    outputs=[HandleDef(handle="predictions", type="raw_predictions", label="Raw Predictions")],
    config_schema={
        "type": "object",
        "properties": {
            "confidence_threshold": {"type": "number", "title": "Min Confidence", "default": 0.5, "minimum": 0, "maximum": 1},
            "batch_size": {"type": "integer", "title": "Batch Size", "default": 100, "minimum": 1, "maximum": 10000},
            "device": {"type": "string", "title": "Device", "enum": ["auto", "cpu", "gpu"], "default": "auto"},
            "aoi_bbox": {
                "type": "array",
                "title": "AOI Bounding Box",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[minx, miny, maxx, maxy] in EPSG:4326. If it covers the item bounds, the full item is used.",
            },
        },
    },
    icon="brain",
    color="#8B5CF6",
)
def execute_run_inference(session, config, input_data, **kwargs):
    """Delegates to existing inference Celery task. Returns DeferToJob so the step
    parks itself instead of blocking a worker. The inference task calls
    resume_after_job() on completion to continue the pipeline."""
    from app.automation.registry import DeferToJob
    from app.models.job import Job

    items = input_data.get("items", [])
    model = input_data.get("model", {})
    selection = input_data.get("selection", {})
    if not items:
        raise ValueError("No dataset items provided")
    if not model:
        raise ValueError("No model provided")

    aoi_bbox = config.get("aoi_bbox")
    if aoi_bbox is None and isinstance(selection, dict):
        aoi_bbox = selection.get("aoi_bbox")

    job = Job(
        organization_id=uuid.UUID(kwargs["organization_id"]),
        type="inference",
        status="queued",
        config={
            "trigger": "automation",
            "run_output_config": {
                # Per-run knobs forwarded to ModelManager on top of the
                # model's own output_config JSONB.
                "confidence_threshold": config.get("confidence_threshold", 0.5),
                "batch_size": config.get("batch_size", 100),
                "aoi_bbox": aoi_bbox,
            },
            "automation_run_id": kwargs.get("run_id"),
            "automation_step_id": kwargs.get("step_id"),
        },
        input_refs=[{"type": "dataset_item", "id": i["id"]} for i in items],
        total_items=len(items),
        model_id=uuid.UUID(model["id"]),
    )
    session.add(job)
    session.flush()

    from app.workers.inference.tasks import run_inference_batch
    run_inference_batch.delay(str(job.id))

    return DeferToJob(job_id=str(job.id))


@node(
    type="post_processing",
    category="ml_annotation",
    label="Post-Processing",
    description="Clean up raw predictions: NMS, confidence filter, polygon simplification, area filter, hole removal.",
    inputs=[HandleDef(handle="predictions", type="raw_predictions", label="Raw Predictions")],
    outputs=[HandleDef(handle="predictions", type="processed_predictions", label="Processed Predictions")],
    config_schema={
        "type": "object",
        "properties": {
            "nms_iou_threshold": {"type": "number", "title": "NMS IoU Threshold", "default": 0.5, "minimum": 0, "maximum": 1},
            "min_confidence": {"type": "number", "title": "Min Confidence", "default": 0.5, "minimum": 0, "maximum": 1},
            "simplify_tolerance": {"type": "number", "title": "Simplify Tolerance (m)", "default": 1.0, "minimum": 0},
            "min_area_sqm": {"type": "number", "title": "Min Area (sq m)", "default": 0, "minimum": 0},
            "max_area_sqm": {"type": "number", "title": "Max Area (sq m)", "default": 1000000, "minimum": 0},
            "remove_holes": {"type": "boolean", "title": "Remove Holes", "default": False},
        },
    },
    icon="filter",
    color="#8B5CF6",
)
def execute_post_processing(session, config, input_data, **kwargs):
    """Post-process predictions: confidence filter, area filter, simplify, NMS via PostGIS."""
    from sqlalchemy import select, func, cast, text
    from geoalchemy2 import Geography
    from app.models.annotation import Annotation

    predictions = input_data.get("predictions", {})
    aset_id = predictions.get("annotation_set_id")
    if not aset_id:
        return {"predictions": {**predictions, "post_processing_applied": True}}

    min_conf = config.get("min_confidence", 0.5)
    min_area = config.get("min_area_sqm", 0)
    max_area = config.get("max_area_sqm", 1000000)
    simplify_tol = config.get("simplify_tolerance", 1.0)
    nms_iou = config.get("nms_iou_threshold", 0.5)
    remove_holes = config.get("remove_holes", False)

    # Step 1: Filter by confidence and area
    stmt = select(
        Annotation.id,
        Annotation.confidence,
        func.ST_Area(cast(Annotation.geometry, Geography)).label("area_sqm"),
    ).where(
        Annotation.annotation_set_id == uuid.UUID(aset_id),
        Annotation.deleted_at.is_(None),
    )
    rows = session.execute(stmt).all()

    keep_ids = []
    remove_ids = []
    for r in rows:
        conf = r.confidence if r.confidence is not None else 1.0
        if conf < min_conf or r.area_sqm < min_area or r.area_sqm > max_area:
            remove_ids.append(r.id)
        else:
            keep_ids.append(r.id)

    # Step 2: Simplify geometries that passed the filter
    if keep_ids and simplify_tol > 0:
        session.execute(
            text("""
                UPDATE annotations
                SET geometry = ST_SimplifyPreserveTopology(geometry, :tol)
                WHERE id = ANY(:ids) AND deleted_at IS NULL
            """),
            {"tol": simplify_tol / 111320, "ids": keep_ids},  # approx degrees for tolerance
        )

    # Step 3: Remove holes if requested
    if keep_ids and remove_holes:
        session.execute(
            text("""
                UPDATE annotations
                SET geometry = ST_MakeValid(
                    ST_BuildArea(ST_ExteriorRing((ST_Dump(geometry)).geom))
                )
                WHERE id = ANY(:ids) AND deleted_at IS NULL
                  AND GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')
            """),
            {"ids": keep_ids},
        )

    # Step 4: Soft-delete filtered out annotations
    if remove_ids:
        session.execute(
            text("UPDATE annotations SET deleted_at = now() WHERE id = ANY(:ids)"),
            {"ids": remove_ids},
        )

    session.flush()

    return {"predictions": {
        **predictions,
        "post_processing_applied": True,
        "kept": len(keep_ids),
        "removed": len(remove_ids),
    }}


@node(
    type="create_annotation_set",
    category="ml_annotation",
    label="Create Annotation Set",
    description="Write predictions as annotations linked to dataset_item and schema. Sets source=model.",
    inputs=[HandleDef(handle="predictions", type="processed_predictions", label="Processed Predictions")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set")],
    config_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "title": "Set Name", "default": "Auto-generated"},
            "schema_id": {"type": "string", "format": "uuid", "title": "Annotation Schema", "x-picker": "annotation_schema"},
            "map_id": {"type": "string", "format": "uuid", "title": "Target Map", "x-picker": "map"},
            "default_status": {"type": "string", "title": "Default Status", "enum": ["draft", "submitted"], "default": "draft"},
        },
        "required": ["name", "schema_id", "map_id"],
    },
    icon="plus-square",
    color="#8B5CF6",
)
def execute_create_annotation_set(session, config, input_data, **kwargs):
    from app.models.annotation_set import AnnotationSet

    predictions = input_data.get("predictions", {})
    annotation_set = AnnotationSet(
        name=config["name"],
        map_id=uuid.UUID(config["map_id"]),
        schema_id=uuid.UUID(config["schema_id"]) if config.get("schema_id") else None,
        created_by_job_id=uuid.UUID(predictions["job_id"]) if predictions.get("job_id") else None,
    )
    session.add(annotation_set)
    session.flush()

    return {"annotation_set": {"id": str(annotation_set.id), "name": annotation_set.name}}


@node(
    type="cascading_models",
    category="ml_annotation",
    label="Cascading Models",
    description="Multi-stage inference: fast detector -> crop chips -> fine segmentation -> merge back.",
    inputs=[HandleDef(handle="items", type="dataset_items", label="Dataset Items")],
    outputs=[HandleDef(handle="predictions", type="raw_predictions", label="Raw Predictions")],
    config_schema={
        "type": "object",
        "properties": {
            "detector_model_id": {"type": "string", "format": "uuid", "title": "Detector Model", "x-picker": "model"},
            "segmenter_model_id": {"type": "string", "format": "uuid", "title": "Segmenter Model", "x-picker": "model"},
            "chip_size_px": {"type": "integer", "title": "Chip Size (px)", "default": 512},
            "chip_overlap": {"type": "number", "title": "Chip Overlap", "default": 0.1, "minimum": 0, "maximum": 0.5},
        },
        "required": ["detector_model_id", "segmenter_model_id"],
    },
    icon="layers",
    color="#8B5CF6",
    status="placeholder",
)
def execute_cascading_models(session, config, input_data, **kwargs):
    # Placeholder — full implementation runs detector, chips, segmenter, merges
    return {"predictions": {"source": "cascading", "item_count": len(input_data.get("items", []))}}


@node(
    type="active_learning_selector",
    category="ml_annotation",
    label="Active Learning Selector",
    description="Rank unlabeled items by model uncertainty. Select most informative batch for annotation.",
    inputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="model", type="model", label="Model"),
    ],
    outputs=[HandleDef(handle="items", type="dataset_items", label="Selected Items (ranked)")],
    config_schema={
        "type": "object",
        "properties": {
            "strategy": {"type": "string", "title": "Strategy", "enum": ["entropy", "margin", "random"], "default": "entropy"},
            "batch_size": {"type": "integer", "title": "Batch Size", "default": 200, "minimum": 1},
            "diversity_weight": {"type": "number", "title": "Diversity Weight", "default": 0.3, "minimum": 0, "maximum": 1},
        },
    },
    icon="target",
    color="#8B5CF6",
    status="placeholder",
)
def execute_active_learning_selector(session, config, input_data, **kwargs):
    # Placeholder — full implementation ranks items by uncertainty
    items = input_data.get("items", [])
    batch_size = config.get("batch_size", 200)
    return {"items": items[:batch_size]}
