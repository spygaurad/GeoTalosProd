import logging

import httpx
from fastapi import HTTPException
from fastapi.responses import Response

logger = logging.getLogger(__name__)


async def forward_tile(
    client: httpx.AsyncClient,
    url: str,
    *,
    default_content_type: str,
) -> Response:
    """Fetch tile bytes after the caller has checked access."""
    try:
        resp = await client.get(url)
    except httpx.RequestError as exc:
        logger.error("Tile service connection failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Tile service unavailable",
        ) from exc

    if resp.status_code == 204:
        return Response(status_code=204)

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Tile not found")

    if resp.status_code >= 400:
        logger.warning(
            "Tile service error: status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        raise HTTPException(
            status_code=502,
            detail="Tile service error",
        )

    content_type = (
        resp.headers.get("content-type", default_content_type)
        .split(";")[0]
        .strip()
    )

    headers = {
        name: resp.headers[name]
        for name in ("cache-control", "etag", "last-modified")
        if name in resp.headers
    }

    # HTTPX may decompress the body; Response calculates its actual length.
    return Response(
        content=resp.content,
        status_code=200,
        media_type=content_type,
        headers=headers,
    )