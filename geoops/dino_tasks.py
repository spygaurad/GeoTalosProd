"""Celery task: export training data for ONE class-specific head (DINOv2
tier) — crops + normalized bbox/mask targets + manifest.json to S3 — then
call geoops-service to actually train + eval the head, and register the
result in the model catalogue (ai_models + model_class_mappings) so it's
immediately usable through the existing ModelManager inference pipeline.

Sync (WorkerSession / psycopg2), same split as geoops.yolo_tasks documents:
the async session/service helpers are FastAPI-request-path only. Reuses
geoops.yolo_tasks's crop-fetching helpers (_fetch_crop_png, _crop_dims) —
that logic is generic image-cropping, not YOLO-specific, and
geoops.yolo_service's geometry_to_yolo_bbox/geometry_to_yolo_polygon for the
same reason (pure normalized-coordinate math, not YOLO-label-format
specific). HTTP calls to geoops-service are deliberately synchronous
(urllib), mirroring geoops/tasks.py's established pattern for sync Celery
tasks calling an external model endpoint.

Crops are built by tiling each exhaustively-annotated AOI (see
_build_aoi_tile_groups) at a fixed, real-world-meter scale, not by padding
around individual objects: a tile sized to one object's own footprint can
only ever show that one object, so a detector trained on it never sees
"background" and never learns what *isn't* the class — which is exactly
why an earlier version of this pipeline produced detectors with near-100%
recall and near-0% precision (they fired on everything). Because an AOI is
exhaustively labeled for this class, a tile with zero annotations inside it
is a trustworthy negative example, not a guess — and a tile can and often
does contain more than one object, giving the detector genuine multi-object
training examples instead of one-object-per-frame crops. An annotation with
no ``aoi_id`` (not drawn inside a tracked AOI) can't participate in this —
there's no way to know its surroundings were exhaustively labeled — so it's
reported as skipped rather than silently used.

Only the detect (FCOS) tier is wired to geoops-service — segment isn't
implemented there yet, so a segment-task job still stops after export with
training_triggered=False, same as before.
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import UTC, datetime
from urllib import request as urlrequest

from geoalchemy2.shape import to_shape
from shapely.geometry import box as shapely_box, mapping, shape
from sqlalchemy import func, select

from app.config import settings
from app.core.enums import JobStatus
from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.job_output import JobOutput
from app.models.map_aoi import MapAOI
from app.models.model_class_mapping import ModelClassMapping
from app.services import storage_service
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import TRAINING

from geoops.export_common import partition_member_sets_sync, stable_bucket
from geoops.scale import DEFAULT_TIERS_M, bbox_coords_span_m, canonical_tiles, nearest_tier
from geoops.yolo_service import geometry_to_yolo_bbox, geometry_to_yolo_polygon
from geoops.yolo_tasks import _crop_dims, _fetch_crop_png

logger = logging.getLogger(__name__)

MAX_CROP_PX = 1280
# Cap on how many negative (background-only) tiles are kept per exported
# job, relative to positive (object-containing) tiles — an exhaustively
# tiled AOI is mostly empty space, so without a cap negatives would swamp
# the (usually tiny, few-shot) positive count many times over.
NEG_TO_POS_RATIO_CAP = 2.0
MIN_NEGATIVE_TILES = 5
DEFAULT_BACKBONE_KEY = "dinov2-vits14"  # small variant — 21M params vs. vitb14's 86.6M, much faster on CPU
BACKBONE_HF_IDS = {
    "dinov2-vits14": "facebook/dinov2-small",
    "dinov2-vitb14": "facebook/dinov2-base",
}


def _call_geoops_service(path: str, body: dict, timeout: float = 1800.0) -> dict:
    url = f"{settings.GEOOPS_SERVICE_URL.rstrip('/')}{path}"
    req = urlrequest.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.GEOOPS_SERVICE_API_TOKEN}",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _resolve_tier(rows: list[dict]) -> int:
    """Snap this class's typical object size up to the nearest scale tier
    (geoops.scale.DEFAULT_TIERS_M) — the tier used both for the training-
    crop tile grid and, via ``patch_size_m`` on the registered head, for
    inference scanning, so train/scan scale match by construction. A tile
    sized to exactly one small object's own extent can only ever show that
    one object; the smallest tier (10m, roughly a zoom 21-23 web tile)
    leaves room for several nearby objects in one frame instead.
    """
    spans = sorted(bbox_coords_span_m(list(shape(r["geometry"]).bounds)) for r in rows)
    median_span = spans[len(spans) // 2] if spans else DEFAULT_TIERS_M[0]
    return nearest_tier(median_span)


def _build_aoi_tile_groups(item, aoi_rows: list[dict], aoi_bbox: list[float], aoi_geometry: dict | None, tier: int) -> list[dict]:
    """Tile one exhaustively-annotated AOI (on one dataset_item) into a
    deterministic, non-overlapping grid at the given scale tier (see
    geoops.scale.canonical_tiles), and assign every one of this AOI's own
    class annotations to its home tile by centroid — so each object lands in
    exactly one tile, never duplicated across neighbors.

    A tile with zero rows is a real negative (the AOI is exhaustively
    labeled for this class, so "nothing here" is trustworthy); a tile can
    hold more than one row, giving genuine multi-object training examples.

    Returns [{"crop_bbox", "rows", "tile_key"}, ...].
    """
    item_bbox = list(shape(item.geometry).bounds)
    clip_bbox = [
        max(item_bbox[0], aoi_bbox[0]),
        max(item_bbox[1], aoi_bbox[1]),
        min(item_bbox[2], aoi_bbox[2]),
        min(item_bbox[3], aoi_bbox[3]),
    ]
    if clip_bbox[0] >= clip_bbox[2] or clip_bbox[1] >= clip_bbox[3]:
        return []

    tiles = canonical_tiles(item_bbox, tier, clip_bbox=clip_bbox)
    if not tiles:
        return []

    aoi_poly = shape(aoi_geometry) if aoi_geometry else shapely_box(*aoi_bbox)
    row_shapes = [shape(r["geometry"]) for r in aoi_rows]

    groups: list[dict] = []
    for col, row_idx, tile_bbox in tiles:
        tile_poly = shapely_box(*tile_bbox)
        if not tile_poly.intersects(aoi_poly):
            continue
        tile_rows = [aoi_rows[i] for i, s in enumerate(row_shapes) if tile_poly.contains(s.centroid)]
        groups.append({"crop_bbox": tile_bbox, "rows": tile_rows, "tile_key": f"{item.id}:{col}:{row_idx}"})
    return groups


def _build_export_groups(session, org_id: uuid.UUID, item_rows: list[dict]) -> tuple[list[dict], int, int]:
    """Turn this class's raw annotation rows into the final list of export
    tile groups, sourced only from annotations tagged with an ``aoi_id``
    (see module docstring for why). Negatives are subsampled to a bounded
    ratio against positives so an exhaustively-empty AOI doesn't swamp a
    small few-shot positive count.

    Returns (groups, skipped_no_aoi_count, tier).
    """
    aoi_rows = [r for r in item_rows if r.get("aoi_id")]
    skipped_no_aoi = len(item_rows) - len(aoi_rows)
    if not aoi_rows:
        return [], skipped_no_aoi, 0

    tier = _resolve_tier(aoi_rows)

    by_item_aoi: dict[tuple[uuid.UUID, str], list[dict]] = {}
    for row in aoi_rows:
        by_item_aoi.setdefault((row["item"].id, row["aoi_id"]), []).append(row)

    aoi_cache: dict[str, MapAOI | None] = {}
    all_groups: list[dict] = []
    for (_item_id, aoi_id), group_rows in by_item_aoi.items():
        item = group_rows[0]["item"]
        if not item.geometry:
            continue
        if aoi_id not in aoi_cache:
            try:
                aoi = session.get(MapAOI, uuid.UUID(aoi_id))
            except (ValueError, TypeError):
                aoi = None
            aoi_cache[aoi_id] = aoi if aoi is not None and aoi.organization_id == org_id and aoi.deleted_at is None else None
        aoi = aoi_cache[aoi_id]
        if aoi is None:
            skipped_no_aoi += len(group_rows)
            continue
        for group in _build_aoi_tile_groups(item, group_rows, list(aoi.bbox_4326), aoi.geometry, tier):
            all_groups.append({"item": item, **group})

    positive_groups = [g for g in all_groups if g["rows"]]
    negative_groups = [g for g in all_groups if not g["rows"]]
    neg_cap = int(max(len(positive_groups) * NEG_TO_POS_RATIO_CAP, MIN_NEGATIVE_TILES))
    if len(negative_groups) > neg_cap:
        negative_groups = random.sample(negative_groups, neg_cap)

    return positive_groups + negative_groups, skipped_no_aoi, tier


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _gather_rows_for_class(session, org_id: uuid.UUID, collection_id: uuid.UUID, class_id: uuid.UUID):
    """Sync equivalent of DinoExportService.build_preview, but returns full rows
    (item + geometry) rather than just counts. Returns (rows, skipped, collection, class_name).
    """
    collection = session.get(AnnotationSetCollection, collection_id)
    if collection is None or collection.organization_id != org_id:
        raise ValueError("AnnotationSetCollection not found")

    cls = session.get(AnnotationClass, class_id)
    if cls is None or cls.schema_id != collection.schema_id:
        raise ValueError("AnnotationClass not found for this collection's schema")

    included_ids, skipped = partition_member_sets_sync(session, collection)
    if not included_ids:
        return [], skipped, collection, cls.name

    item_id_expr = func.coalesce(Annotation.dataset_item_id, AnnotationSet.dataset_item_id)
    query = (
        select(Annotation, item_id_expr.label("item_id"))
        .select_from(Annotation)
        .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
        .where(
            Annotation.annotation_set_id.in_(included_ids),
            Annotation.class_id == class_id,
            Annotation.deleted_at.is_(None),
        )
    )

    rows: list[dict] = []
    items_cache: dict[uuid.UUID, DatasetItem | None] = {}
    for ann, item_id in session.execute(query).all():
        if item_id is None:
            continue
        if item_id not in items_cache:
            items_cache[item_id] = session.get(DatasetItem, item_id)
        item = items_cache[item_id]
        if item is None or not item.is_active:
            continue
        rows.append({
            "item": item,
            "annotation_id": ann.id,
            "geometry": mapping(to_shape(ann.geometry)),
            "aoi_id": (ann.properties or {}).get("aoi_id"),
        })
    return rows, skipped, collection, cls.name


def _run_classify_export(session, job: Job, req: dict) -> None:
    """Classifier tier: reuses the same AOI-tile export as the detect tier
    (see _build_export_groups) — a tile with any of this class's annotations
    in it is a positive, an empty tile is a negative. Unlike the detect
    tier, sparse/partial labeling would technically be safe here too (a
    classifier never claims anything about pixels outside the patch it was
    given), but using the same exhaustively-labeled-AOI source as detect
    keeps both tiers trained on the same real scale/data and keeps this
    function simple. See AwakeForestProd's design discussion for the
    classify -> scan -> human review -> train-a-detector bootstrap reasoning.
    """
    eval_split = float(req.get("eval_split", 0.15))
    test_split = float(req.get("test_split", 0.15))
    collection_id = uuid.UUID(req["annotation_set_collection_id"])
    class_id = uuid.UUID(req["class_id"])

    rows, skipped, collection, class_name = _gather_rows_for_class(
        session, job.organization_id, collection_id, class_id
    )
    if not rows:
        job.status = JobStatus.FAILED
        job.logs = "No eligible (verified, resolvable-item) annotations of this class to export."
        job.config = {**(job.config or {}), "result": {"sets_skipped": skipped}}
        job.finished_at = _now()
        session.commit()
        return

    all_groups, skipped_no_aoi, tier = _build_export_groups(session, job.organization_id, rows)
    if not all_groups:
        job.status = JobStatus.FAILED
        job.logs = (
            f"None of this class's {len(rows)} verified annotation(s) are tagged to a tracked AOI. "
            "Draw an AOI, label every instance of this class inside it, then re-run — see the module "
            "docstring in geoops/dino_tasks.py for why an AOI is required."
        )
        job.config = {**(job.config or {}), "result": {"sets_skipped": skipped, "annotations_skipped_no_aoi": skipped_no_aoi}}
        job.finished_at = _now()
        session.commit()
        return

    units: list[dict] = [
        {"item": g["item"], "crop_bbox": g["crop_bbox"], "label": 1 if g["rows"] else 0, "key": g["tile_key"]}
        for g in all_groups
    ]

    job.total_items = len(units)
    session.commit()

    train_count = eval_count = test_count = 0
    errors: list[str] = []
    processed = 0
    manifest_items: list[dict] = []
    scan_patch_size_m = DEFAULT_TIERS_M[tier]

    for unit in units:
        key = unit["key"]
        try:
            width_px, height_px = _crop_dims(unit["crop_bbox"], MAX_CROP_PX)
            png_bytes = _fetch_crop_png(unit["item"], unit["crop_bbox"], width_px, height_px)

            bucket = stable_bucket(key)
            test_cutoff = int(test_split * 100)
            eval_cutoff = test_cutoff + int(eval_split * 100)
            if bucket < test_cutoff:
                split = "test"
                test_count += 1
            elif bucket < eval_cutoff:
                split = "eval"
                eval_count += 1
            else:
                split = "train"
                train_count += 1

            base = f"training-runs/{job.id}/{split}"
            image_key = f"{base}/images/{key.replace(':', '_')}.png"
            storage_service.upload_bytes(job.organization_id, image_key, png_bytes, "image/png")
            manifest_items.append(
                {"item_id": str(unit["item"].id), "split": split, "image": image_key, "label": unit["label"]}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("dino_classify_export_unit_failed job_id=%s key=%s", job.id, key)
            errors.append(f"{key}: {exc}")

        processed += 1
        job.processed_items = processed
        job.failed_items = len(errors)
        job.progress = processed / job.total_items if job.total_items else 1.0
        session.commit()

    manifest = {
        "class_id": str(class_id),
        "class_name": class_name,
        "task": "classify",
        "items": manifest_items,
    }
    storage_service.upload_bytes(
        job.organization_id,
        f"training-runs/{job.id}/manifest.json",
        json.dumps(manifest).encode("utf-8"),
        "application/json",
    )

    base_result = {
        "s3_prefix": f"training-runs/{job.id}",
        "task": "classify",
        "class_id": str(class_id),
        "class_name": class_name,
        "train_items": train_count,
        "eval_items": eval_count,
        "test_items": test_count,
        "sets_skipped": skipped,
        "errors": errors[:50],
        "annotations_skipped_no_aoi": skipped_no_aoi,
        "patch_span_m": DEFAULT_TIERS_M[tier],
    }
    if train_count == 0:
        job.status = JobStatus.FAILED
        job.logs = "Export produced zero training images."
        job.config = {**(job.config or {}), "result": {**base_result, "training_triggered": False}}
        job.finished_at = _now()
        session.commit()
        return

    s3_prefix = f"training-runs/{job.id}"
    backbone_key = DEFAULT_BACKBONE_KEY
    try:
        train_result = _call_geoops_service(
            "/train",
            {
                "org_id": str(job.organization_id),
                "backbone_key": backbone_key,
                "kind": "classify",
                "s3_prefix": s3_prefix,
                "epochs": int(req.get("epochs", 30)),
                "lr": float(req.get("lr", 1e-2)),
            },
        )
        eval_result = _call_geoops_service(
            "/eval",
            {
                "org_id": str(job.organization_id),
                "s3_prefix": s3_prefix,
                "kind": "classify",
                "split": "test",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("dino_classify_train_or_eval_failed job_id=%s", job.id)
        job.status = JobStatus.FAILED
        job.logs = f"Export succeeded but training/eval failed: {exc}"
        job.config = {**(job.config or {}), "result": {**base_result, "training_triggered": False, "error": str(exc)}}
        job.finished_at = _now()
        session.commit()
        return

    backbone_row = session.execute(
        select(AIModel).where(
            AIModel.organization_id == job.organization_id,
            AIModel.type == "backbone",
            AIModel.framework == "dinov2",
            AIModel.deleted_at.is_(None),
        )
    ).scalars().first()
    if backbone_row is None:
        backbone_row = AIModel(
            organization_id=job.organization_id,
            name=f"DINOv2 backbone ({backbone_key})",
            type="backbone",
            framework="dinov2",
            config={"source": "huggingface", "model_id": BACKBONE_HF_IDS.get(backbone_key, backbone_key)},
        )
        session.add(backbone_row)
        session.flush()

    artifact_key = train_result["artifact_key"]
    bucket_name = storage_service.bucket_name(job.organization_id)
    # Scan tiles are sized to EXACTLY the tier used to build the training
    # tiles (scan_patch_size_m, set above) — not a pixel count, and not a
    # post-hoc measurement of what got exported. ModelManager converts this
    # to pixels using each target image's OWN resolution at scan time, so
    # the same head scans correctly across imagery of different ground
    # sample distance, and train/scan scale match by construction.
    head_row = AIModel(
        organization_id=job.organization_id,
        name=req.get("result_model_name") or f"{class_name} (DINOv2 classifier)",
        type="classify_head",
        framework="dinov2",
        backbone_model_id=backbone_row.id,
        artifact_uri=f"s3://{bucket_name}/{artifact_key}",
        endpoint_url=f"{settings.GEOOPS_SERVICE_URL.rstrip('/')}/infer",
        request_config={
            "payload": {
                "org_id": str(job.organization_id),
                "artifact_key": artifact_key,
                "class_label": class_name,
                "kind": "classify",
                "backbone_key": backbone_key,
                "score_thresh": 0.5,
            }
        },
        auth_config={"bearer_token": settings.GEOOPS_SERVICE_API_TOKEN},
        output_config={"adapter": "platform_passthrough", "patch_size_m": scan_patch_size_m},
        config={"kind": "classify", "eval_metrics": eval_result},
        annotation_schema_id=collection.schema_id,
        created_by=job.created_by_user_id,
    )
    session.add(head_row)
    session.flush()

    session.add(ModelClassMapping(model_id=head_row.id, model_label=class_name, annotation_class_id=class_id))
    session.add(JobOutput(job_id=job.id, output_type="ai_model", output_id=head_row.id))
    job.model_id = head_row.id

    job.status = JobStatus.COMPLETED
    job.logs = (
        f"Trained + registered {class_name} classifier (ai_model_id={head_row.id}). "
        f"Accuracy={eval_result.get('accuracy')} on {eval_result.get('num_images', 0)} test images."
    )
    job.config = {
        **(job.config or {}),
        "result": {
            **base_result,
            "training_triggered": True,
            "ai_model_id": str(head_row.id),
            "backbone_model_id": str(backbone_row.id),
            "train_result": train_result,
            "eval_result": eval_result,
        },
    }
    job.finished_at = _now()
    session.commit()


@celery_app.task(bind=True, queue=TRAINING, max_retries=1, default_retry_delay=30)
def run_dino_finetune_export(self, job_id: str) -> None:
    with WorkerSession() as session:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.warning("run_dino_finetune_export: job %s not found", job_id)
            return

        req = (job.config or {}).get("request") or {}
        task_type = req.get("task", "detect")

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        session.commit()

        if task_type == "classify":
            try:
                _run_classify_export(session, job, req)
            except Exception as exc:
                session.rollback()
                job2 = session.get(Job, uuid.UUID(job_id))
                if job2 is not None:
                    job2.status = JobStatus.FAILED
                    job2.logs = str(exc)[:2000]
                    job2.finished_at = _now()
                    session.commit()
                raise
            return

        eval_split = float(req.get("eval_split", 0.15))
        test_split = float(req.get("test_split", 0.15))
        collection_id = uuid.UUID(req["annotation_set_collection_id"])
        class_id = uuid.UUID(req["class_id"])

        try:
            rows, skipped, collection, class_name = _gather_rows_for_class(
                session, job.organization_id, collection_id, class_id
            )
            if not rows:
                job.status = JobStatus.FAILED
                job.logs = "No eligible (verified, resolvable-item) annotations of this class to export."
                job.config = {**(job.config or {}), "result": {"sets_skipped": skipped}}
                job.finished_at = _now()
                session.commit()
                return

            all_groups, skipped_no_aoi, tier = _build_export_groups(session, job.organization_id, rows)
            if not all_groups:
                job.status = JobStatus.FAILED
                job.logs = (
                    f"None of this class's {len(rows)} verified annotation(s) are tagged to a tracked AOI. "
                    "Draw an AOI, label every instance of this class inside it, then re-run — see the "
                    "module docstring in geoops/dino_tasks.py for why an AOI is required."
                )
                job.config = {
                    **(job.config or {}),
                    "result": {"sets_skipped": skipped, "annotations_skipped_no_aoi": skipped_no_aoi},
                }
                job.finished_at = _now()
                session.commit()
                return

            job.total_items = len(all_groups)
            session.commit()

            train_count = 0
            eval_count = 0
            test_count = 0
            errors: list[str] = []
            processed = 0
            manifest_items: list[dict] = []
            scan_patch_size_m = DEFAULT_TIERS_M[tier]

            for group in all_groups:
                item = group["item"]
                crop_bbox = group["crop_bbox"]
                group_rows = group["rows"]
                # Tile's own deterministic key (item + grid col/row) names this
                # crop's files and seeds its split bucket — stable across
                # re-runs, and valid whether the tile is positive or negative
                # (unlike the old per-object anchor id, which only existed for
                # positive crops).
                tile_key = group["tile_key"]
                try:
                    width_px, height_px = _crop_dims(crop_bbox, MAX_CROP_PX)
                    png_bytes = _fetch_crop_png(item, crop_bbox, width_px, height_px)

                    targets: list[dict] = []
                    for r in group_rows:
                        try:
                            if task_type == "segment":
                                coords = geometry_to_yolo_polygon(r["geometry"], crop_bbox)
                                targets.append({"polygon": coords})
                            else:
                                cx, cy, w, h = geometry_to_yolo_bbox(r["geometry"], crop_bbox)
                                targets.append({"bbox": [cx, cy, w, h]})
                        except ValueError as exc:
                            errors.append(f"{tile_key}: {exc}")

                    # Empty `targets` is fine and expected for a negative
                    # (background-only) tile — see _build_aoi_tile_groups. Only
                    # bail out here if this was meant to be a positive tile but
                    # every one of its targets failed to convert.
                    if group_rows and not targets:
                        processed += 1
                        job.processed_items = processed
                        job.progress = processed / job.total_items if job.total_items else 1.0
                        session.commit()
                        continue

                    # Deterministic three-way split by tile (hash-based on the
                    # tile's own key, not the dataset_item) — a single image
                    # contributes many independent tiles to different splits,
                    # which is correct: they're genuinely different sub-regions,
                    # not the same picture duplicated across splits.
                    bucket = stable_bucket(tile_key)
                    test_cutoff = int(test_split * 100)
                    eval_cutoff = test_cutoff + int(eval_split * 100)
                    if bucket < test_cutoff:
                        split = "test"
                        test_count += 1
                    elif bucket < eval_cutoff:
                        split = "eval"
                        eval_count += 1
                    else:
                        split = "train"
                        train_count += 1

                    file_key = tile_key.replace(":", "_")
                    base = f"training-runs/{job.id}/{split}"
                    image_key = f"{base}/images/{file_key}.png"
                    targets_key = f"{base}/targets/{file_key}.json"
                    storage_service.upload_bytes(job.organization_id, image_key, png_bytes, "image/png")
                    storage_service.upload_bytes(
                        job.organization_id,
                        targets_key,
                        json.dumps({"targets": targets}).encode("utf-8"),
                        "application/json",
                    )
                    manifest_items.append(
                        {"item_id": str(item.id), "split": split, "image": image_key, "targets": targets_key}
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "dino_finetune_export_group_failed job_id=%s tile_key=%s", job.id, tile_key
                    )
                    errors.append(f"{tile_key}: {exc}")

                processed += 1
                job.processed_items = processed
                job.failed_items = len(errors)
                job.progress = processed / job.total_items if job.total_items else 1.0
                session.commit()

            manifest = {
                "class_id": str(class_id),
                "class_name": class_name,
                "task": task_type,
                "backbone_model_id": req.get("backbone_model_id"),
                "items": manifest_items,
            }
            storage_service.upload_bytes(
                job.organization_id,
                f"training-runs/{job.id}/manifest.json",
                json.dumps(manifest).encode("utf-8"),
                "application/json",
            )

            base_result = {
                "s3_prefix": f"training-runs/{job.id}",
                "task": task_type,
                "class_id": str(class_id),
                "class_name": class_name,
                "train_items": train_count,
                "eval_items": eval_count,
                "test_items": test_count,
                "sets_skipped": skipped,
                "errors": errors[:50],
                "annotations_skipped_no_aoi": skipped_no_aoi,
                "patch_span_m": scan_patch_size_m,
            }

            if train_count == 0:
                job.status = JobStatus.FAILED
                job.logs = "Export produced zero training images (all items landed in eval/test, or all failed)."
                job.config = {
                    **(job.config or {}),
                    "result": {**base_result, "training_triggered": False},
                }
                job.finished_at = _now()
                session.commit()
                return

            if task_type != "detect":
                job.status = JobStatus.COMPLETED
                job.logs = (
                    f"Exported {train_count} train / {eval_count} eval / {test_count} test images. "
                    f"{len(errors)} item errors. Training not triggered: geoops-service only "
                    "implements the detect (FCOS) tier so far."
                )
                job.config = {
                    **(job.config or {}),
                    "result": {
                        **base_result,
                        "training_triggered": False,
                        "note": "segment task not yet implemented on geoops-service.",
                    },
                }
                job.finished_at = _now()
                session.commit()
                return

            s3_prefix = f"training-runs/{job.id}"
            backbone_key = DEFAULT_BACKBONE_KEY
            try:
                train_result = _call_geoops_service(
                    "/train",
                    {
                        "org_id": str(job.organization_id),
                        "backbone_key": backbone_key,
                        "kind": "fcos",
                        "s3_prefix": s3_prefix,
                        "epochs": int(req.get("epochs", 20)),
                        "lr": float(req.get("lr", 1e-3)),
                    },
                )
                eval_result = _call_geoops_service(
                    "/eval",
                    {
                        "org_id": str(job.organization_id),
                        "s3_prefix": s3_prefix,
                        "split": "test",
                        "iou_thresh": 0.5,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("dino_train_or_eval_failed job_id=%s", job.id)
                job.status = JobStatus.FAILED
                job.logs = f"Export succeeded but training/eval failed: {exc}"
                job.config = {
                    **(job.config or {}),
                    "result": {**base_result, "training_triggered": False, "error": str(exc)},
                }
                job.finished_at = _now()
                session.commit()
                return

            # ── Catalogue registration ──────────────────────────────────
            # Find-or-create the shared backbone row (one per org, reused
            # across every class head trained against it), then create this
            # head's own row + its class binding — ModelManager can call it
            # through the existing inference pipeline with zero code changes.
            backbone_row = session.execute(
                select(AIModel).where(
                    AIModel.organization_id == job.organization_id,
                    AIModel.type == "backbone",
                    AIModel.framework == "dinov2",
                    AIModel.deleted_at.is_(None),
                )
            ).scalars().first()
            if backbone_row is None:
                backbone_row = AIModel(
                    organization_id=job.organization_id,
                    name=f"DINOv2 backbone ({backbone_key})",
                    type="backbone",
                    framework="dinov2",
                    config={"source": "huggingface", "model_id": BACKBONE_HF_IDS.get(backbone_key, backbone_key)},
                )
                session.add(backbone_row)
                session.flush()

            artifact_key = train_result["artifact_key"]
            bucket = storage_service.bucket_name(job.organization_id)
            # Scan tiles are sized to EXACTLY the tier used to build the
            # training tiles (scan_patch_size_m, set above) — not a pixel
            # count, and not a post-hoc measurement of what got exported.
            # ModelManager converts this to pixels using each target image's
            # OWN resolution at scan time, so the same head scans correctly
            # across imagery of different ground sample distance, and
            # train/scan scale match by construction.
            head_row = AIModel(
                organization_id=job.organization_id,
                name=req.get("result_model_name") or f"{class_name} (DINOv2 head)",
                type="detect_head",
                framework="dinov2",
                backbone_model_id=backbone_row.id,
                artifact_uri=f"s3://{bucket}/{artifact_key}",
                endpoint_url=f"{settings.GEOOPS_SERVICE_URL.rstrip('/')}/infer",
                request_config={
                    "payload": {
                        "org_id": str(job.organization_id),
                        "artifact_key": artifact_key,
                        "class_label": class_name,
                        "backbone_key": backbone_key,
                        "score_thresh": 0.3,
                    }
                },
                auth_config={"bearer_token": settings.GEOOPS_SERVICE_API_TOKEN},
                output_config={"adapter": "platform_passthrough", "patch_size_m": scan_patch_size_m},
                config={"kind": "fcos", "eval_metrics": eval_result},
                annotation_schema_id=collection.schema_id,
                created_by=job.created_by_user_id,
            )
            session.add(head_row)
            session.flush()

            session.add(
                ModelClassMapping(model_id=head_row.id, model_label=class_name, annotation_class_id=class_id)
            )
            session.add(JobOutput(job_id=job.id, output_type="ai_model", output_id=head_row.id))
            job.model_id = head_row.id

            job.status = JobStatus.COMPLETED
            job.logs = (
                f"Trained + registered {class_name} head (ai_model_id={head_row.id}). "
                f"AP50={eval_result.get('ap50')} on {eval_result.get('num_images', 0)} test images."
            )
            job.config = {
                **(job.config or {}),
                "result": {
                    **base_result,
                    "training_triggered": True,
                    "ai_model_id": str(head_row.id),
                    "backbone_model_id": str(backbone_row.id),
                    "train_result": train_result,
                    "eval_result": eval_result,
                },
            }
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
