"""Presigned download URL endpoint.

Returns a short-lived presigned GET URL so the browser can download a file
directly from MinIO/S3 without proxying the bytes through the API.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_org_role
from app.models.dataset_item import DatasetItem
from app.models.user import User
from app.services import storage_service

router = APIRouter(prefix="/storage", tags=["storage"])


@router.get("/download-url")
async def get_download_url(
    dataset_item_id: UUID = Query(..., description="ID of the dataset item to download."),
    ttl: int = Query(default=3600, ge=60, le=86400, description="URL validity in seconds."),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    db: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a presigned download URL for a dataset item's source file."""
    result = await db.execute(
        select(DatasetItem).where(
            DatasetItem.id == dataset_item_id,
            DatasetItem.organization_id == org_id,
            DatasetItem.is_active.is_(True),
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Dataset item not found")

    s3_key = item.s3_uri
    if s3_key.startswith("s3://"):
        # Strip bucket prefix — storage_service builds bucket name from org_id
        parts = s3_key.split("/", 3)
        s3_key = parts[3] if len(parts) > 3 else s3_key

    url = await asyncio.to_thread(
        storage_service.generate_download_url, org_id, s3_key, ttl,
    )
    return {"url": url, "expires_in": ttl}
