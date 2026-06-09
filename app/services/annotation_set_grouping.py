"""Auto-grouping of annotation sets into schema-scoped collections.

Whenever an annotation set with a schema is created (model inference, raster
vectorization, merge, manual creation), it is automatically linked to the
``AnnotationSetCollection`` for its ``(organization, schema)``. The collection
is created on demand. Grouping is by **schema only** — deliberately independent
of any map, so a collection stays valid even after its maps are deleted.

Both a sync and an async variant are provided because set creation happens in
sync worker/automation contexts (Celery, automation nodes) and async API
contexts (annotation set service).
"""
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.annotation_set_collection import AnnotationSetCollection
from app.models.annotation_set_collection_item import AnnotationSetCollectionItem

logger = logging.getLogger(__name__)


def _collection_name(schema_name: str | None, schema_id: uuid.UUID) -> str:
    return schema_name or f"Schema {str(schema_id)[:8]}"


def ensure_schema_collection_sync(session, annotation_set: AnnotationSet) -> None:
    """Link ``annotation_set`` to its ``(org, schema)`` collection (sync session).

    No-op when the set has no schema. The set must already be flushed so it has
    an id. Safe to call repeatedly — the link is created only once.
    """
    schema_id = annotation_set.schema_id
    if schema_id is None:
        return
    org_id = annotation_set.organization_id

    # A schema can own several collections — users create named series via the
    # UI (and per-run grouping makes one per inference run), so (org, schema) is
    # NOT unique. The auto-grouper just needs a stable home collection: pick the
    # oldest one (the on-demand auto collection, created on the first set). Using
    # scalar_one_or_none() here would crash with MultipleResultsFound once a
    # second same-schema collection exists, failing every inference item.
    collection = session.execute(
        select(AnnotationSetCollection)
        .where(
            AnnotationSetCollection.organization_id == org_id,
            AnnotationSetCollection.schema_id == schema_id,
            AnnotationSetCollection.deleted_at.is_(None),
        )
        .order_by(
            AnnotationSetCollection.created_at.asc(),
            AnnotationSetCollection.id.asc(),
        )
    ).scalars().first()

    if collection is None:
        schema_name = session.execute(
            select(AnnotationSchema.name).where(AnnotationSchema.id == schema_id)
        ).scalar_one_or_none()
        collection = AnnotationSetCollection(
            organization_id=org_id,
            schema_id=schema_id,
            name=_collection_name(schema_name, schema_id),
        )
        session.add(collection)
        try:
            session.flush()
        except IntegrityError:
            # Name collides with an existing collection in this org — retry with
            # a schema-scoped suffix so the auto-collection is still created.
            session.rollback()
            collection = AnnotationSetCollection(
                organization_id=org_id,
                schema_id=schema_id,
                name=f"{_collection_name(schema_name, schema_id)} ({str(schema_id)[:8]})",
            )
            session.add(collection)
            session.flush()

    existing = session.get(
        AnnotationSetCollectionItem, (collection.id, annotation_set.id)
    )
    if existing is None:
        session.add(
            AnnotationSetCollectionItem(
                collection_id=collection.id,
                annotation_set_id=annotation_set.id,
            )
        )
        session.flush()


async def ensure_schema_collection_async(db, annotation_set: AnnotationSet) -> None:
    """Async counterpart of :func:`ensure_schema_collection_sync`.

    Does not commit; the caller's transaction owns the lifecycle.
    """
    schema_id = annotation_set.schema_id
    if schema_id is None:
        return
    org_id = annotation_set.organization_id

    # See the sync variant: (org, schema) is not unique, so pick the oldest
    # collection deterministically rather than asserting a single row.
    collection = (
        await db.execute(
            select(AnnotationSetCollection)
            .where(
                AnnotationSetCollection.organization_id == org_id,
                AnnotationSetCollection.schema_id == schema_id,
                AnnotationSetCollection.deleted_at.is_(None),
            )
            .order_by(
                AnnotationSetCollection.created_at.asc(),
                AnnotationSetCollection.id.asc(),
            )
        )
    ).scalars().first()

    if collection is None:
        schema_name = (
            await db.execute(
                select(AnnotationSchema.name).where(AnnotationSchema.id == schema_id)
            )
        ).scalar_one_or_none()
        collection = AnnotationSetCollection(
            organization_id=org_id,
            schema_id=schema_id,
            name=_collection_name(schema_name, schema_id),
        )
        db.add(collection)
        await db.flush()

    existing = await db.get(
        AnnotationSetCollectionItem, (collection.id, annotation_set.id)
    )
    if existing is None:
        db.add(
            AnnotationSetCollectionItem(
                collection_id=collection.id,
                annotation_set_id=annotation_set.id,
            )
        )
        await db.flush()
