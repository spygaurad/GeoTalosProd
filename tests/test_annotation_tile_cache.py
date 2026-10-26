"""Versioned Redis tile cache on the annotation-set tile route.

Uses an in-memory stand-in for Redis and a mocked Martin client.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.deps import get_current_org_id
from app.api.v1.endpoints import annotation_sets


class FakeCache:
    def __init__(self, fail=False):
        self.data, self.fail = {}, fail

    async def get(self, key):
        if self.fail:
            raise RedisConnectionError("simulated outage")
        return self.data.get(key)

    async def set(self, key, value):
        if self.fail:
            raise RedisConnectionError("simulated outage")
        self.data[key] = value


@pytest.fixture()
def tile(client, monkeypatch):
    """Return (request_tile, state) with a fake cache, Martin mock and set lookup."""
    state = SimpleNamespace(set_id=uuid4(), version=1, cache=FakeCache(), status=200, body=b"mvt-bytes")
    client.app.state.tile_cache = state.cache

    async def get_set(self, set_id, organization_id):
        return SimpleNamespace(id=state.set_id, tile_version=state.version)

    monkeypatch.setattr(annotation_sets.AnnotationSetService, "get_set", get_set)

    async def martin_get(url):
        return httpx.Response(state.status, content=state.body if state.status == 200 else b"",
                              headers={"content-type": "application/x-protobuf"})

    state.martin = AsyncMock(side_effect=martin_get)
    monkeypatch.setattr(client.app.state.martin_client, "get", state.martin)

    def request_tile(z=14, x=12306, y=7077):
        return client.get(f"/api/v1/annotation-sets/{state.set_id}/tiles/{z}/{x}/{y}.pbf")

    return request_tile, state


def test_miss_then_hit_calls_martin_once(tile):
    request_tile, state = tile
    first, second = request_tile(), request_tile()
    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"mvt-bytes"
    assert second.headers["content-type"] == "application/x-protobuf"
    assert second.headers["cache-control"] == "private, no-store"
    state.martin.assert_awaited_once_with(f"/annotation_set_mvt/14/12306/7077?set_id={state.set_id}&v=1")
    assert list(state.cache.data) == [
        f"tile:00000000-0000-0000-0000-000000000001:{state.set_id}:1:14:12306:7077"
    ]


def test_version_change_misses_and_serves_new_bytes(tile):
    request_tile, state = tile
    request_tile()
    state.version, state.body = 2, b"edited-bytes"
    response = request_tile()
    assert response.content == b"edited-bytes"
    assert state.martin.await_count == 2
    assert state.martin.await_args.args[0].endswith("&v=2")
    assert len(state.cache.data) == 2  # old version left for LRU, never purged


def test_other_organization_uses_its_own_key(tile, client, monkeypatch):
    request_tile, state = tile
    request_tile()
    other_org = uuid4()

    async def other():
        return other_org

    monkeypatch.setitem(client.app.dependency_overrides, get_current_org_id, other)
    request_tile()
    assert state.martin.await_count == 2
    assert any(k.startswith(f"tile:{other_org}:") for k in state.cache.data)


def test_empty_tile_is_cached_as_204(tile):
    request_tile, state = tile
    state.status = 204
    first, second = request_tile(), request_tile()
    assert first.status_code == second.status_code == 204
    assert second.content == b""
    assert second.headers["cache-control"] == "private, no-store"
    state.martin.assert_awaited_once()


def test_upstream_error_is_not_cached(tile):
    request_tile, state = tile
    state.status = 500
    assert request_tile().status_code == 502
    assert state.cache.data == {}
    state.status = 200
    assert request_tile().status_code == 200
    assert state.martin.await_count == 2


def test_redis_outage_falls_back_to_martin(tile, client):
    request_tile, state = tile
    client.app.state.tile_cache = FakeCache(fail=True)
    first, second = request_tile(), request_tile()
    assert first.status_code == second.status_code == 200
    assert second.content == b"mvt-bytes"
    assert state.martin.await_count == 2


def test_cache_disabled_always_calls_martin(tile, client):
    request_tile, state = tile
    client.app.state.tile_cache = None
    request_tile()
    request_tile()
    assert state.martin.await_count == 2
