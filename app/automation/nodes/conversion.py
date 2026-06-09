"""Data-conversion nodes for the automation pipeline.

These wrap the reusable converters in ``app.services.conversion`` so format
conversions become first-class pipeline steps. Currently:

- ``vectorize_raster_mask``: raster segmentation mask (COG) -> vector annotation
  set, so a raster ground-truth set can feed straight into Ground Truth
  Comparison / IoU / area nodes alongside vector model predictions.

(Add further conversion nodes here as the converter library grows.)
"""

import uuid

from app.automation.registry import HandleDef, node


@node(
    type="vectorize_raster_mask",
    category="data_operations",
    label="Vectorize Raster Mask",
    description=(
        "Convert a raster segmentation-mask annotation set (COG + value→class map) "
        "into a vector annotation set, so it can be compared against vector model "
        "predictions with Ground Truth Comparison / IoU / area nodes."
    ),
    inputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Raster Mask Set")],
    outputs=[HandleDef(handle="annotation_set", type="annotation_set", label="Annotation Set")],
    config_schema={
        "type": "object",
        "properties": {
            "out_name": {
                "type": "string",
                "title": "Output Set Name",
                "description": "Defaults to '<source> · vectorized'.",
            },
            "schema_id": {
                "type": "string",
                "title": "Schema (for class filter)",
                "x-picker": "annotation_schema",
                "description": (
                    "Pick the mask's schema to enable per-class extraction below. "
                    "Leave empty to vectorize every mapped class."
                ),
            },
            "class_ids": {
                "type": "array",
                "items": {"type": "string"},
                "title": "Classes to Extract",
                "x-picker": "annotation_class",
                "description": (
                    "Only these classes are vectorized. Leave empty for all mapped "
                    "classes. Selecting a class that is not in the mask's value→class "
                    "map fails the run."
                ),
            },
            "simplify_tolerance": {
                "type": "number",
                "title": "Simplify Tolerance (source CRS units)",
                "description": "Douglas-Peucker tolerance; leave empty for full detail.",
                "minimum": 0,
            },
            "min_area_px": {
                "type": "number",
                "title": "Min Region Area (pixels)",
                "default": 0,
                "minimum": 0,
            },
            "connectivity": {
                "type": "integer",
                "title": "Pixel Connectivity",
                "enum": [4, 8],
                "default": 4,
            },
            "dissolve": {
                "type": "boolean",
                "title": "Dissolve per Class",
                "description": "One merged geometry per class (semantic IoU) instead of per region.",
                "default": False,
            },
        },
    },
    icon="vector-square",
    color="#0EA5E9",
)
def execute_vectorize_raster_mask(session, config, input_data, **kwargs):
    from app.config import settings
    from app.models.annotation_set import AnnotationSet
    from app.services.conversion import vectorize_raster_mask_set

    source = input_data.get("annotation_set") or {}
    source_id = source.get("id")
    if not source_id:
        raise ValueError("vectorize_raster_mask requires an input annotation_set with an id")

    raster_set = session.get(AnnotationSet, uuid.UUID(str(source_id)))
    if raster_set is None:
        raise ValueError(f"Annotation set {source_id} not found")
    org_id = kwargs.get("organization_id")
    if org_id and str(raster_set.organization_id) != str(org_id):
        raise ValueError("Annotation set does not belong to this organization")
    value_class_map = (raster_set.raster_config or {}).get("value_class_map") or {}
    if not value_class_map:
        raise ValueError(
            "Input set is not a raster mask (no raster_config.value_class_map). "
            "Use a raster-backed annotation set with a configured value→class map."
        )

    # Optional per-class extraction. Validate the chosen classes against the
    # mask's mapping so a class that isn't actually in the mask fails loudly
    # rather than silently producing an empty set.
    class_filter: set[str] | None = None
    selected_class_ids = [str(c) for c in (config.get("class_ids") or []) if c]
    if selected_class_ids:
        selected = set(selected_class_ids)
        mask_class_ids = {str(v) for v in value_class_map.values()}
        missing = selected - mask_class_ids
        if missing:
            raise ValueError(
                "Selected class(es) are not present in this raster mask's "
                f"value→class map: {sorted(missing)}. "
                f"Mapped classes: {sorted(mask_class_ids)}"
            )
        class_filter = selected

    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    gdal_env = {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if settings.AWS_ENDPOINT_URL.startswith("https://") else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }

    simplify = config.get("simplify_tolerance")
    result = vectorize_raster_mask_set(
        session,
        raster_set,
        gdal_env=gdal_env,
        name=config.get("out_name") or None,
        simplify_tolerance=float(simplify) if simplify not in (None, "") else None,
        min_area_px=float(config.get("min_area_px", 0) or 0),
        connectivity=int(config.get("connectivity", 4) or 4),
        dissolve_by_class=bool(config.get("dissolve", False)),
        confidence=1.0,
        class_filter=class_filter,
        # The automation framework owns the transaction — flush, don't commit.
        commit=False,
    )

    return {
        "annotation_set": {
            "id": str(result.annotation_set_id),
            "name": config.get("out_name") or f"{raster_set.name} · vectorized",
            "feature_count": result.feature_count,
            "class_counts": result.class_counts,
        }
    }


def _collect_set_ids(payloads) -> list[str]:
    """Flatten upstream annotation_set payloads to a de-duplicated list of ids.

    Accepts single-set payloads (``{id, ...}``) and multi-set payloads
    (``{annotation_set_ids: [...]}``, as emitted by Run Inference).
    """
    if isinstance(payloads, dict):
        payloads = [payloads]
    out: list[str] = []
    seen: set[str] = set()
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        ids = payload.get("annotation_set_ids") or []
        if not ids and payload.get("id"):
            ids = [payload["id"]]
        for sid in ids:
            s = str(sid)
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


@node(
    type="rasterize_annotation_set",
    category="data_operations",
    label="Rasterize Annotation Set",
    description=(
        "Burn one or more vector annotation sets into a single-band "
        "segmentation-mask COG and ingest it as a new dataset (with a "
        "value→class map). Use it to turn a vector ground truth into a raster "
        "mask that can be compared against a model-output mask with Raster Mask "
        "Metrics, or rendered on the map with class colors."
    ),
    inputs=[
        HandleDef(handle="annotation_sets", type="annotation_set", label="Annotation Sets", multiple=True),
    ],
    outputs=[HandleDef(handle="dataset", type="dataset", label="Mask Dataset")],
    config_schema={
        "type": "object",
        "properties": {
            "dataset_name": {
                "type": "string",
                "title": "Output Dataset Name",
                "description": "Defaults to '<first source> · mask'.",
            },
            "reference_dataset_id": {
                "type": "string",
                "format": "uuid",
                "title": "Reference Dataset (grid)",
                "x-picker": "dataset",
                "description": (
                    "The mask is gridded to this dataset's native CRS + "
                    "resolution so it aligns pixel-for-pixel with it — pick the "
                    "model-output raster you'll compare against. Leave empty to "
                    "use Web Mercator at the resolution below."
                ),
            },
            "resolution_m": {
                "type": "number",
                "title": "Resolution (m/pixel)",
                "description": (
                    "Ground sampling distance. Overrides the reference "
                    "dataset's native resolution when set; used directly when no "
                    "reference dataset is chosen."
                ),
                "minimum": 0,
            },
        },
    },
    icon="grid-2x2",
    color="#0EA5E9",
)
def execute_rasterize_annotation_set(session, config, input_data, **kwargs):
    """Dispatch a Celery job that rasterizes the input set(s) into a mask dataset.

    Heavy raster I/O (read reference COG, burn, write COG, ingest) runs off the
    pipeline worker as a Job; the step parks itself with ``DeferToJob`` and
    resumes with the new ``dataset`` once the job completes.
    """
    from app.automation.registry import DeferToJob
    from app.core.enums import JobType
    from app.models.job import Job

    set_ids = _collect_set_ids(input_data.get("annotation_sets"))
    if not set_ids:
        raise ValueError("rasterize_annotation_set requires at least one input annotation set")

    org_id = kwargs.get("organization_id")
    job = Job(
        organization_id=uuid.UUID(str(org_id)),
        type=JobType.RASTERIZE_ANNOTATION_SET.value,
        status="queued",
        config={
            "trigger": "automation",
            "annotation_set_ids": set_ids,
            "reference_dataset_id": config.get("reference_dataset_id") or None,
            "resolution_m": config.get("resolution_m"),
            "dataset_name": (config.get("dataset_name") or "").strip() or None,
            "automation_run_id": kwargs.get("run_id"),
            "automation_step_id": kwargs.get("step_id"),
        },
        total_items=1,
    )
    session.add(job)
    session.flush()

    from app.workers.ingestion.tasks import rasterize_annotation_set
    rasterize_annotation_set.delay(str(job.id))

    return DeferToJob(job_id=str(job.id))
