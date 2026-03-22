from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from shapely.geometry import shape as to_shape
from sqlalchemy import func, select

from app.core.enums import JobStatus, JobType, MapLayerSourceType, MapLayerType
from app.core.geometry import parse_geometry
from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.job_output import JobOutput
from app.models.map_layer import MapLayer
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.inference.geometry import bbox_to_polygon, mask_to_polygon
from app.workers.queues import INFERENCE

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class PermanentTaskError(Exception):
    """Errors that should not trigger retries."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _resolve_template_value(path: str, context: dict[str, Any]) -> Any:
    value: Any = context
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return ""
    return value


def _render_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _TEMPLATE_RE.sub(lambda m: str(_resolve_template_value(m.group(1), context)), value)
    if isinstance(value, dict):
        return {k: _render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, context) for v in value]
    return value


def _is_geometry_allowed(geometry: dict[str, Any], allowed_types: list[str] | None) -> bool:
    if not allowed_types:
        return True
    geom_type = to_shape(geometry).geom_type
    if geom_type in allowed_types:
        return True
    if geom_type.startswith("Multi") and geom_type.replace("Multi", "", 1) in allowed_types:
        return True
    return False


def _build_headers(model: AIModel) -> dict[str, str]:
    headers = dict((model.request_config or {}).get("headers") or {})
    auth = model.auth_config or {}
    auth_type = (auth.get("type") or "").lower()
    if auth_type == "bearer" and auth.get("token"):
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif auth_type == "api_key" and auth.get("value"):
        header_name = auth.get("header", "X-API-Key")
        headers[header_name] = auth["value"]
    elif auth.get("secret_ref"):
        logger.warning("model_auth_secret_ref_unresolved model_id=%s", model.id)
    return headers


def _resolve_targets(session, job: Job) -> list[dict[str, Any]]:
    config = job.config or {}
    dataset_id = config.get("dataset_id")
    stac_item_ids = config.get("stac_item_ids") or []

    if dataset_id:
        rows = session.execute(
            select(DatasetItem).where(
                DatasetItem.organization_id == job.organization_id,
                DatasetItem.dataset_id == uuid.UUID(dataset_id),
                DatasetItem.is_active.is_(True),
            )
        ).scalars().all()
        return [
            {
                "stac_item_id": row.stac_item_id,
                "s3_uri": row.s3_uri,
                "geometry": row.geometry,
                "dataset_item_id": str(row.id),
            }
            for row in rows
        ]

    targets: list[dict[str, Any]] = []
    for item_id in stac_item_ids:
        row = session.execute(
            select(DatasetItem).where(
                DatasetItem.organization_id == job.organization_id,
                DatasetItem.stac_item_id == item_id,
                DatasetItem.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if row is not None:
            targets.append(
                {
                    "stac_item_id": row.stac_item_id,
                    "s3_uri": row.s3_uri,
                    "geometry": row.geometry,
                    "dataset_item_id": str(row.id),
                }
            )
        else:
            targets.append({"stac_item_id": item_id, "s3_uri": None, "geometry": None})
    return targets


def _invoke_model(model: AIModel, job: Job, item: dict[str, Any]) -> dict[str, Any]:
    if not model.endpoint_url:
        raise PermanentTaskError("Model has no endpoint_url configured")

    request_config = model.request_config or {}
    method = (request_config.get("method") or "POST").upper()
    timeout = float(request_config.get("timeout_seconds") or 60)
    template = request_config.get("template")

    context = {
        "job": {"id": str(job.id), "config": job.config or {}},
        "item": item,
        "params": (job.config or {}).get("params", {}),
    }

    if template:
        payload = _render_template(template, context)
    else:
        payload = {
            "stac_item_id": item.get("stac_item_id"),
            "s3_uri": item.get("s3_uri"),
            "params": (job.config or {}).get("params", {}),
        }

    headers = _build_headers(model)
    with httpx.Client(timeout=timeout) as client:
        response = client.request(method, model.endpoint_url, json=payload, headers=headers)

    if response.status_code >= 500:
        raise RuntimeError(f"Upstream model server error: {response.status_code}")
    if response.status_code >= 400:
        raise PermanentTaskError(f"Model request failed: {response.status_code} {response.text[:300]}")
    try:
        return response.json()
    except Exception as exc:
        raise PermanentTaskError("Model response is not valid JSON") from exc


def _extract_prediction_records(output: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(output.get("predictions"), list):
        return output["predictions"]

    records: list[dict[str, Any]] = []
    for det in output.get("detections") or []:
        det_copy = dict(det)
        det_copy.setdefault("type", "detection")
        records.append(det_copy)
    for seg in output.get("segmentations") or []:
        seg_copy = dict(seg)
        seg_copy.setdefault("type", "segmentation")
        records.append(seg_copy)
    return records


def _resolve_class_id(
    session,
    *,
    schema_id: uuid.UUID,
    class_id: str | None,
    class_name: str | None,
    auto_create: bool,
) -> uuid.UUID:
    if class_id:
        resolved = session.execute(
            select(AnnotationClass).where(
                AnnotationClass.id == uuid.UUID(class_id),
                AnnotationClass.schema_id == schema_id,
            )
        ).scalar_one_or_none()
        if resolved is None:
            raise PermanentTaskError(f"class_id {class_id} is not in schema {schema_id}")
        return resolved.id

    if not class_name:
        raise PermanentTaskError("Prediction record requires class_id or class_name")

    resolved = session.execute(
        select(AnnotationClass).where(
            AnnotationClass.schema_id == schema_id,
            AnnotationClass.name == class_name,
        )
    ).scalar_one_or_none()
    if resolved is not None:
        return resolved.id

    if not auto_create:
        raise PermanentTaskError(f"Unknown class_name '{class_name}' and auto_create_classes=false")

    created = AnnotationClass(
        id=uuid.uuid4(),
        schema_id=schema_id,
        name=class_name,
    )
    session.add(created)
    session.flush()
    return created.id


@celery_app.task(bind=True, queue=INFERENCE, max_retries=2, default_retry_delay=30)
def run_batch_inference(self, job_id: str):
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.error("inference_job_missing job_id=%s", job_id)
            return
        if job.type != JobType.MODEL_INFERENCE:
            logger.error("inference_job_wrong_type job_id=%s type=%s", job_id, job.type)
            return

        try:
            model = session.get(AIModel, job.model_id) if job.model_id else None
            if model is None:
                raise PermanentTaskError("Model not found for inference job")

            config = job.config or {}
            map_id = uuid.UUID(config["map_id"])
            schema_id = uuid.UUID(config["schema_id"])
            schema = session.get(AnnotationSchema, schema_id)
            if schema is None:
                raise PermanentTaskError("Annotation schema not found")

            targets = _resolve_targets(session, job)
            if not targets:
                raise PermanentTaskError("No input items resolved for inference")

            job.status = JobStatus.RUNNING
            job.started_at = _now()
            job.total_items = len(targets)
            job.processed_items = 0
            job.failed_items = 0
            job.progress = 0.0
            session.commit()

            set_name = config.get("set_name") or f"inference-{job.id}"
            annotation_set = AnnotationSet(
                id=uuid.uuid4(),
                map_id=map_id,
                schema_id=schema_id,
                dataset_id=uuid.UUID(config["dataset_id"]) if config.get("dataset_id") else None,
                stac_item_id=targets[0]["stac_item_id"] if len(targets) == 1 else None,
                name=set_name,
                description=f"Model inference output for job {job.id}",
                created_by_job_id=job.id,
            )
            session.add(annotation_set)
            session.flush()

            session.add(
                JobOutput(job_id=job.id, output_type="annotation_set", output_id=annotation_set.id)
            )

            if config.get("create_overlay_layer", True):
                max_z = session.execute(
                    select(func.max(MapLayer.z_index)).where(MapLayer.map_id == map_id)
                ).scalar_one_or_none()
                layer = MapLayer(
                    id=uuid.uuid4(),
                    map_id=map_id,
                    name=f"{set_name} overlay",
                    layer_type=MapLayerType.ANNOTATION,
                    source_type=MapLayerSourceType.ANNOTATION_SET,
                    annotation_set_id=annotation_set.id,
                    z_index=(max_z or 0) + 1,
                    visible=True,
                    opacity=1.0,
                )
                session.add(layer)
                session.add(JobOutput(job_id=job.id, output_type="map_layer", output_id=layer.id))

            auto_create = bool(config.get("auto_create_classes", False))
            errors: list[str] = []
            created_annotations = 0

            for idx, item in enumerate(targets, start=1):
                try:
                    output = _invoke_model(model, job, item)
                    records = _extract_prediction_records(output)
                    for record in records:
                        record_type = (record.get("type") or "detection").lower()
                        geometry: dict[str, Any] | None = None

                        if record_type == "detection":
                            if "bbox" not in record:
                                raise PermanentTaskError("Detection record missing bbox")
                            geometry = bbox_to_polygon(record["bbox"])
                        elif record_type == "segmentation":
                            if record.get("geometry"):
                                geometry = record["geometry"]
                            elif record.get("mask") is not None:
                                if "bbox" not in record:
                                    raise PermanentTaskError("Segmentation mask record missing bbox")
                                geometry = mask_to_polygon(record["mask"], record["bbox"])
                        else:
                            raise PermanentTaskError(f"Unsupported prediction type: {record_type}")

                        if geometry is None:
                            continue
                        if not _is_geometry_allowed(geometry, schema.geometry_types):
                            raise PermanentTaskError(
                                f"Geometry type not allowed by schema {schema.id}: {schema.geometry_types}"
                            )

                        class_uuid = _resolve_class_id(
                            session,
                            schema_id=schema_id,
                            class_id=record.get("class_id"),
                            class_name=record.get("class_name"),
                            auto_create=auto_create,
                        )

                        props = dict(record.get("properties") or {})
                        props.setdefault("source_stac_item_id", item.get("stac_item_id"))
                        props.setdefault("prediction_type", record_type)
                        session.add(
                            Annotation(
                                id=uuid.uuid4(),
                                annotation_set_id=annotation_set.id,
                                class_id=class_uuid,
                                geometry=parse_geometry(geometry),
                                confidence=record.get("confidence"),
                                properties=props,
                            )
                        )
                        created_annotations += 1
                except PermanentTaskError as exc:
                    errors.append(f"{item.get('stac_item_id')}: {exc}")
                    job.failed_items += 1
                except Exception as exc:  # pragma: no cover - retry path
                    raise self.retry(exc=exc)

                job.processed_items = idx
                job.progress = idx / len(targets)
                if idx % 10 == 0 or idx == len(targets):
                    session.commit()

            if created_annotations == 0:
                raise PermanentTaskError("Inference completed with zero annotations")

            if errors:
                job.logs = "\n".join(errors[:200])

            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()
        except PermanentTaskError as exc:
            session.rollback()
            # If previous loop commits already persisted partial artifacts,
            # remove them on terminal failure so retries start cleanly.
            stale_sets = session.execute(
                select(AnnotationSet).where(AnnotationSet.created_by_job_id == job.id)
            ).scalars().all()
            for stale in stale_sets:
                session.delete(stale)
            stale_outputs = session.execute(
                select(JobOutput).where(
                    JobOutput.job_id == job.id,
                    JobOutput.output_type.in_(["annotation_set", "map_layer"]),
                )
            ).scalars().all()
            for stale in stale_outputs:
                session.delete(stale)
            job.status = JobStatus.FAILED
            job.logs = str(exc)
            job.finished_at = _now()
            session.commit()
        except Exception as exc:  # pragma: no cover - retry path
            session.rollback()
            logger.exception("inference_task_retry job_id=%s", job_id)
            raise self.retry(exc=exc)
