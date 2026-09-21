"""Celery task: export a verified AnnotationSetCollection into a YOLO-format
training dataset (images + labels + data.yaml) in S3.

Sync (WorkerSession / psycopg2), duplicating the read-side filtering
``geoops.yolo_service.YoloExportService`` performs async in the request
path — same split ``geoops/tasks.py`` documents for aoi-scan/anomaly-
detection: the async httpx/session helpers are FastAPI-request-path only
and can't be reused inside a sync Celery task. The pure geometry math
(``geometry_to_yolo_bbox`` / ``geometry_to_yolo_polygon``) *is* shared —
those functions don't touch the session.

Scope today: produces a ready-to-train dataset in S3 and records its
manifest on ``job.config["result"]``. It does NOT call yolo-service — that
service doesn't exist yet. That's the next step, which extends this task
rather than replacing it.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from urllib import parse, request as urlrequest

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping, shape
from sqlalchemy import func, select

from app.config import settings
from app.core.enums import JobStatus
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.services import storage_service
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import TRAINING

from geoops.export_common import partition_member_sets_sync, stable_bucket
from geoops.yolo_service import geometry_to_yolo_bbox, geometry_to_yolo_polygon

logger = logging.getLogger(__name__)

# A single padded crop per dataset_item (not tiled like inference patches) —
# simpler, correct v1. Sparse items with annotations spread far apart across
# a huge orthomosaic won't tile well under this scheme; revisit with
# PatchService-style tiling if that turns out to matter in practice.
MAX_CROP_PX = 1280
PAD_FRACTION = 0.10


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _crop_dims(bbox: list[float], max_px: int) -> tuple[int, int]:
    minx, miny, maxx, maxy = bbox
    span_x, span_y = maxx - minx, maxy - miny
    if span_x <= 0 or span_y <= 0:
        raise ValueError("degenerate bbox")
    if span_x >= span_y:
        width = max_px
        height = max(1, round(max_px * span_y / span_x))
    else:
        height = max_px
        width = max(1, round(max_px * span_x / span_y))
    return width, height


def _pad_bbox(bbox: list[float], item_bbox: list[float], fraction: float) -> list[float]:
    minx, miny, maxx, maxy = bbox
    pad_x = (maxx - minx) * fraction
    pad_y = (maxy - miny) * fraction
    return [
        max(item_bbox[0], minx - pad_x),
        max(item_bbox[1], miny - pad_y),
        min(item_bbox[2], maxx + pad_x),
        min(item_bbox[3], maxy + pad_y),
    ]


def _fetch_crop_png(item: DatasetItem, bbox: list[float], width_px: int, height_px: int) -> bytes:
    # Same asset/bidx convention geoops.service._crop_png_b64 uses: "data" is
    # this codebase's standard single-asset COG key, and forcing an explicit
    # 3-band RGB selection avoids rio-tiler PNG-encoder errors on band counts
    # TiTiler can't auto-select a sane default for.
    bbox_csv = ",".join(str(float(v)) for v in bbox)
    base_url = settings.TITILER_URL.rstrip("/")
    endpoint = (
        f"{base_url}/collections/{item.stac_collection_id}"
        f"/items/{item.stac_item_id}/bbox/{bbox_csv}/{width_px}x{height_px}.png"
    )
    params = {"assets": "data", "asset_bidx": "data|1,2,3"}
    url = f"{endpoint}?{parse.urlencode(params)}"
    req = urlrequest.Request(url, method="GET")
    with urlrequest.urlopen(req, timeout=60.0) as resp:  # nosec B310
        data = resp.read()
        if not data:
            raise ValueError("Empty crop image from TiTiler")
        return data


def _gather_rows(
    session, org_id: uuid.UUID, collection_id: uuid.UUID
) -> tuple[list[dict], list[dict], AnnotationSetCollection]:
    """Sync equivalent of YoloExportService's manifest build.

    Returns (rows, skipped_sets, collection). Each row:
    {item: DatasetItem, class_id, class_name, geometry: GeoJSON dict}.
    """
    collection = session.get(AnnotationSetCollection, collection_id)
    if collection is None or collection.organization_id != org_id:
        raise ValueError("AnnotationSetCollection not found")

    included_ids, skipped = partition_member_sets_sync(session, collection)
    if not included_ids:
        return [], skipped, collection

    item_id_expr = func.coalesce(Annotation.dataset_item_id, AnnotationSet.dataset_item_id)
    query = (
        select(Annotation, AnnotationClass.name.label("class_name"), item_id_expr.label("item_id"))
        .select_from(Annotation)
        .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
        .join(AnnotationClass, AnnotationClass.id == Annotation.class_id)
        .where(Annotation.annotation_set_id.in_(included_ids), Annotation.deleted_at.is_(None))
    )

    rows: list[dict] = []
    items_cache: dict[uuid.UUID, DatasetItem | None] = {}
    for ann, class_name, item_id in session.execute(query).all():
        if item_id is None:
            continue
        if item_id not in items_cache:
            items_cache[item_id] = session.get(DatasetItem, item_id)
        item = items_cache[item_id]
        if item is None or not item.is_active:
            continue
        rows.append(
            {
                "item": item,
                "class_id": ann.class_id,
                "class_name": class_name,
                "geometry": mapping(to_shape(ann.geometry)),
            }
        )
    return rows, skipped, collection


@celery_app.task(bind=True, queue=TRAINING, max_retries=1, default_retry_delay=30)
def run_finetune_export(self, job_id: str) -> None:
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.warning("run_finetune_export: job %s not found", job_id)
            return

        req = (job.config or {}).get("request") or {}
        task_type = req.get("task", "detect")
        val_split = float(req.get("val_split", 0.15))
        collection_id = uuid.UUID(req["annotation_set_collection_id"])

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        session.commit()

        try:
            rows, skipped, _collection = _gather_rows(session, job.organization_id, collection_id)
            if not rows:
                job.status = JobStatus.FAILED
                job.logs = "No eligible (verified, resolvable-item) annotations to export."
                job.config = {**(job.config or {}), "result": {"sets_skipped": skipped}}
                job.finished_at = _now()
                session.commit()
                return

            by_item: dict[uuid.UUID, list[dict]] = defaultdict(list)
            for row in rows:
                by_item[row["item"].id].append(row)

            # Deterministic, name-ordered class index — matches
            # YoloExportPreviewResponse's ordering so preview and export agree.
            name_by_class: dict[uuid.UUID, str] = {}
            for row in rows:
                name_by_class.setdefault(row["class_id"], row["class_name"])
            class_ids_used = sorted(name_by_class, key=lambda cid: name_by_class[cid])
            class_index = {cid: i for i, cid in enumerate(class_ids_used)}
            class_names = [name_by_class[cid] for cid in class_ids_used]

            job.total_items = len(by_item)
            session.commit()

            train_count = 0
            val_count = 0
            errors: list[str] = []
            processed = 0

            for item_id, item_rows in by_item.items():
                item = item_rows[0]["item"]
                try:
                    if not item.geometry:
                        raise ValueError("dataset_item has no geometry")
                    item_bbox = list(shape(item.geometry).bounds)

                    ann_geoms = [shape(r["geometry"]) for r in item_rows]
                    union_bounds = [
                        min(g.bounds[0] for g in ann_geoms),
                        min(g.bounds[1] for g in ann_geoms),
                        max(g.bounds[2] for g in ann_geoms),
                        max(g.bounds[3] for g in ann_geoms),
                    ]
                    crop_bbox = _pad_bbox(union_bounds, item_bbox, PAD_FRACTION)
                    width_px, height_px = _crop_dims(crop_bbox, MAX_CROP_PX)

                    png_bytes = _fetch_crop_png(item, crop_bbox, width_px, height_px)

                    label_lines: list[str] = []
                    for r in item_rows:
                        idx = class_index[r["class_id"]]
                        try:
                            if task_type == "segment":
                                coords = geometry_to_yolo_polygon(r["geometry"], crop_bbox)
                                label_lines.append(
                                    " ".join([str(idx)] + [f"{c:.6f}" for c in coords])
                                )
                            else:
                                cx, cy, w, h = geometry_to_yolo_bbox(r["geometry"], crop_bbox)
                                label_lines.append(f"{idx} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
                        except ValueError as exc:
                            errors.append(f"{item_id}: {exc}")

                    if not label_lines:
                        processed += 1
                        job.processed_items = processed
                        job.progress = processed / job.total_items if job.total_items else 1.0
                        session.commit()
                        continue

                    is_val = stable_bucket(str(item_id)) < int(val_split * 100)
                    split = "val" if is_val else "train"
                    if is_val:
                        val_count += 1
                    else:
                        train_count += 1

                    base = f"training-runs/{job.id}/{split}"
                    storage_service.upload_bytes(
                        job.organization_id, f"{base}/images/{item_id}.png", png_bytes, "image/png"
                    )
                    storage_service.upload_bytes(
                        job.organization_id,
                        f"{base}/labels/{item_id}.txt",
                        "\n".join(label_lines).encode("utf-8"),
                        "text/plain",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "finetune_export_item_failed job_id=%s item_id=%s", job.id, item_id
                    )
                    errors.append(f"{item_id}: {exc}")

                processed += 1
                job.processed_items = processed
                job.failed_items = len(errors)
                job.progress = processed / job.total_items if job.total_items else 1.0
                session.commit()

            data_yaml = (
                f"path: training-runs/{job.id}\n"
                "train: train/images\n"
                "val: val/images\n"
                f"nc: {len(class_names)}\n"
                f"names: {class_names!r}\n"
            )
            storage_service.upload_bytes(
                job.organization_id,
                f"training-runs/{job.id}/data.yaml",
                data_yaml.encode("utf-8"),
                "text/yaml",
            )

            job.config = {
                **(job.config or {}),
                "result": {
                    "s3_prefix": f"training-runs/{job.id}",
                    "task": task_type,
                    "classes": [
                        {"class_id": str(cid), "name": name, "index": class_index[cid]}
                        for cid, name in zip(class_ids_used, class_names, strict=True)
                    ],
                    "train_items": train_count,
                    "val_items": val_count,
                    "sets_skipped": skipped,
                    "errors": errors[:50],
                    "training_triggered": False,
                    "note": (
                        "Dataset exported to S3. Calling yolo-service to actually train "
                        "is a follow-up step, not yet implemented."
                    ),
                },
            }
            if train_count == 0:
                job.status = JobStatus.FAILED
                job.logs = "Export produced zero training images (all items landed in val, or all failed)."
            else:
                job.status = JobStatus.COMPLETED
                job.logs = f"Exported {train_count} train / {val_count} val images. {len(errors)} item errors."
            job.finished_at = _now()
            session.commit()
        except Exception as exc:
            session.rollback()
            job2 = session.get(Job, uuid.UUID(job_id))
            if job2 is not None:
                job2.status = JobStatus.FAILED
                job2.logs = str(exc)[:2000]
                job2.finished_at = _now()
                session.commit()
            raise
