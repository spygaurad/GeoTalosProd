"""Generic, model-agnostic inference orchestrator.

``ModelManager.run_job`` is the single code path for every model. It reads the
model's JSONB config off ``ai_models`` to decide how to call the endpoint and
which adapter converts the raw response, so onboarding a new framework is
pure configuration — no code changes here.

Flow per Job:
  for each dataset_item in job.input_refs:
      generate patches via PatchService
      for each patch:
          fetch patch PNG from TiTiler
          HTTP POST to model.endpoint_url (generic body)
          adapter.convert_fn normalizes to platform predictions
          write Annotation rows (filtered by ModelClassMapping + threshold)
      write JobOutput(annotation_set_id)
"""
from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import parse, request
from uuid import UUID

from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.adapters import get_adapter
from app.config import settings
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
from app.services.patch_service import PatchService

logger = logging.getLogger(__name__)


@dataclass
class InferenceResult:
    processed_items: int
    failed_items: int
    output_set_ids: list[UUID]


class ModelManager:
    """Central inference orchestrator: patch -> model -> adapter -> annotations."""

    _DEFAULT_PATCH_SIZE = 1024
    _DEFAULT_MAX_PATCHES = 1024

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _dataset_item_context(self, item: DatasetItem) -> dict[str, Any]:
        geom = item.geometry or {}
        shp = shape(geom) if geom else None
        min_lon, min_lat, max_lon, max_lat = (
            shp.bounds if shp is not None else (0.0, 0.0, 0.0, 0.0)
        )
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

    def _build_patch_metadata(
        self,
        *,
        item: DatasetItem,
        context: dict[str, Any],
        output_cfg: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        patch_size_px = int(output_cfg.get("patch_size_px", self._DEFAULT_PATCH_SIZE))
        stride_px_cfg = output_cfg.get("stride_px")
        stride_px = int(stride_px_cfg) if stride_px_cfg is not None else None
        max_patches = int(output_cfg.get("max_patches_per_item", self._DEFAULT_MAX_PATCHES))
        aoi_bbox = output_cfg.get("aoi_bbox")
        effective_aoi = self._resolve_effective_aoi(
            item_bbox=context["bbox"],
            item_width=context.get("width"),
            item_height=context.get("height"),
            aoi_bbox=aoi_bbox,
        )

        if effective_aoi["skip_item"]:
            return [], False, effective_aoi

        windows, capped = PatchService.generate(
            item_id=str(item.id),
            item_bbox=context["bbox"],
            item_width=context.get("width"),
            item_height=context.get("height"),
            patch_size_px=patch_size_px,
            stride_px=stride_px,
            max_patches=max_patches,
            clip_bbox=effective_aoi["effective_bbox"],
        )
        return [w.as_dict() for w in windows], capped, effective_aoi

    def _resolve_effective_aoi(
        self,
        *,
        item_bbox: list[float],
        item_width: int | None,
        item_height: int | None,
        aoi_bbox: list[float] | None,
    ) -> dict[str, Any]:
        if aoi_bbox is None:
            return {
                "requested_bbox": None,
                "effective_bbox": item_bbox,
                "used_full_item": True,
                "skip_item": False,
            }

        minx = max(item_bbox[0], aoi_bbox[0])
        miny = max(item_bbox[1], aoi_bbox[1])
        maxx = min(item_bbox[2], aoi_bbox[2])
        maxy = min(item_bbox[3], aoi_bbox[3])
        if minx >= maxx or miny >= maxy:
            return {
                "requested_bbox": aoi_bbox,
                "effective_bbox": None,
                "used_full_item": False,
                "skip_item": True,
            }

        effective_bbox = [float(minx), float(miny), float(maxx), float(maxy)]
        used_full_item = effective_bbox == [float(v) for v in item_bbox]

        if not used_full_item and item_width and item_height:
            lon_span = item_bbox[2] - item_bbox[0]
            lat_span = item_bbox[3] - item_bbox[1]
            if lon_span > 0 and lat_span > 0:
                window_width = max(
                    1, math.ceil(((effective_bbox[2] - effective_bbox[0]) / lon_span) * item_width)
                )
                window_height = max(
                    1, math.ceil(((effective_bbox[3] - effective_bbox[1]) / lat_span) * item_height)
                )
                if window_width < 1 or window_height < 1:
                    return {
                        "requested_bbox": aoi_bbox,
                        "effective_bbox": None,
                        "used_full_item": False,
                        "skip_item": True,
                    }

        return {
            "requested_bbox": aoi_bbox,
            "effective_bbox": effective_bbox,
            "used_full_item": used_full_item,
            "skip_item": False,
        }

    def _fetch_patch_png(
        self,
        *,
        item: DatasetItem,
        patch: dict[str, Any],
        asset_name: str = "data",
    ) -> str:
        """Fetch patch PNG bytes from TiTiler and return base64 payload."""
        bbox = patch.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("Patch bbox is required")

        width_px = int(patch.get("width_px") or 0)
        height_px = int(patch.get("height_px") or 0)
        if width_px <= 0 or height_px <= 0:
            raise ValueError("Patch pixel dimensions must be > 0")

        bbox_csv = ",".join(str(float(v)) for v in bbox)
        dim_part = f"{width_px}x{height_px}.png"
        base_url = settings.TITILER_URL.rstrip("/")
        endpoint = (
            f"{base_url}/collections/{item.stac_collection_id}"
            f"/items/{item.stac_item_id}/bbox/{bbox_csv}/{dim_part}"
        )
        query = parse.urlencode({"assets": asset_name})
        crop_url = f"{endpoint}?{query}"

        req = request.Request(crop_url, method="GET")
        with request.urlopen(req, timeout=60.0) as resp:  # nosec B310
            data = resp.read()
            if not data:
                raise ValueError("Empty patch image from TiTiler")
            return base64.b64encode(data).decode("ascii")

    def _call_model(
        self,
        model: AIModel,
        item: DatasetItem,
        georef_context: dict[str, Any],
        patch: dict[str, Any],
        patch_image_base64: str,
    ) -> Any:
        output_config = model.output_config or {}
        if "mock_raw_output" in output_config:
            return output_config["mock_raw_output"]

        if not model.endpoint_url:
            raise ValueError(
                "Model endpoint_url is required unless output_config.mock_raw_output is set"
            )

        body = {
            "dataset_item_id": str(item.id),
            "stac_item_id": item.stac_item_id,
            "s3_uri": item.s3_uri,
            "filename": item.filename,
            "georef_metadata": georef_context,
            "patch": patch,
            "patch_image_format": "png",
            "patch_image_base64": patch_image_base64,
        }
        req_cfg = model.request_config or {}
        if isinstance(req_cfg.get("payload"), dict):
            body.update(req_cfg["payload"])

        headers = {"Content-Type": "application/json"}
        auth_cfg = model.auth_config or {}
        token = auth_cfg.get("bearer_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        http_req = request.Request(
            model.endpoint_url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method=str(req_cfg.get("method", "POST")).upper(),
        )
        timeout = float(req_cfg.get("timeout_seconds", 60))
        with request.urlopen(http_req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode("utf-8")
            return json.loads(raw)

    def _resolve_class_mapping(self, model_id: UUID) -> dict[str, ModelClassMapping]:
        rows = (
            self.session.execute(
                select(ModelClassMapping).where(ModelClassMapping.model_id == model_id)
            )
            .scalars()
            .all()
        )
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
        patch_asset = str(output_cfg.get("patch_asset", "data"))
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
                job.processed_items = processed
                job.failed_items = failed
                job.progress = idx / total if total else 1.0
                self.session.commit()
                continue

            try:
                context = self._dataset_item_context(item)
                patch_metadata, capped, aoi_state = self._build_patch_metadata(
                    item=item,
                    context=context,
                    output_cfg=output_cfg,
                )
                if aoi_state["skip_item"]:
                    failed += 1
                    job.processed_items = processed
                    job.failed_items = failed
                    job.progress = idx / total if total else 1.0
                    logger.info(
                        "inference_item_skipped_no_aoi_overlap job_id=%s item_id=%s requested_aoi=%s",
                        job.id,
                        item.id,
                        aoi_state["requested_bbox"],
                    )
                    self.session.commit()
                    continue

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
                for patch in patch_metadata:
                    patch_context = {
                        **context,
                        "bbox": patch["bbox"],
                        "width": patch["width_px"],
                        "height": patch["height_px"],
                        "patch": patch,
                        "parent_bbox": context["bbox"],
                        "parent_width": context.get("width"),
                        "parent_height": context.get("height"),
                        "requested_aoi_bbox": aoi_state["requested_bbox"],
                        "effective_aoi_bbox": aoi_state["effective_bbox"],
                        "used_full_item": aoi_state["used_full_item"],
                    }
                    try:
                        patch_png_b64 = self._fetch_patch_png(
                            item=item,
                            patch=patch,
                            asset_name=patch_asset,
                        )
                        raw_output = self._call_model(
                            model=model,
                            item=item,
                            georef_context=patch_context,
                            patch=patch,
                            patch_image_base64=patch_png_b64,
                        )
                        normalized = adapter.convert_fn(raw_output, adapter_config, patch_context)
                        predictions = normalized.get("predictions") or []
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "inference_patch_failed job_id=%s item_id=%s patch_id=%s error=%s",
                            job.id,
                            item.id,
                            patch.get("patch_id"),
                            exc,
                        )
                        continue

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
                                "georef_metadata": patch_context,
                                "model_meta": model_meta,
                                "patch": patch,
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
                    "inference_item_processed job_id=%s item_id=%s patches=%s "
                    "patches_capped=%s created_annotations=%s effective_aoi=%s "
                    "used_full_item=%s",
                    job.id,
                    item.id,
                    len(patch_metadata),
                    capped,
                    created_annotations,
                    aoi_state["effective_bbox"],
                    aoi_state["used_full_item"],
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
