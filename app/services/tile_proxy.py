import logging

import httpx
from fastapi import HTTPException
from fastapi.responses import Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

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


async def cached_forward_tile(
    cache: Redis | None,
    key: str,
    client: httpx.AsyncClient,
    url: str,
    *,
    content_type: str,
) -> Response:
    """forward_tile() behind a Redis cache keyed by the caller.

    The caller must check access first and put everything that changes the
    bytes (org, set, version, z/x/y) in ``key``. Only 200 and 204 responses are
    stored; an empty value means 204. Redis errors fall back to the upstream.
    """
    if cache is not None:
        try:
            cached = await cache.get(key)
        except (RedisError, OSError) as exc:
            logger.warning("Tile cache read failed: %s", exc)
        else:
            if cached is not None:
                if not cached:
                    return Response(status_code=204)
                return Response(content=cached, media_type=content_type)

    response = await forward_tile(client, url, default_content_type=content_type)

    if cache is not None:
        try:
            await cache.set(key, response.body if response.status_code == 200 else b"")
        except (RedisError, OSError) as exc:
            logger.warning("Tile cache write failed: %s", exc)
    return response
