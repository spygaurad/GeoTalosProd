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

    logger.debug(
        "upsert_stac_item id=%s collection=%s",
        item.get("id"),
        item.get("collection"),
    )
    with PgstacDB(dsn, debug=False) as db:
        loader = Loader(db=db)
        loader.load_items([item], insert_mode=Methods.upsert)
