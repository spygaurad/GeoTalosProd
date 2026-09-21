"""Embedding generation + similarity search.

Reaches into ``app/`` only for what it has no reason to duplicate: the DB
models it references by FK (``AIModel``, ``DatasetItem``, ``Annotation``,
``AnnotationClass``, ``AnnotationSchema``), and
``app.services.titiler_service.get_item_bbox_preview`` to crop a patch image
out of a STAC item — the same TiTiler bbox-crop path
``app.services.model_manager`` uses for inference patches.
"""
from __future__ import annotations

import base64
import logging
from typing import Any
from uuid import UUID

import httpx
from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import bad_request, not_found
from app.models.ai_model import AIModel
from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.services.titiler_service import get_item_bbox_preview

from geoops.models import Embedding
from geoops.scale import bbox_span_m, nearest_tier
from geoops.schemas import (
    EmbeddingCreateRequest,
    EmbeddingSearchMatch,
    EmbeddingSearchRequest,
)

logger = logging.getLogger(__name__)


def _bbox_of(geometry: dict[str, Any]) -> list[float]:
    minx, miny, maxx, maxy = shape(geometry).bounds
    return [float(minx), float(miny), float(maxx), float(maxy)]


class EmbeddingService:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ── shared lookups ───────────────────────────────────────────────────

    async def _get_model(self, org_id: UUID, model_id: UUID) -> AIModel:
        model = await self.session.scalar(
            select(AIModel).where(
                AIModel.id == model_id,
                AIModel.organization_id == org_id,
                AIModel.deleted_at.is_(None),
            )
        )
        if model is None:
            raise not_found("Model")
        if not model.endpoint_url:
            raise bad_request("Model has no endpoint_url configured")
        return model

    async def _get_dataset_item(self, org_id: UUID, dataset_item_id: UUID) -> DatasetItem:
        item = await self.session.scalar(
            select(DatasetItem).where(
                DatasetItem.id == dataset_item_id,
                DatasetItem.organization_id == org_id,
                DatasetItem.is_active.is_(True),
            )
        )
        if item is None:
            raise not_found("Dataset item")
        return item

    async def get_embedding(self, org_id: UUID, embedding_id: UUID) -> Embedding:
        return await self._get_embedding(org_id, embedding_id)

    async def _get_embedding(self, org_id: UUID, embedding_id: UUID) -> Embedding:
        row = await self.session.scalar(
            select(Embedding).where(
                Embedding.id == embedding_id,
                Embedding.organization_id == org_id,
            )
        )
        if row is None:
            raise not_found("Embedding")
        return row

    async def _resolve_class_and_annotation(
        self, org_id: UUID, annotation_id: UUID | None, class_id: UUID | None
    ) -> tuple[UUID | None, UUID | None]:
        """Returns (class_id, annotation_id), preferring the annotation's own class."""
        if annotation_id is not None:
            annotation = await self.session.scalar(
                select(Annotation)
                .join(AnnotationSet, AnnotationSet.id == Annotation.annotation_set_id)
                .where(Annotation.id == annotation_id, AnnotationSet.organization_id == org_id)
            )
            if annotation is None:
                raise not_found("Annotation")
            return annotation.class_id, annotation.id

        if class_id is not None:
            cls = await self.session.scalar(
                select(AnnotationClass)
                .join(AnnotationSchema, AnnotationSchema.id == AnnotationClass.schema_id)
                .where(AnnotationClass.id == class_id, AnnotationSchema.organization_id == org_id)
            )
            if cls is None:
                raise not_found("Annotation class")
            return cls.id, None

        return None, None

    # ── embed model call ─────────────────────────────────────────────────

    async def _crop_png_b64(self, item: DatasetItem, bbox: list[float], size_px: int) -> str:
        # TiTiler-pgstac 400s with "assets must be defined either via expression
        # or assets options" if `assets` is omitted — "data" is this codebase's
        # standard single-asset COG key (see app.services.model_manager's
        # patch_asset default). `asset_bidx` forces an explicit 3-band RGB
        # selection: without it, TiTiler renders every band of the "data"
        # asset, and rio-tiler's PNG encoder 500s on some band counts (e.g.
        # 4-band assets — "Could not encode array of shape (4,H,W) ... using
        # PNG driver"). The embed contract expects an RGB patch regardless of
        # the source asset's actual band count, same as
        # app.services.model_manager's rendering-config-derived asset_bidx.
        try:
            image_bytes = await get_item_bbox_preview(
                item.stac_collection_id, item.stac_item_id, bbox=bbox,
                width=size_px, height=size_px, assets="data", asset_bidx="data|1,2,3",
            )
        except RuntimeError as exc:
            raise bad_request(f"Failed to crop patch from TiTiler: {exc}") from exc
        if not image_bytes:
            raise bad_request("Empty patch image from TiTiler")
        return base64.b64encode(image_bytes).decode("ascii")

    async def _call_embed_model(
        self, model: AIModel, item: DatasetItem, patch_image_b64: str, bbox: list[float]
    ) -> dict[str, Any]:
        body = {
            "dataset_item_id": str(item.id),
            "stac_item_id": item.stac_item_id,
            "bbox": bbox,
            "patch_image_format": "png",
            "patch_image_base64": patch_image_b64,
        }
        req_cfg = model.request_config or {}
        if isinstance(req_cfg.get("payload"), dict):
            body.update(req_cfg["payload"])

        headers = {"Content-Type": "application/json"}
        token = (model.auth_config or {}).get("bearer_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        timeout = float(req_cfg.get("timeout_seconds", 60))
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                str(req_cfg.get("method", "POST")).upper(), model.endpoint_url, json=body, headers=headers
            )
        if resp.status_code != 200:
            logger.error("embed_model_request_failed model_id=%s status=%s body=%s", model.id, resp.status_code, resp.text[:500])
            raise bad_request(f"Embed model request failed: HTTP {resp.status_code}")

        data = resp.json()
        embedding = data.get("embedding")
        embedding_dim = data.get("embedding_dim")
        model_name = data.get("model_name")
        if not isinstance(embedding, list) or not embedding:
            raise bad_request("Embed model response missing a non-empty 'embedding' list")
        if not isinstance(model_name, str) or not model_name:
            raise bad_request("Embed model response missing 'model_name'")
        if not isinstance(embedding_dim, int) or embedding_dim != len(embedding):
            raise bad_request("Embed model response 'embedding_dim' does not match embedding length")
        return {"model_name": model_name, "embedding_dim": embedding_dim, "embedding": embedding}

    # ── create ────────────────────────────────────────────────────────────

    async def create_embedding(
        self, org_id: UUID, user_id: UUID | None, payload: EmbeddingCreateRequest
    ) -> Embedding:
        model = await self._get_model(org_id, payload.model_id)
        item = await self._get_dataset_item(org_id, payload.dataset_item_id)
        class_id, annotation_id = await self._resolve_class_and_annotation(
            org_id, payload.annotation_id, payload.class_id
        )

        # An annotation only ever needs one embedding per model (unique index
        # uq_embeddings_annotation_model) — the anomaly-detection job relies
        # on this same one-per-(annotation,model) invariant, so re-embedding
        # here returns the existing row instead of hitting a constraint error.
        if annotation_id is not None:
            existing = await self.session.scalar(
                select(Embedding).where(
                    Embedding.annotation_id == annotation_id, Embedding.model_id == model.id
                )
            )
            if existing is not None:
                return existing

        bbox = _bbox_of(payload.geometry)
        patch_b64 = await self._crop_png_b64(item, bbox, payload.crop_size_px)
        embed_result = await self._call_embed_model(model, item, patch_b64, bbox)

        row = Embedding(
            organization_id=org_id,
            model_id=model.id,
            model_name=embed_result["model_name"],
            embedding_dim=embed_result["embedding_dim"],
            dataset_item_id=item.id,
            class_id=class_id,
            annotation_id=annotation_id,
            source_geometry=payload.geometry,
            tile_tier=nearest_tier(bbox_span_m(payload.geometry)),
            embedding=embed_result["embedding"],
            created_by_user_id=user_id,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    # ── search over the bank ─────────────────────────────────────────────

    async def search(self, org_id: UUID, payload: EmbeddingSearchRequest) -> list[EmbeddingSearchMatch]:
        reference = await self._get_embedding(org_id, payload.embedding_id)

        distance_expr = Embedding.embedding.cosine_distance(reference.embedding)
        query = select(Embedding, distance_expr.label("distance")).where(
            Embedding.organization_id == org_id,
            Embedding.model_name == reference.model_name,
            # Same real-world scale only — a 5m object and a 500m object are
            # never comparable just because they were both cropped to the
            # same crop_size_px pixel grid. See geoops/scale.py.
            Embedding.tile_tier == reference.tile_tier,
        )
        if payload.class_id is not None:
            query = query.where(Embedding.class_id == payload.class_id)
        if payload.dataset_id is not None:
            query = query.join(DatasetItem, DatasetItem.id == Embedding.dataset_item_id).where(
                DatasetItem.dataset_id == payload.dataset_id
            )
        if payload.exclude_self:
            query = query.where(Embedding.id != reference.id)

        query = query.order_by(distance_expr.asc()).limit(payload.top_k)
        rows = (await self.session.execute(query)).all()

        return [
            EmbeddingSearchMatch(
                embedding_id=row.Embedding.id,
                dataset_item_id=row.Embedding.dataset_item_id,
                class_id=row.Embedding.class_id,
                annotation_id=row.Embedding.annotation_id,
                source_geometry=row.Embedding.source_geometry,
                similarity=1.0 - float(row.distance),
                distance=float(row.distance),
            )
            for row in rows
        ]

    # AOI scan is no longer handled here — it's a Celery job (multi-tile,
    # HTTP-bound work doesn't belong inline in a request). See
    # geoops/tasks.py::run_aoi_scan and POST /embeddings/aoi-scan in
    # geoops/api.py, which only creates the Job and enqueues the task.
