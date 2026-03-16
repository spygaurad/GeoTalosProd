"""STAC proxy endpoints.

Forwards requests to the internal stac-fastapi-pgstac service and scopes
responses to the current organisation's collections.  All calls are
authenticated via the standard JWT / API-key middleware.

Collection ownership is enforced by comparing each collection's ID against
the set of ``stac_collection_id`` values stored in the ``datasets`` table for
the current org.  This avoids exposing data from other tenants even though
the underlying pgSTAC database is shared.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.config import settings
from app.models.dataset import Dataset
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stac", tags=["stac"])

# Module-level httpx client — reuses connections to stac-api across requests.
_stac_client: httpx.AsyncClient | None = None


def _get_stac_client() -> httpx.AsyncClient:
    global _stac_client
    if _stac_client is None or _stac_client.is_closed:
        _stac_client = httpx.AsyncClient(
            base_url=settings.STAC_API_URL,
            timeout=30.0,
        )
    return _stac_client


async def _org_collection_ids(db: AsyncSession, org_id: Any) -> set[str]:
    """Return the set of stac_collection_id values registered to this org."""
    result = await db.execute(
        select(Dataset.stac_collection_id).where(
            Dataset.organization_id == org_id,
            Dataset.stac_collection_id.is_not(None),
            Dataset.deleted_at.is_(None),
        )
    )
    return {row for (row,) in result.all()}


def _raise_if_stac_error(resp: httpx.Response, context: str) -> None:
    if resp.status_code >= 500:
        logger.error("stac_proxy_upstream_error context=%s status=%s", context, resp.status_code)
        raise HTTPException(status_code=502, detail="STAC service returned an error")


# ---------------------------------------------------------------------------
# GET /stac/collections
# ---------------------------------------------------------------------------

@router.get("/collections")
async def list_stac_collections(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_id: Any = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Any:
    """List STAC collections that belong to the current organisation."""
    allowed = await _org_collection_ids(db, org_id)
    if not allowed:
        return {"collections": [], "numberMatched": 0, "numberReturned": 0}

    client = _get_stac_client()
    resp = await client.get("/collections", params={"limit": 200})
    _raise_if_stac_error(resp, "list_collections")

    body = resp.json()
    collections = [c for c in body.get("collections", []) if c.get("id") in allowed]

    # Apply pagination after filtering
    total = len(collections)
    page = collections[offset : offset + limit]
    return {"collections": page, "numberMatched": total, "numberReturned": len(page)}


# ---------------------------------------------------------------------------
# GET /stac/collections/{collection_id}
# ---------------------------------------------------------------------------

@router.get("/collections/{collection_id}")
async def get_stac_collection(
    collection_id: str,
    org_id: Any = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Any:
    """Get a single STAC collection, scoped to the current organisation."""
    allowed = await _org_collection_ids(db, org_id)
    if collection_id not in allowed:
        raise HTTPException(status_code=404, detail="Collection not found")

    client = _get_stac_client()
    resp = await client.get(f"/collections/{collection_id}")
    _raise_if_stac_error(resp, "get_collection")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Collection not found")

    return resp.json()


# ---------------------------------------------------------------------------
# GET /stac/collections/{collection_id}/items
# ---------------------------------------------------------------------------

@router.get("/collections/{collection_id}/items")
async def list_stac_collection_items(
    collection_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    bbox: str | None = Query(default=None, description="minx,miny,maxx,maxy"),
    datetime: str | None = Query(default=None, description="ISO-8601 datetime or interval"),
    org_id: Any = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Any:
    """List STAC items in a collection, scoped to the current organisation."""
    allowed = await _org_collection_ids(db, org_id)
    if collection_id not in allowed:
        raise HTTPException(status_code=404, detail="Collection not found")

    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if bbox:
        params["bbox"] = bbox
    if datetime:
        params["datetime"] = datetime

    client = _get_stac_client()
    resp = await client.get(f"/collections/{collection_id}/items", params=params)
    _raise_if_stac_error(resp, "list_items")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Collection not found")

    return resp.json()


# ---------------------------------------------------------------------------
# POST /stac/search
# ---------------------------------------------------------------------------

@router.post("/search")
async def search_stac(
    request: Request,
    org_id: Any = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Any:
    """Forward a STAC search request, constraining results to org collections.

    The request body is a standard OGC/STAC search payload.  The ``collections``
    field is replaced (or injected) with the set of collection IDs owned by the
    current organisation so that cross-tenant data is never returned.
    """
    allowed = await _org_collection_ids(db, org_id)
    if not allowed:
        return {"type": "FeatureCollection", "features": [], "numberMatched": 0}

    try:
        body: dict = await request.json()
    except Exception:
        body = {}

    # Force the collections filter to the org's own collections.
    # If the caller already specified a subset, intersect with allowed.
    requested = set(body.get("collections") or [])
    effective = (requested & allowed) if requested else allowed
    if not effective:
        return {"type": "FeatureCollection", "features": [], "numberMatched": 0}

    body["collections"] = list(effective)

    client = _get_stac_client()
    resp = await client.post("/search", json=body)
    _raise_if_stac_error(resp, "search")

    return resp.json()


# ---------------------------------------------------------------------------
# GET /stac/search  (convenience — mirrors POST with query params)
# ---------------------------------------------------------------------------

@router.get("/search")
async def search_stac_get(
    collections: str | None = Query(default=None, description="Comma-separated collection IDs"),
    bbox: str | None = Query(default=None),
    datetime: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    org_id: Any = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> Any:
    """GET variant of STAC search for simple integrations."""
    allowed = await _org_collection_ids(db, org_id)
    if not allowed:
        return {"type": "FeatureCollection", "features": [], "numberMatched": 0}

    requested = set(collections.split(",")) if collections else set()
    effective = (requested & allowed) if requested else allowed
    if not effective:
        return {"type": "FeatureCollection", "features": [], "numberMatched": 0}

    params: dict[str, Any] = {"collections": ",".join(effective), "limit": limit}
    if bbox:
        params["bbox"] = bbox
    if datetime:
        params["datetime"] = datetime

    client = _get_stac_client()
    resp = await client.get("/search", params=params)
    _raise_if_stac_error(resp, "search_get")

    return resp.json()
