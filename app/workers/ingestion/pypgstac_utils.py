"""
Thin synchronous wrappers around pypgstac for use inside Celery workers.

pypgstac uses psycopg2 internally, so these functions must be called from
a sync context (Celery task), never from an async FastAPI request handler.

DSN format expected: ``postgresql://user:password@host:port/dbname``
(plain libpq URI — no ``+asyncpg`` prefix).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def upsert_stac_collection(collection: dict, dsn: str) -> None:
    """Insert or update a STAC Collection in pgSTAC."""
    from pypgstac.db import PgstacDB
    from pypgstac.load import Loader, Methods

    logger.debug("upsert_stac_collection id=%s", collection.get("id"))
    with PgstacDB(dsn, debug=False) as db:
        loader = Loader(db=db)
        loader.load_collections([collection], insert_mode=Methods.upsert)


def upsert_stac_item(item: dict, dsn: str) -> None:
    """Insert or update a STAC Item in pgSTAC."""
    from pypgstac.db import PgstacDB
    from pypgstac.load import Loader, Methods

    item_id = item.get("id")
    collection_id = item.get("collection")
    
    logger.info("upsert_stac_item STARTING id=%s collection=%s", item_id, collection_id)
    
    try:
        with PgstacDB(dsn, debug=False) as db:
            loader = Loader(db=db)
            loader.load_items([item], insert_mode=Methods.upsert)
            logger.info("upsert_stac_item SUCCESS id=%s", item_id)
    except Exception as exc:
        logger.error("upsert_stac_item FAILED id=%s collection=%s error=%s", 
                     item_id, collection_id, exc, exc_info=True)
        raise


def batch_upsert_stac_items(items: list[dict], dsn: str) -> None:
    """Insert or update multiple STAC Items in pgSTAC in a single batch.
    
    This prevents partition constraint issues that can occur when inserting
    items one-at-a-time with sequential timestamps.
    
    Uses pypgstac.load functionality as recommended for batch operations.
    """
    from pypgstac.db import PgstacDB
    from pypgstac.load import Loader, Methods

    if not items:
        logger.info("batch_upsert_stac_items: No items to insert")
        return
    
    collection_id = items[0].get("collection") if items else "unknown"
    item_count = len(items)
    
    logger.info("batch_upsert_stac_items STARTING count=%d collection=%s", 
                item_count, collection_id)
    
    # Log datetime range for debugging timezone issues
    datetimes = []
    for item in items:
        dt_str = item.get("properties", {}).get("datetime")
        if dt_str:
            datetimes.append(dt_str)
    
    if datetimes:
        logger.info("batch_upsert_stac_items datetime range: %s to %s", 
                   min(datetimes), max(datetimes))
    
    try:
        with PgstacDB(dsn, debug=False) as db:
            loader = Loader(db=db)
            # Use pypgstac.load functionality for proper batch handling
            loader.load_items(items, insert_mode=Methods.upsert)
            logger.info("batch_upsert_stac_items SUCCESS count=%d collection=%s", 
                       item_count, collection_id)
    except Exception as exc:
        logger.error("batch_upsert_stac_items FAILED count=%d collection=%s error=%s", 
                     item_count, collection_id, exc, exc_info=True)
        raise
