import uuid

from app.automation.registry import node, HandleDef


@node(
    type="run_inference",
    category="ml_annotation",
    label="Run Inference",
    description=(
        "Pick a registered model and run it on the incoming dataset items. Each "
        "item gets its own annotation set (schema is pulled from the registered "
        "model) and is auto-grouped into that schema's collection. The model's "
        "prompt fields (text / box / point, per the model's adapter) render below "
        "once a model is chosen. Wire an Area of Interest in to limit inference to "
        "a sub-region."
    ),
    inputs=[
        HandleDef(handle="items", type="dataset_items", label="Dataset Items"),
        HandleDef(handle="aoi", type="map_selection", label="Area of Interest", required=False),
    ],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set")],
    config_schema={
        "type": "object",
        "properties": {
            "model_id": {
                "type": "string",
                "format": "uuid",
                "title": "AI Model",
                "x-picker": "model",
            },
            "annotation_set_name": {
                "type": "string",
                "title": "Annotation Set Name",
                "description": "Base name for the generated sets (item identifier is appended for disambiguation). Leave empty to use the model name.",
                "default": "",
            },
            "output_class_id": {
                "type": "string",
                "title": "Output Class",
                "description": "For prompted models (e.g., SAM3), the class to force-label predictions as. Ignored for non-prompted models.",
                "default": "",
            },
            "prompt_payload": {
                "type": "object",
                "title": "Prompt Payload",
                "description": "Model-specific prompts (text / boxes / points). Rendered from the selected model's adapter schema; forwarded opaquely to the model request.",
                "default": {},
            },
        },
        "required": ["model_id"],
    },
    icon="brain",
    color="#8B5CF6",
)
def execute_run_inference(session, config, input_data, **kwargs):
    """Delegates to existing inference Celery task. Returns DeferToJob so the step
    parks itself instead of blocking a worker. The inference task calls
    resume_after_job() on completion to continue the pipeline."""
    from app.automation.registry import DeferToJob
    from app.models.ai_model import AIModel
    from app.models.job import Job

    items = input_data.get("items", [])
    aoi = input_data.get("aoi", {})
    model_id = config.get("model_id")
    if not items:
        raise ValueError("No dataset items provided")
    if not model_id:
        raise ValueError("No model selected")

    model = session.get(AIModel, uuid.UUID(model_id))
    if not model:
        raise ValueError(f"Model {model_id} not found")

    aoi_bbox = None
    if isinstance(aoi, dict):
        aoi_bbox = aoi.get("aoi_bbox")

    job = Job(
        organization_id=uuid.UUID(kwargs["organization_id"]),
        type="inference",
        status="queued",
        config={
            "trigger": "automation",
            "run_output_config": {
                # Per-run knobs forwarded to ModelManager on top of the
                # model's own output_config JSONB. Confidence/batch/device
                # are model concerns — not overridable per run.
                "aoi_bbox": aoi_bbox,
                "prompt_payload": config.get("prompt_payload", {}),
                "output_class_id": config.get("output_class_id") or None,
                "annotation_set_name": (config.get("annotation_set_name") or "").strip() or None,
            },
            "automation_run_id": kwargs.get("run_id"),
            "automation_step_id": kwargs.get("step_id"),
        },
        input_refs=[{"type": "dataset_item", "id": i["id"]} for i in items],
        total_items=len(items),
        model_id=model.id,
    )
    session.add(job)
    session.flush()

    from app.workers.inference.tasks import run_inference_batch
    run_inference_batch.delay(str(job.id))

    return DeferToJob(job_id=str(job.id))


# NOTE: `post_processing` and `create_annotation_set` were deleted in favour
# of `Run Inference` producing fully-formed annotation sets directly (one per
# dataset item, schema pulled from the registered model). Confidence/area/NMS
# filtering will live on a future dedicated filter node — not the inference run.


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
