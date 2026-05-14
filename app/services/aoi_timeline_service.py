import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from redis import Redis

from app.config import settings
from app.models.dataset_item import DatasetItem

logger = logging.getLogger(__name__)

_manifest_client: Redis | None = None


def _redis_client() -> Redis:
    global _manifest_client
    if _manifest_client is None:
        _manifest_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _manifest_client


class AOITimelineService:
    TTL_SECONDS = 60 * 60

    @staticmethod
    def build_manifest_payload(
        *,
        aoi_id: UUID,
        bbox_4326: list[float],
        render_config: dict | None,
        dataset_items: list[DatasetItem],
    ) -> dict[str, Any]:
        frames = [
            {
                "dataset_item_id": str(item.id),
                "dataset_id": str(item.dataset_id),
                "stac_item_id": item.stac_item_id,
                "stac_collection_id": item.stac_collection_id,
                "item_datetime": item.item_datetime.isoformat() if item.item_datetime else None,
                "geometry": item.geometry,
            }
            for item in dataset_items
        ]
        return {
            "aoi_id": str(aoi_id),
            "bbox_4326": bbox_4326,
            "render_config": render_config or {},
            "frame_count": len(frames),
            "frames": frames,
            "prepared_at": datetime.utcnow().isoformat() + "Z",
        }

    @classmethod
    def manifest_key(
        cls,
        *,
        aoi_id: UUID,
        bbox_4326: list[float],
        render_config: dict | None,
        dataset_item_ids: list[UUID],
    ) -> str:
        identity = {
            "aoi_id": str(aoi_id),
            "bbox_4326": bbox_4326,
            "render_config": render_config or {},
            "dataset_item_ids": [str(item_id) for item_id in dataset_item_ids],
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return f"aoi_timeline_manifest:{aoi_id}:{digest}"

    @classmethod
    def store_manifest(cls, key: str, payload: dict[str, Any]) -> str:
        try:
            _redis_client().setex(key, cls.TTL_SECONDS, json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("aoi_timeline_manifest_store_failed key=%s error=%s", key, exc)
        return key

    @classmethod
    def get_manifest(cls, key: str) -> dict[str, Any] | None:
        try:
            raw = _redis_client().get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("aoi_timeline_manifest_get_failed key=%s error=%s", key, exc)
            return None
        if not raw:
            return None
        return json.loads(raw)
