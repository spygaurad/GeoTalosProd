from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import request
from uuid import UUID

from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.adapters import get_adapter
from app.core.enums import JobStatus
from app.core.geometry import parse_geometry
from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.job_output import JobOutput
from app.models.map import Map
from app.models.map_annotation_set import MapAnnotationSet
from app.models.model_class_mapping import ModelClassMapping
from app.models.project import Project
from app.models.project_annotation_set import ProjectAnnotationSet

logger = logging.getLogger(__name__)


@dataclass
class InferenceResult:
    processed_items: int
    failed_items: int
    output_set_ids: list[UUID]


class ModelManager:
    """Central inference orchestrator: model call -> adapter -> class mapping -> annotations."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _dataset_item_context(self, item: DatasetItem) -> dict[str, Any]:
        geom = item.geometry or {}
        shp = shape(geom) if geom else None
        min_lon, min_lat, max_lon, max_lat = shp.bounds if shp is not None else (0.0, 0.0, 0.0, 0.0)
        props = item.properties_cache or {}
        proj_shape = props.get("proj:shape") or []
        width = proj_shape[1] if isinstance(proj_shape, list) and len(proj_shape) == 2 else None
        height = proj_shape[0] if isinstance(proj_shape, list) and len(proj_shape) == 2 else None
        return {
            "dataset_item_id": str(item.id),
            "stac_item_id": item.stac_item_id,
            "bbox": [min_lon, min_lat, max_lon, max_lat],
            "width": width,
            "height": height,
            "crs": props.get("proj:epsg", "EPSG:4326"),
            "properties_cache": props,
        }

    def _call_model(
        self,
        model: AIModel,
        item: DatasetItem,
        georef_context: dict[str, Any],
    ) -> Any:
        output_config = model.output_config or {}
        if "mock_raw_output" in output_config:
            return output_config["mock_raw_output"]

        if not model.endpoint_url:
            raise ValueError("Model endpoint_url is required unless output_config.mock_raw_output is set")

        body = {
            "dataset_item_id": str(item.id),
            "stac_item_id": item.stac_item_id,
            "s3_uri": item.s3_uri,
            "filename": item.filename,
            "georef_metadata": georef_context,
        }
        req_cfg = model.request_config or {}
        if isinstance(req_cfg.get("payload"), dict):
            body.update(req_cfg["payload"])

        headers = {"Content-Type": "application/json"}
        auth_cfg = model.auth_config or {}
        token = auth_cfg.get("bearer_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = request.Request(
            model.endpoint_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method=str(req_cfg.get("method", "POST")).upper(),
        )
        timeout = float(req_cfg.get("timeout_seconds", 60))
        with request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - controlled URL from DB config
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    def _resolve_class_mapping(self, model_id: UUID) -> dict[str, ModelClassMapping]:
        rows = self.session.execute(
            select(ModelClassMapping).where(ModelClassMapping.model_id == model_id)
        ).scalars().all()
        return {row.model_label: row for row in rows}

    def _validate_geojson_4326(self, geom: dict[str, Any]) -> dict[str, Any]:
        shp = shape(geom)
        if not shp.is_valid:
            shp = shp.buffer(0)
        return json.loads(json.dumps(shp.__geo_interface__))

    def run_job(self, job: Job) -> InferenceResult:
        model = self.session.get(AIModel, job.model_id)
        if model is None or model.deleted_at is not None:
            raise ValueError("Model not found")

        input_refs = job.input_refs or []
        item_ids = [UUID(r["id"]) for r in input_refs if r.get("type") == "dataset_item"]
        if not item_ids:
            raise ValueError("No dataset_item inputs found in job")

        mapping_by_label = self._resolve_class_mapping(model.id)
        output_cfg = dict(model.output_config or {})
        run_cfg = (job.config or {}).get("run_output_config")
        if isinstance(run_cfg, dict):
            output_cfg.update(run_cfg)
        adapter_name = output_cfg.get("adapter", "platform_passthrough")
        adapter_config = output_cfg.get("adapter_config") or {}
        adapter = get_adapter(adapter_name)

        project_id = output_cfg.get("project_id")
        map_id = output_cfg.get("map_id")
        mount_on_map = bool(output_cfg.get("mount_on_map", False))
        model_meta = {
            "model_id": str(model.id),
            "model_name": model.name,
            "model_version": model.version,
            "framework": model.framework,
            "adapter": adapter_name,
        }

        processed = 0
        failed = 0
        output_set_ids: list[UUID] = []
        total = len(item_ids)
        job.status = JobStatus.RUNNING
        job.started_at = self._now()
        job.total_items = total
        self.session.commit()

        for idx, item_id in enumerate(item_ids, start=1):
            item = self.session.get(DatasetItem, item_id)
            if item is None or item.organization_id != job.organization_id or not item.is_active:
                failed += 1
                continue
            try:
                context = self._dataset_item_context(item)
                raw_output = self._call_model(model, item, context)
                normalized = adapter.convert_fn(raw_output, adapter_config, context)
                predictions = normalized.get("predictions") or []

                annotation_set = AnnotationSet(
                    organization_id=job.organization_id,
                    schema_id=model.annotation_schema_id,
                    dataset_id=item.dataset_id,
                    dataset_item_id=item.id,
                    source_type="model",
                    model_id=model.id,
                    job_id=job.id,
                    name=f"{model.name}::{item.stac_item_id}",
                    description=f"Model inference output for item {item.stac_item_id}",
                )
                self.session.add(annotation_set)
                self.session.flush()

                if project_id:
                    project = self.session.get(Project, UUID(project_id))
                    if project and project.organization_id == job.organization_id:
                        self.session.add(
                            ProjectAnnotationSet(
                                project_id=project.id,
                                annotation_set_id=annotation_set.id,
                                linked_by=job.created_by_user_id,
                            )
                        )
                if mount_on_map and map_id:
                    map_row = self.session.get(Map, UUID(map_id))
                    if map_row:
                        project = self.session.get(Project, map_row.project_id)
                        if project and project.organization_id == job.organization_id:
                            self.session.add(
                                MapAnnotationSet(
                                    map_id=map_row.id,
                                    annotation_set_id=annotation_set.id,
                                    visible=True,
                                    opacity=1.0,
                                    z_index=0,
                                )
                            )

                created_annotations = 0
                for pred in predictions:
                    label = str(pred.get("label", ""))
                    mapping = mapping_by_label.get(label)
                    if mapping is None:
                        continue
                    confidence = float(pred.get("confidence", 1.0) or 1.0)
                    if (
                        mapping.confidence_threshold is not None
                        and confidence < mapping.confidence_threshold
                    ):
                        continue
                    geom = pred.get("geometry")
                    if not isinstance(geom, dict):
                        continue
                    geom_4326 = self._validate_geojson_4326(geom)
                    properties = dict(pred.get("properties") or {})
                    properties.update(
                        {
                            "source_label": label,
                            "georef_metadata": context,
                            "model_meta": model_meta,
                        }
                    )
                    self.session.add(
                        Annotation(
                            annotation_set_id=annotation_set.id,
                            class_id=mapping.annotation_class_id,
                            geometry=parse_geometry(geom_4326),
                            confidence=confidence,
                            properties=properties,
                            created_by_job_id=job.id,
                        )
                    )
                    created_annotations += 1

                self.session.add(
                    JobOutput(
                        job_id=job.id,
                        output_type="annotation_set",
                        output_id=annotation_set.id,
                    )
                )
                self.session.commit()
                output_set_ids.append(annotation_set.id)
                processed += 1
                logger.info(
                    "inference_item_processed job_id=%s item_id=%s created_annotations=%s",
                    job.id,
                    item.id,
                    created_annotations,
                )
            except Exception as exc:  # noqa: BLE001
                self.session.rollback()
                failed += 1
                logger.exception(
                    "inference_item_failed job_id=%s item_id=%s error=%s",
                    job.id,
                    item_id,
                    exc,
                )

            job.processed_items = processed
            job.failed_items = failed
            job.progress = idx / total if total else 1.0
            self.session.commit()

        if processed == 0:
            job.status = JobStatus.FAILED
            job.logs = "No outputs were generated."
        else:
            job.status = JobStatus.COMPLETED
            job.logs = f"Generated {processed} annotation set(s). Failed items: {failed}"
        job.finished_at = self._now()
        self.session.commit()

        return InferenceResult(
            processed_items=processed,
            failed_items=failed,
            output_set_ids=output_set_ids,
        )
