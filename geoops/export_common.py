"""Shared AnnotationSetCollection validation logic — the "which member sets
are actually eligible" filtering used by every training-data exporter
(currently geoops.yolo_service, geoops.dino_service), so the multi-tier
architecture doesn't re-derive this once per tier.

Pure DB-read logic (async, request-path). No torch/ultralytics import.
"""
from __future__ import annotations

import hashlib
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.annotation_set_collection_item import AnnotationSetCollectionItem
from app.schemas.common import ORMModel


def stable_bucket(key: str, modulus: int = 100) -> int:
    """Deterministic bucket in [0, modulus) for a string key — used for
    train/eval/test split assignment (geoops.dino_tasks, geoops.yolo_tasks).

    Python's built-in hash() is NOT safe for this: string hashing is
    randomized per-process by default (PYTHONHASHSEED), so the same
    item_id would land in a different split every time a worker restarts.
    sha256 is stable across processes/restarts/machines.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


class SkippedSet(ORMModel):
    annotation_set_id: UUID
    name: str
    reason: str = Field(
        description="Why this member set was excluded, e.g. "
        "'review_status=raw (must be verified)' or 'schema_id mismatch with collection'."
    )


async def partition_member_sets(
    session: AsyncSession, collection: AnnotationSetCollection
) -> tuple[list[UUID], list[SkippedSet]]:
    """Split a collection's member sets into (eligible ids, skipped-with-reason).

    Re-validates schema match defensively rather than trusting that every
    link went through annotation_set_collection_service.add_set_to_collection
    (the only write path that enforces it today) — there's no DB-level
    constraint backing that invariant. See geoops/yolo_service.py's original
    design notes.
    """
    member_sets = (
        await session.scalars(
            select(AnnotationSet)
            .join(
                AnnotationSetCollectionItem,
                AnnotationSetCollectionItem.annotation_set_id == AnnotationSet.id,
            )
            .where(
                AnnotationSetCollectionItem.collection_id == collection.id,
                AnnotationSet.deleted_at.is_(None),
            )
        )
    ).all()

    included: list[UUID] = []
    skipped: list[SkippedSet] = []
    for s in member_sets:
        if s.schema_id != collection.schema_id:
            skipped.append(
                SkippedSet(annotation_set_id=s.id, name=s.name, reason="schema_id mismatch with collection")
            )
        elif s.review_status != "verified":
            skipped.append(
                SkippedSet(
                    annotation_set_id=s.id,
                    name=s.name,
                    reason=f"review_status={s.review_status} (must be verified)",
                )
            )
        else:
            included.append(s.id)
    return included, skipped


def partition_member_sets_sync(session, collection: AnnotationSetCollection) -> tuple[list[UUID], list[dict]]:
    """Sync (WorkerSession/psycopg2) equivalent of partition_member_sets, for
    Celery tasks. Returns skipped sets as plain dicts (JSON-serializable
    directly onto job.config), not SkippedSet models, since sync task code
    writes straight into JSONB.
    """
    member_sets = session.scalars(
        select(AnnotationSet)
        .join(
            AnnotationSetCollectionItem,
            AnnotationSetCollectionItem.annotation_set_id == AnnotationSet.id,
        )
        .where(
            AnnotationSetCollectionItem.collection_id == collection.id,
            AnnotationSet.deleted_at.is_(None),
        )
    ).all()

    included: list[UUID] = []
    skipped: list[dict] = []
    for s in member_sets:
        if s.schema_id != collection.schema_id:
            skipped.append(
                {"annotation_set_id": str(s.id), "name": s.name, "reason": "schema_id mismatch with collection"}
            )
        elif s.review_status != "verified":
            skipped.append(
                {
                    "annotation_set_id": str(s.id),
                    "name": s.name,
                    "reason": f"review_status={s.review_status} (must be verified)",
                }
            )
        else:
            included.append(s.id)
    return included, skipped
