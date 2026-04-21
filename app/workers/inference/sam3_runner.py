"""SAM3 inference orchestration for a single job.

Called by ``run_inference_job`` Celery task. Decouples SAM3-specific logic
from the generic Celery task wrapper so the task itself stays thin.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import shape as shp_shape
from sqlalchemy import select

from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.model_class_mapping import ModelClassMapping
from app.schemas.inference import SAM3PromptPCS, SAM3PromptPVS
from app.services.sam3_client import SAM3Client

logger = logging.getLogger(__name__)


def _build_class_map(session, model_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, float | None]]:
    """Return {model_label: (annotation_class_id, per_class_threshold)} for this model."""
    rows = session.execute(
        select(
            ModelClassMapping.model_label,
            ModelClassMapping.annotation_class_id,
            ModelClassMapping.confidence_threshold,
        ).where(ModelClassMapping.model_id == model_id)
    ).all()
    return {r.model_label: (r.annotation_class_id, r.confidence_threshold) for r in rows}


def _resolve_s3_uri(dataset_item: DatasetItem) -> str:
    """Prefer the explicit s3_uri column, fall back to properties_cache assets."""
    if getattr(dataset_item, "s3_uri", None):
        return dataset_item.s3_uri
    props = dataset_item.properties_cache or {}
    assets = props.get("assets") or {}
    for key in ("data", "image", "visual", "cog"):
        asset = assets.get(key) or {}
        if asset.get("href"):
            return asset["href"]
    raise ValueError(f"DatasetItem {dataset_item.id} has no resolvable S3 URI")


def run_sam3_inference(
    session,
    job: Job,
    annotation_set: AnnotationSet,
    ai_model: AIModel,
    dataset_item: DatasetItem,
    cfg: dict,
) -> dict:
    """Execute one SAM3 inference request and persist outputs.

    Returns a summary dict: {annotation_count, mask_s3_uri, warnings}.
    """
    from app.workers.inference import sam3_io

    warnings: list[str] = []
    task_type = cfg["task_type"]
    output_format = cfg.get("output_format", "vector")
    confidence_threshold = float(cfg.get("confidence_threshold", 0.5))
    aoi_geometry = cfg.get("aoi_geometry")

    # 1. Load raster chip (optionally clipped to AOI)
    s3_uri = _resolve_s3_uri(dataset_item)
    array, raster_meta = sam3_io.load_raster_chip(s3_uri, aoi_geom_4326=aoi_geometry)
    image_b64 = sam3_io.array_to_b64_tiff(array, raster_meta)

    # 2. Call SAM3
    client = SAM3Client(ai_model)
    return_format = "geojson" if output_format == "vector" else "mask_png_b64"

    if task_type == "pcs":
        prompt = SAM3PromptPCS.model_validate(cfg["prompt_pcs"])
        sam_response = client.run_pcs(
            image_b64_tiff=image_b64,
            aoi_bbox_4326=raster_meta["bbox_4326"],
            prompt=prompt,
            confidence_threshold=confidence_threshold,
            return_format=return_format,
        )
    elif task_type == "pvs":
        prompt = SAM3PromptPVS.model_validate(cfg["prompt_pvs"])
        pixel_points = (
            sam3_io.lonlat_to_pixel(prompt.points, raster_meta["transform"], raster_meta["crs"])
            if prompt.points else None
        )
        pixel_boxes = (
            [sam3_io.bbox_to_pixel(box, raster_meta["transform"], raster_meta["crs"])
             for box in prompt.boxes]
            if prompt.boxes else None
        )
        sam_response = client.run_pvs(
            image_b64_tiff=image_b64,
            aoi_bbox_4326=raster_meta["bbox_4326"],
            pixel_points=pixel_points,
            point_labels=prompt.point_labels,
            pixel_boxes=pixel_boxes,
            confidence_threshold=confidence_threshold,
            return_format=return_format,
        )
    else:
        raise ValueError(f"Unsupported task_type: {task_type}")

    # 3. Persist outputs
    if output_format == "vector":
        count = _persist_vector_instances(
            session, job, annotation_set, ai_model.id, sam_response, warnings
        )
        return {"annotation_count": count, "mask_s3_uri": None, "warnings": warnings}

    # raster_cog output
    mask_uri, sidecar_uri = _persist_mask_cog(
        session, job, annotation_set, ai_model.id, sam_response, raster_meta, warnings
    )
    return {
        "annotation_count": 0,
        "mask_s3_uri": mask_uri,
        "sidecar_s3_uri": sidecar_uri,
        "warnings": warnings,
    }


def _persist_vector_instances(
    session,
    job: Job,
    annotation_set: AnnotationSet,
    model_id: uuid.UUID,
    sam_response: dict,
    warnings: list[str],
) -> int:
    """Create Annotation rows for each instance, resolving class via ModelClassMapping."""
    class_map = _build_class_map(session, model_id)
    instances = sam_response.get("instances") or []
    created = 0
    skipped_labels: set[str] = set()

    for inst in instances:
        label = inst.get("label")
        confidence = inst.get("confidence")
        geometry = inst.get("geometry")
        if not label or geometry is None:
            warnings.append(f"instance missing label or geometry: {inst}")
            continue

        mapping = class_map.get(label)
        if mapping is None:
            skipped_labels.add(label)
            continue
        class_id, per_class_threshold = mapping

        if per_class_threshold is not None and confidence is not None and confidence < per_class_threshold:
            continue

        try:
            shp = shp_shape(geometry)
        except Exception as exc:
            warnings.append(f"bad geometry for label={label}: {exc}")
            continue

        annotation = Annotation(
            annotation_set_id=annotation_set.id,
            class_id=class_id,
            geometry=from_shape(shp, srid=4326),
            confidence=confidence,
            properties={"model_label": label, "model_id": str(model_id)},
            created_by_job_id=job.id,
        )
        session.add(annotation)
        created += 1

    if skipped_labels:
        warnings.append(
            f"labels without ModelClassMapping were skipped: {sorted(skipped_labels)}"
        )

    session.flush()
    return created


def _persist_mask_cog(
    session,
    job: Job,
    annotation_set: AnnotationSet,
    model_id: uuid.UUID,
    sam_response: dict,
    raster_meta: dict,
    warnings: list[str],
) -> tuple[str, str | None]:
    """Decode mask, write COG + sidecar JSON to S3, update annotation_set.raster_config."""
    from app.config import settings
    from app.workers.inference import sam3_io

    mask_b64 = sam_response.get("mask_png_b64")
    if not mask_b64:
        raise ValueError("SAM3 response missing 'mask_png_b64' for raster_cog output")

    mask = sam3_io.decode_mask_png(mask_b64)

    # COG goes under org bucket at annotation-sets/<id>/mask.tif
    bucket = f"{settings.S3_BUCKET_PREFIX}{annotation_set.organization_id}"
    mask_s3_uri = f"s3://{bucket}/annotation-sets/{annotation_set.id}/mask.tif"
    sidecar_s3_uri = f"s3://{bucket}/annotation-sets/{annotation_set.id}/mask.aux.json"

    raster_config = sam3_io.mask_to_cog(
        mask=mask,
        transform=raster_meta["transform"],
        crs=raster_meta["crs"],
        s3_uri=mask_s3_uri,
    )

    # Build instance metadata using ModelClassMapping
    class_map = _build_class_map(session, model_id)
    instances = sam_response.get("instances") or []
    instance_metadata: dict[int, dict] = {}
    skipped_labels: set[str] = set()
    for inst in instances:
        instance_id = inst.get("instance_id")
        if instance_id is None:
            continue
        label = inst.get("label")
        mapping = class_map.get(label) if label else None
        class_id_str: str | None = None
        if mapping is not None:
            class_id_str = str(mapping[0])
        elif label:
            skipped_labels.add(label)
        instance_metadata[int(instance_id)] = {
            "label": label,
            "confidence": inst.get("confidence"),
            "class_id": class_id_str,
        }

    if skipped_labels:
        warnings.append(
            f"labels without ModelClassMapping kept in sidecar only: {sorted(skipped_labels)}"
        )

    sam3_io.write_mask_sidecar(sidecar_s3_uri, instance_metadata)

    raster_config["sidecar_s3_uri"] = sidecar_s3_uri
    annotation_set.raster_config = raster_config
    session.flush()
    return mask_s3_uri, sidecar_s3_uri
