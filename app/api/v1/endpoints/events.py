"""SSE event stream endpoint.

Clients connect to ``GET /events`` to receive real-time server-sent events
scoped to their organization. Supports ``Last-Event-ID`` for reconnect replay
and optional ``topics`` filtering.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, get_session, require_org_role
from app.core.events import subscribe
from app.models.user import User

router = APIRouter(prefix="/events", tags=["events"])


@router.get("")
async def stream_events(
    request: Request,
    topics: str | None = Query(
        default=None,
        description="Comma-separated topic prefixes to filter (e.g. 'automation,job').",
    ),
    org_id: UUID = Depends(require_org_role("org:viewer")),
    _current_user: User = Depends(get_current_user),
    _db: Any = Depends(get_session),
) -> StreamingResponse:
    """Server-Sent Events stream for the caller's organization.

    Supports ``Last-Event-ID`` header for reconnect replay.
    """
    last_event_id = request.headers.get("Last-Event-ID")
    topic_list = [t.strip() for t in topics.split(",")] if topics else None

    return StreamingResponse(
        subscribe(str(org_id), topics=topic_list, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
