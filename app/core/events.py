"""
Generic event bus over Redis pub/sub.

Producers call publish / publish_sync to emit typed events.
Consumers call subscribe to receive SSE-formatted strings.

Events are buffered in a Redis sorted set (last 50 per org) so
late-connecting clients can replay via Last-Event-ID.
"""
import json
import time
import uuid
from datetime import datetime, UTC
from typing import Any, AsyncGenerator

import redis as sync_redis
import redis.asyncio as aioredis

from app.config import settings

# ── Constants ────────────────────────────────────────────────────────────────

CHANNEL_PREFIX = "events"          # Redis channel: events:{org_id}
BUFFER_KEY_PREFIX = "event_buf"    # Sorted set: event_buf:{org_id}
MAX_BUFFER_SIZE = 50               # Keep last N events per org
BUFFER_TTL_SECONDS = 3600          # Expire buffer after 1 hour of inactivity


# ── Helpers ──────────────────────────────────────────────────────────────────

def _channel(org_id: str) -> str:
    return f"{CHANNEL_PREFIX}:{org_id}"


def _buffer_key(org_id: str) -> str:
    return f"{BUFFER_KEY_PREFIX}:{org_id}"


def _make_event(org_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "org_id": org_id,
        "data": data,
        "ts": datetime.now(UTC).isoformat(),
    }


def _event_to_sse(event: dict[str, Any]) -> str:
    """Format an event dict as an SSE text block."""
    return (
        f"id: {event['id']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event)}\n\n"
    )


# ── Async interface (FastAPI / async callers) ────────────────────────────────

_async_redis: aioredis.Redis | None = None


async def _get_async_redis() -> aioredis.Redis:
    global _async_redis
    if _async_redis is None:
        _async_redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _async_redis


async def publish(org_id: str, event_type: str, data: dict[str, Any]) -> str:
    """Publish an event (async). Returns the event ID."""
    r = await _get_async_redis()
    event = _make_event(org_id, event_type, data)
    payload = json.dumps(event)

    pipe = r.pipeline()
    pipe.publish(_channel(org_id), payload)
    # Buffer for reconnect replay
    pipe.zadd(_buffer_key(org_id), {payload: time.time()})
    pipe.zremrangebyrank(_buffer_key(org_id), 0, -(MAX_BUFFER_SIZE + 1))
    pipe.expire(_buffer_key(org_id), BUFFER_TTL_SECONDS)
    await pipe.execute()

    return event["id"]


async def subscribe(
    org_id: str,
    topics: list[str] | None = None,
    last_event_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted strings for an org.

    If last_event_id is provided, replays buffered events first.
    topics filters by event type prefix (e.g. "automation" matches "automation.step.started").
    """
    r = await _get_async_redis()

    # Replay buffered events if reconnecting
    if last_event_id:
        buffered = await r.zrange(_buffer_key(org_id), 0, -1)
        found = False
        for raw in buffered:
            event = json.loads(raw)
            if found:
                if _matches_topics(event["type"], topics):
                    yield _event_to_sse(event)
            elif event["id"] == last_event_id:
                found = True
        # If ID not found in buffer, replay everything (client was away too long)
        if not found and buffered:
            for raw in buffered:
                event = json.loads(raw)
                if _matches_topics(event["type"], topics):
                    yield _event_to_sse(event)

    # Subscribe to live events
    pubsub = r.pubsub()
    await pubsub.subscribe(_channel(org_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            event = json.loads(message["data"])
            if _matches_topics(event["type"], topics):
                yield _event_to_sse(event)
    finally:
        await pubsub.unsubscribe(_channel(org_id))
        await pubsub.aclose()


# ── Sync interface (Celery workers) ──────────────────────────────────────────

_sync_redis: sync_redis.Redis | None = None


def _get_sync_redis() -> sync_redis.Redis:
    global _sync_redis
    if _sync_redis is None:
        _sync_redis = sync_redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _sync_redis


def publish_sync(org_id: str, event_type: str, data: dict[str, Any]) -> str:
    """Publish an event (sync, for Celery workers). Returns the event ID."""
    r = _get_sync_redis()
    event = _make_event(org_id, event_type, data)
    payload = json.dumps(event)

    pipe = r.pipeline()
    pipe.publish(_channel(org_id), payload)
    pipe.zadd(_buffer_key(org_id), {payload: time.time()})
    pipe.zremrangebyrank(_buffer_key(org_id), 0, -(MAX_BUFFER_SIZE + 1))
    pipe.expire(_buffer_key(org_id), BUFFER_TTL_SECONDS)
    pipe.execute()

    return event["id"]


# ── Topic filter ─────────────────────────────────────────────────────────────

def _matches_topics(event_type: str, topics: list[str] | None) -> bool:
    """Check if event_type matches any of the topic prefixes."""
    if not topics:
        return True
    return any(event_type.startswith(t) for t in topics)
