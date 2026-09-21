"""Celery tasks for AOI similarity scans and anomaly detection.

Multi-tile / multi-annotation work — one TiTiler crop + one embed-model call
per item not already cached — so both tasks run as Celery jobs rather than
inline in a request. HTTP calls are deliberately synchronous
(``urllib.request.urlopen``), mirroring ``app.services.model_manager``'s
worker-side pattern — the async ``httpx`` helpers in ``geoops/service.py``
are FastAPI-request-path only and can't be reused inside a sync Celery task.

Registered with Celery via ``app.workers.celery_app`` (``include`` +
``task_routes``) rather than living under ``app/workers/`` — this keeps the
module self-contained per ``geoops/README.md``.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import UTC, datetime
from urllib import request

from geoalchemy2 import WKTElement
from geoalchemy2.shape import to_shape
from shapely.geometry import box, mapping, shape
from sqlalchemy import func, insert, select

from app.config import settings
from app.core.enums import JobStatus
from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.workers.celery_app import celery_app
from app.workers.db import WorkerSession
from app.workers.queues import EMBEDDING

from geoops.models import Embedding, EmbeddingTile
from geoops.scale import bbox_span_m, canonical_tiles, cosine_distance, nearest_tier

logger = logging.getLogger(__name__)

# Tiles are cropped+embedded in small batches so job.progress is pollable
# during the scan rather than only jumping at the very end — a scan tops out
# at max_patches (<=400) new tiles, far below the 1000-row batches other
# bulk workers use for pure DB inserts.
BATCH_SIZE = 25
MAX_ERROR_SAMPLE = 50


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _intersect(a: list[float], b: list[float]) -> list[float] | None:
    minx, miny = max(a[0], b[0]), max(a[1], b[1])
    maxx, maxy = min(a[2], b[2]), min(a[3], b[3])
    if minx >= maxx or miny >= maxy:
        return None
    return [minx, miny, maxx, maxy]


def _bbox_to_polygon(bbox: list[float]) -> dict:
    minx, miny, maxx, maxy = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny],
        ]],
    }


def _persist_results_annotation_set(
    session,
    job: Job,
    output_class_id: uuid.UUID,
    model_id: uuid.UUID,
    dataset_id: uuid.UUID | None,
    name: str,
    rows: list[dict],
) -> uuid.UUID | None:
    """Persist scan/detection results as a new annotation_set (one per job —
    see embedding_search_changed.md's "one annotation_set per job" decision)
    so they render as a normal map layer via the existing annotation-layer
    pipeline instead of the ad hoc client-side rectangle overlays this
    replaces. ``rows`` are ``{"geometry": shapely geometry, "confidence":
    float, "properties": dict}``. Returns None (and writes nothing) if
    ``rows`` is empty — an empty scan shouldn't create a zero-annotation set.
    """
    if not rows:
        return None

    output_class = session.get(AnnotationClass, output_class_id)
    if output_class is None:
        raise ValueError(f"Output annotation class {output_class_id} not found")

    annotation_set = AnnotationSet(
        organization_id=job.organization_id,
        schema_id=output_class.schema_id,
        dataset_id=dataset_id,
        source_type="analysis",
        model_id=model_id,
        job_id=job.id,
        name=name,
        review_status="raw",
    )
    session.add(annotation_set)
    session.flush()  # populate annotation_set.id for the batch below

    batch = [
        {
            "annotation_set_id": annotation_set.id,
            "class_id": output_class_id,
            "geometry": WKTElement(r["geometry"].wkt, srid=4326),
            "confidence": r.get("confidence"),
            "properties": r.get("properties"),
            "created_by_user_id": None,
            "created_by_job_id": job.id,
        }
        for r in rows
    ]
    session.execute(insert(Annotation.__table__), batch)
    return annotation_set.id


def _fetch_patch_png_b64(item: DatasetItem, bbox: list[float], size_px: int) -> str:
    """Sync equivalent of geoops.service.EmbeddingService._crop_png_b64."""
    bbox_csv = ",".join(str(v) for v in bbox)
    base_url = settings.TITILER_URL.rstrip("/")
    # `assets=data` is required — TiTiler-pgstac 400s without it ("assets must
    # be defined either via expression or assets options"). "data" is this
    # codebase's standard single-asset COG key (see
    # app.services.model_manager's patch_asset default and
    # geoops.service.EmbeddingService._crop_png_b64's sync twin of this call).
    # `asset_bidx=data|1,2,3` forces an explicit 3-band RGB selection —
    # without it TiTiler renders every band of the asset, and rio-tiler's PNG
    # encoder 500s on some band counts (e.g. a 4-band asset: "Could not
    # encode array of shape (4,H,W) ... using PNG driver").
    url = (
        f"{base_url}/collections/{item.stac_collection_id}/items/{item.stac_item_id}"
        f"/bbox/{bbox_csv}/{size_px}x{size_px}.png?assets=data&asset_bidx=data%7C1%2C2%2C3"
    )
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=60.0) as resp:  # nosec B310
        data = resp.read()
        if not data:
            raise ValueError("Empty patch image from TiTiler")
        return base64.b64encode(data).decode("ascii")


def _call_embed_model(model: AIModel, item: DatasetItem, patch_b64: str, bbox: list[float]) -> dict:
    """Sync equivalent of geoops.service.EmbeddingService._call_embed_model."""
    body = {
        "dataset_item_id": str(item.id),
        "stac_item_id": item.stac_item_id,
        "bbox": bbox,
        "patch_image_format": "png",
        "patch_image_base64": patch_b64,
    }
    req_cfg = model.request_config or {}
    if isinstance(req_cfg.get("payload"), dict):
        body.update(req_cfg["payload"])

    headers = {"Content-Type": "application/json"}
    token = (model.auth_config or {}).get("bearer_token")
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
        data = json.loads(resp.read().decode("utf-8"))

    embedding = data.get("embedding")
    embedding_dim = data.get("embedding_dim")
    model_name = data.get("model_name")
    if not isinstance(embedding, list) or not embedding:
        raise ValueError("Embed model response missing a non-empty 'embedding' list")
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("Embed model response missing 'model_name'")
    if not isinstance(embedding_dim, int) or embedding_dim != len(embedding):
        raise ValueError("Embed model response 'embedding_dim' does not match embedding length")
    return {"model_name": model_name, "embedding_dim": embedding_dim, "embedding": embedding}


@celery_app.task(bind=True, queue=EMBEDDING, max_retries=2, default_retry_delay=60)
def run_aoi_scan(self, job_id: str) -> None:
    """Scan an AOI for patches similar to a reference embedding.

    Reads the ``AOIScanRequest`` payload from ``job.config["request"]``
    (JSON-serialized by ``POST /embeddings/aoi-scan``). Every candidate tile
    at the reference's own scale tier is checked against ``embedding_tiles``
    first; only tiles with no existing ``(dataset_item, model, tier, col,
    row)`` row are cropped and embedded, so re-scanning an overlapping AOI
    never redoes work a prior scan already did. Results land in
    ``job.config["result"]`` for the client to read via ``GET /jobs/{id}``.
    """
    job_uuid = uuid.UUID(job_id)

    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("run_aoi_scan: job %s not found", job_id)
            return

        cfg = dict(job.config or {})
        req = cfg.get("request") or {}
        try:
            reference_id = uuid.UUID(req["reference_embedding_id"])
            dataset_item_ids = [uuid.UUID(i) for i in req["dataset_item_ids"]]
            aoi_bbox = req.get("aoi_bbox")
            crop_size_px = int(req.get("crop_size_px", 256))
            top_k = int(req.get("top_k", 20))
            min_similarity = req.get("min_similarity")
            max_patches = int(req.get("max_patches", 200))
            output_class_id = uuid.UUID(req["output_class_id"])
        except (KeyError, ValueError, TypeError) as exc:
            job.status = JobStatus.FAILED
            job.logs = f"Invalid job config: {exc}"
            job.finished_at = _now()
            session.commit()
            return

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        session.commit()

        try:
            reference = session.get(Embedding, reference_id)
            if reference is None or reference.organization_id != job.organization_id:
                raise ValueError(f"Reference embedding {reference_id} not found")

            model = session.get(AIModel, reference.model_id) if reference.model_id else None
            if model is None or not model.endpoint_url:
                raise ValueError("Reference embedding's model no longer exists or has no endpoint_url")

            items = session.execute(
                select(DatasetItem).where(
                    DatasetItem.id.in_(dataset_item_ids),
                    DatasetItem.organization_id == job.organization_id,
                    DatasetItem.is_active.is_(True),
                )
            ).scalars().all()

            # ── Build the full candidate tile list up front ──
            all_tiles: list[tuple[DatasetItem, int, int, list[float]]] = []
            for item in items:
                if not item.geometry:
                    continue
                item_bbox = list(shape(item.geometry).bounds)
                clip_bbox = _intersect(item_bbox, aoi_bbox) if aoi_bbox else item_bbox
                if clip_bbox is None:
                    continue
                for col, row, tile_bbox in canonical_tiles(item_bbox, reference.tile_tier, clip_bbox):
                    all_tiles.append((item, col, row, tile_bbox))

            all_keys = {(item.id, col, row) for item, col, row, _ in all_tiles}
            bbox_by_key = {(item.id, col, row): tile_bbox for item, col, row, tile_bbox in all_tiles}

            # ── Cache check: which tiles already have an EmbeddingTile row? ──
            existing_keys: set[tuple[uuid.UUID, int, int]] = set()
            if all_tiles:
                cached = session.execute(
                    select(
                        EmbeddingTile.dataset_item_id, EmbeddingTile.tile_col, EmbeddingTile.tile_row
                    ).where(
                        EmbeddingTile.dataset_item_id.in_({k[0] for k in all_keys}),
                        EmbeddingTile.model_id == model.id,
                        EmbeddingTile.tile_tier == reference.tile_tier,
                    )
                ).all()
                existing_keys = {(r[0], r[1], r[2]) for r in cached}

            missing = [t for t in all_tiles if (t[0].id, t[1], t[2]) not in existing_keys]
            patches_truncated = False
            if len(missing) > max_patches:
                missing = missing[:max_patches]
                patches_truncated = True

            job.total_items = len(missing)
            job.processed_items = 0
            job.failed_items = 0
            job.progress = 1.0 if not missing else 0.0
            session.commit()

            errors: list[dict] = []
            error_total = 0
            embedded = 0
            batch: list[dict] = []

            for idx, (item, col, row, tile_bbox) in enumerate(missing):
                try:
                    patch_b64 = _fetch_patch_png_b64(item, tile_bbox, crop_size_px)
                    embed_result = _call_embed_model(model, item, patch_b64, tile_bbox)
                    if embed_result["model_name"] != reference.model_name:
                        raise ValueError(
                            f"Model returned embeddings for {embed_result['model_name']!r}, "
                            f"expected {reference.model_name!r}"
                        )
                    batch.append(
                        {
                            "organization_id": job.organization_id,
                            "model_id": model.id,
                            "model_name": embed_result["model_name"],
                            "embedding_dim": embed_result["embedding_dim"],
                            "dataset_item_id": item.id,
                            "tile_tier": reference.tile_tier,
                            "tile_col": col,
                            "tile_row": row,
                            "bbox": _bbox_to_polygon(tile_bbox),
                            "embedding": embed_result["embedding"],
                            "created_by_job_id": job.id,
                        }
                    )
                    embedded += 1
                except Exception as exc:  # noqa: BLE001 — one bad tile shouldn't fail the whole scan
                    error_total += 1
                    if len(errors) < MAX_ERROR_SAMPLE:
                        errors.append(
                            {"dataset_item_id": str(item.id), "bbox": tile_bbox, "error": str(exc)}
                        )

                if len(batch) >= BATCH_SIZE or idx == len(missing) - 1:
                    if batch:
                        session.execute(insert(EmbeddingTile.__table__), batch)
                        batch = []
                    job.processed_items = idx + 1
                    job.progress = (idx + 1) / len(missing)
                    session.commit()

            # ── Rank cached + newly-embedded tiles ──
            matches: list[dict] = []
            if all_keys:
                candidate_rows = session.execute(
                    select(
                        EmbeddingTile.dataset_item_id, EmbeddingTile.tile_col, EmbeddingTile.tile_row,
                        EmbeddingTile.embedding,
                    ).where(
                        EmbeddingTile.dataset_item_id.in_({k[0] for k in all_keys}),
                        EmbeddingTile.model_id == model.id,
                        EmbeddingTile.tile_tier == reference.tile_tier,
                    )
                ).all()
                for dataset_item_id, col, row, embedding in candidate_rows:
                    key = (dataset_item_id, col, row)
                    if key not in all_keys:
                        continue
                    distance = cosine_distance(list(embedding), list(reference.embedding))
                    similarity = 1.0 - distance
                    if min_similarity is not None and similarity < min_similarity:
                        continue
                    matches.append(
                        {
                            "dataset_item_id": str(dataset_item_id),
                            "bbox": bbox_by_key[key],
                            "similarity": similarity,
                            "distance": distance,
                        }
                    )
            matches.sort(key=lambda m: m["similarity"], reverse=True)
            top_matches = matches[:top_k]

            # ── Persist top_k matches as a new annotation_set/layer ──
            # Cosine similarity is -1..1; Annotation.confidence is conventionally
            # 0..1 (it also drives the existing confidence-heatmap visualization
            # mode), so remap onto that range for storage — the raw similarity
            # is still kept in `properties` for anyone who wants the exact value.
            result_dataset_id = items[0].dataset_id if items else None
            annotation_set_id = _persist_results_annotation_set(
                session, job, output_class_id, model.id, result_dataset_id,
                name=f"Embedding Search — {len(top_matches)} matches",
                rows=[
                    {
                        "geometry": box(*m["bbox"]),
                        "confidence": max(0.0, min(1.0, (m["similarity"] + 1.0) / 2.0)),
                        "properties": {
                            "dataset_item_id": m["dataset_item_id"],
                            "similarity": m["similarity"],
                            "distance": m["distance"],
                        },
                    }
                    for m in top_matches
                ],
            )

            cfg["result"] = {
                "reference_embedding_id": str(reference.id),
                "model_name": reference.model_name,
                "patches_scanned": len(all_tiles),
                "patches_cached": len(all_tiles) - len(missing),
                "patches_embedded": embedded,
                "patches_truncated": patches_truncated,
                "errors_total": error_total,
                "errors_sample": errors,
                "matches": top_matches,
                "annotation_set_id": str(annotation_set_id) if annotation_set_id else None,
            }
            job.config = cfg
            job.processed_items = len(missing)
            job.failed_items = error_total
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()

            logger.info(
                "run_aoi_scan done job=%s scanned=%d cached=%d embedded=%d matches=%d",
                job_id, len(all_tiles), len(all_tiles) - len(missing), embedded, len(matches[:top_k]),
            )

        except Exception as exc:
            session.rollback()
            logger.exception("run_aoi_scan failed job=%s", job_id)
            try:
                job = session.get(Job, job_uuid)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.logs = str(exc)[:4000]
                    job.finished_at = _now()
                    session.commit()
            except Exception:
                logger.exception("could not mark job %s as failed", job_id)
            raise


def _centroid(vectors: list[list[float]]) -> list[float]:
    dim = len(vectors[0])
    sums = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            sums[i] += x
    return [s / len(vectors) for s in sums]


@celery_app.task(bind=True, queue=EMBEDDING, max_retries=2, default_retry_delay=60)
def run_anomaly_detection(self, job_id: str) -> None:
    """Find anomalous annotations of one class within a set of dataset items.

    Reads the ``AnomalyDetectionRequest`` payload from
    ``job.config["request"]``. Only embeds annotations a model already
    produced (``AnnotationSet.source_type == 'model'``) — never tiles empty
    space like ``run_aoi_scan`` does. Each annotation is embedded once (an
    existing ``embeddings`` row for the same ``(annotation_id, model_id)`` is
    reused — see ``uq_embeddings_annotation_model``), grouped by scale tier,
    and scored by cosine distance to its group's centroid. Results land in
    ``job.config["result"]``.
    """
    job_uuid = uuid.UUID(job_id)

    with WorkerSession() as session:
        job = session.get(Job, job_uuid)
        if job is None:
            logger.error("run_anomaly_detection: job %s not found", job_id)
            return

        cfg = dict(job.config or {})
        req = cfg.get("request") or {}
        try:
            class_id = uuid.UUID(req["class_id"])
            model_id = uuid.UUID(req["model_id"])
            dataset_item_ids = [uuid.UUID(i) for i in req["dataset_item_ids"]]
            aoi_bbox = req.get("aoi_bbox")
            min_group_size = int(req.get("min_group_size", 3))
            top_k = int(req.get("top_k", 20))
            max_new_embeddings = int(req.get("max_new_embeddings", 300))
            output_class_id = uuid.UUID(req["output_class_id"])
        except (KeyError, ValueError, TypeError) as exc:
            job.status = JobStatus.FAILED
            job.logs = f"Invalid job config: {exc}"
            job.finished_at = _now()
            session.commit()
            return

        job.status = JobStatus.RUNNING
        job.started_at = _now()
        session.commit()

        try:
            model = session.get(AIModel, model_id)
            if model is None or model.organization_id != job.organization_id or not model.endpoint_url:
                raise ValueError("Embedding model not found or has no endpoint_url")

            # Only annotations a model produced, in the requested items, of
            # the requested class — AnnotationSet.organization_id is the real
            # tenant-isolation boundary here (the worker's DB role bypasses
            # RLS, same as every other worker query).
            query = (
                select(Annotation, AnnotationSet.dataset_item_id)
                .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
                .where(
                    Annotation.class_id == class_id,
                    Annotation.deleted_at.is_(None),
                    AnnotationSet.organization_id == job.organization_id,
                    AnnotationSet.source_type == "model",
                    AnnotationSet.dataset_item_id.in_(dataset_item_ids),
                )
                .limit(2000)
            )
            if aoi_bbox:
                envelope = func.ST_MakeEnvelope(*aoi_bbox, 4326)
                query = query.where(func.ST_Intersects(Annotation.geometry, envelope))

            rows = session.execute(query).all()
            annotation_ids = [a.id for a, _ in rows]

            # ── Cache check: which annotations are already embedded by this model? ──
            existing_by_annotation: dict[uuid.UUID, Embedding] = {}
            if annotation_ids:
                existing = session.execute(
                    select(Embedding).where(
                        Embedding.annotation_id.in_(annotation_ids), Embedding.model_id == model.id
                    )
                ).scalars().all()
                existing_by_annotation = {e.annotation_id: e for e in existing}

            missing_all = [(a, item_id) for a, item_id in rows if a.id not in existing_by_annotation]
            cached_count = len(rows) - len(missing_all)
            annotations_truncated = len(missing_all) > max_new_embeddings
            missing = missing_all[:max_new_embeddings]

            job.total_items = len(missing)
            job.processed_items = 0
            job.failed_items = 0
            job.progress = 1.0 if not missing else 0.0
            session.commit()

            items_by_id = {
                item.id: item
                for item in session.execute(
                    select(DatasetItem).where(
                        DatasetItem.id.in_({i for _, i in missing}),
                        DatasetItem.organization_id == job.organization_id,
                    )
                ).scalars().all()
            } if missing else {}

            errors: list[dict] = []
            error_total = 0
            embedded = 0
            batch: list[dict] = []
            new_rows: list[Embedding] = []

            for idx, (annotation, item_id) in enumerate(missing):
                item = items_by_id.get(item_id)
                try:
                    if item is None:
                        raise ValueError(f"Dataset item {item_id} not found")
                    shp = to_shape(annotation.geometry)
                    geom_dict = mapping(shp)
                    bbox = list(shp.bounds)
                    patch_b64 = _fetch_patch_png_b64(item, bbox, 256)
                    embed_result = _call_embed_model(model, item, patch_b64, bbox)
                    row_data = {
                        "organization_id": job.organization_id,
                        "model_id": model.id,
                        "model_name": embed_result["model_name"],
                        "embedding_dim": embed_result["embedding_dim"],
                        "dataset_item_id": item.id,
                        "class_id": class_id,
                        "annotation_id": annotation.id,
                        "source_geometry": geom_dict,
                        "tile_tier": nearest_tier(bbox_span_m(geom_dict)),
                        "embedding": embed_result["embedding"],
                        "created_by_job_id": job.id,
                    }
                    batch.append(row_data)
                    new_rows.append(row_data)
                    embedded += 1
                except Exception as exc:  # noqa: BLE001 — one bad annotation shouldn't fail the whole run
                    error_total += 1
                    if len(errors) < MAX_ERROR_SAMPLE:
                        errors.append({"annotation_id": str(annotation.id), "error": str(exc)})

                if len(batch) >= BATCH_SIZE or idx == len(missing) - 1:
                    if batch:
                        session.execute(insert(Embedding.__table__), batch)
                        batch = []
                    job.processed_items = idx + 1
                    job.progress = (idx + 1) / len(missing)
                    session.commit()

            # ── Score: group by tile_tier, distance-to-centroid within each group ──
            # Re-read new rows' embeddings back (Core insert doesn't return
            # the pgvector value in a form we can reuse directly).
            candidates: list[dict] = []
            for e in existing_by_annotation.values():
                candidates.append({
                    "annotation_id": e.annotation_id, "dataset_item_id": e.dataset_item_id,
                    "tile_tier": e.tile_tier, "bbox": list(shape(e.source_geometry).bounds),
                    "embedding": list(e.embedding), "model_name": e.model_name,
                })
            if new_rows:
                fresh = session.execute(
                    select(Embedding).where(
                        Embedding.annotation_id.in_([r["annotation_id"] for r in new_rows]),
                        Embedding.model_id == model.id,
                    )
                ).scalars().all()
                for e in fresh:
                    candidates.append({
                        "annotation_id": e.annotation_id, "dataset_item_id": e.dataset_item_id,
                        "tile_tier": e.tile_tier, "bbox": list(shape(e.source_geometry).bounds),
                        "embedding": list(e.embedding), "model_name": e.model_name,
                    })
            result_model_name = candidates[0]["model_name"] if candidates else model.name

            groups: dict[int, list[dict]] = {}
            for c in candidates:
                groups.setdefault(c["tile_tier"], []).append(c)

            group_summaries = []
            anomalies: list[dict] = []
            for tier, members in groups.items():
                scored = len(members) >= min_group_size
                group_summaries.append({"tile_tier": tier, "group_size": len(members), "scored": scored})
                if not scored:
                    continue
                centroid = _centroid([m["embedding"] for m in members])
                for m in members:
                    distance = cosine_distance(m["embedding"], centroid)
                    anomalies.append({
                        "annotation_id": str(m["annotation_id"]),
                        "dataset_item_id": str(m["dataset_item_id"]),
                        "tile_tier": tier,
                        "bbox": m["bbox"],
                        "anomaly_score": distance,
                        "similarity_to_group": 1.0 - distance,
                    })
            anomalies.sort(key=lambda a: a["anomaly_score"], reverse=True)
            top_anomalies = anomalies[:top_k]

            # ── Persist top_k flagged annotations as a new annotation_set/layer ──
            # Copies each flagged item's own geometry (not a derived tile) — the
            # result is a second annotation pointing at the same location, tagged
            # with output_class_id, distinct from the original detection so the
            # scanned class's own layer is untouched. anomaly_score is a cosine
            # distance (0..~2 in practice); clamp onto Annotation.confidence's
            # conventional 0..1 range — raw value is kept in `properties`.
            annotation_by_id = {a.id: a for a, _ in rows}
            result_dataset_id = session.execute(
                select(DatasetItem.dataset_id).where(DatasetItem.id.in_(dataset_item_ids)).limit(1)
            ).scalar()
            annotation_set_id = _persist_results_annotation_set(
                session, job, output_class_id, model.id, result_dataset_id,
                name=f"Anomaly Detection — {len(top_anomalies)} flagged",
                rows=[
                    {
                        "geometry": to_shape(annotation_by_id[uuid.UUID(a["annotation_id"])].geometry),
                        "confidence": max(0.0, min(1.0, a["anomaly_score"])),
                        "properties": {
                            "source_annotation_id": a["annotation_id"],
                            "dataset_item_id": a["dataset_item_id"],
                            "tile_tier": a["tile_tier"],
                            "anomaly_score": a["anomaly_score"],
                            "similarity_to_group": a["similarity_to_group"],
                        },
                    }
                    for a in top_anomalies
                ],
            )

            cfg["result"] = {
                "class_id": str(class_id),
                "model_name": result_model_name,
                "annotations_scanned": len(rows),
                "annotations_cached": cached_count,
                "annotations_embedded": embedded,
                "annotations_truncated": annotations_truncated,
                "errors_total": error_total,
                "errors_sample": errors,
                "groups": group_summaries,
                "anomalies": top_anomalies,
                "annotation_set_id": str(annotation_set_id) if annotation_set_id else None,
            }
            job.config = cfg
            job.processed_items = len(missing)
            job.failed_items = error_total
            job.progress = 1.0
            job.status = JobStatus.COMPLETED
            job.finished_at = _now()
            session.commit()

            logger.info(
                "run_anomaly_detection done job=%s scanned=%d embedded=%d anomalies=%d",
                job_id, len(rows), embedded, len(anomalies[:top_k]),
            )

        except Exception as exc:
            session.rollback()
            logger.exception("run_anomaly_detection failed job=%s", job_id)
            try:
                job = session.get(Job, job_uuid)
                if job is not None:
                    job.status = JobStatus.FAILED
                    job.logs = str(exc)[:4000]
                    job.finished_at = _now()
                    session.commit()
            except Exception:
                logger.exception("could not mark job %s as failed", job_id)
            raise


__all__ = ["run_aoi_scan", "run_anomaly_detection"]
