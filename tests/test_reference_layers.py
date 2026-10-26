"""Public reference-layer tile route: allowlist, bounds, auth and headers.

Martin is mocked; only allowlisted source ids may reach it.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.middleware import clerk_auth


@pytest.fixture()
def martin(client, monkeypatch):
    """Mock Martin: a tilejson for ``/ramsar``, 404 for the other layers' tilejson (not built
    yet, which the list must skip), and tile bytes for any tile URL."""

    async def martin_get(url):
        if url == "/ramsar":
            return httpx.Response(
                200,
                json={"name": "Ramsar Sites", "attribution": "RSIS (CC BY 4.0)", "minzoom": 0, "maxzoom": 12},
                request=httpx.Request("GET", "http://martin" + url),
            )
        if url.count("/") == 1:  # tilejson of an allowlisted layer Martin does not serve yet
            return httpx.Response(404, request=httpx.Request("GET", "http://martin" + url))
        return httpx.Response(200, content=b"mvt-bytes", headers={"content-type": "application/x-protobuf"})

    mock = AsyncMock(side_effect=martin_get)
    monkeypatch.setattr(client.app.state.martin_client, "get", mock)
    return mock


def test_allowed_layer_tile(client, martin):
    response = client.get("/api/v1/reference-layers/ramsar/tiles/8/139/64.pbf")

    assert response.status_code == 200, response.text
    assert response.content == b"mvt-bytes"
    assert response.headers["content-type"] == "application/x-protobuf"
    assert response.headers["cache-control"] == "public, max-age=86400"
    martin.assert_awaited_once_with("/ramsar/8/139/64")


@pytest.mark.parametrize("name", ["unknown", "annotation_set_mvt", "datasets", "feature_layers"])
def test_non_allowlisted_names_never_reach_martin(client, martin, name):
    response = client.get(f"/api/v1/reference-layers/{name}/tiles/8/139/64.pbf")

    assert response.status_code == 404
    martin.assert_not_called()


@pytest.mark.parametrize("z,x,y", [(8, 256, 0), (8, 0, 256), (23, 0, 0), (-1, 0, 0)])
def test_out_of_bounds_tiles_never_reach_martin(client, martin, z, x, y):
    response = client.get(f"/api/v1/reference-layers/ramsar/tiles/{z}/{x}/{y}.pbf")

    assert response.status_code == 422
    martin.assert_not_called()


def test_empty_tile_is_204(client, monkeypatch):
    monkeypatch.setattr(client.app.state.martin_client, "get", AsyncMock(return_value=httpx.Response(204)))

    response = client.get("/api/v1/reference-layers/ramsar/tiles/8/0/0.pbf")

    assert response.status_code == 204
    assert response.content == b""


def test_tile_without_token_is_rejected(client, martin, monkeypatch):
    monkeypatch.setattr(clerk_auth.settings, "ENVIRONMENT", "production")

    response = client.get("/api/v1/reference-layers/ramsar/tiles/8/139/64.pbf")

    assert response.status_code == 401
    martin.assert_not_called()


def test_list_reference_layers(client, martin):
    response = client.get("/api/v1/reference-layers")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "name": "ramsar",
                "title": "Ramsar Sites",
                "description": None,
                "attribution": "RSIS (CC BY 4.0)",
                "minzoom": 0,
                "maxzoom": 12,
                "bounds": None,
                "tile_url": "/api/v1/reference-layers/ramsar/tiles/{z}/{x}/{y}.pbf",
            }
        ]
    }


def test_list_skips_unavailable_layer(client, monkeypatch):
    async def missing(url):
        return httpx.Response(404, request=httpx.Request("GET", "http://martin" + url))

    monkeypatch.setattr(client.app.state.martin_client, "get", AsyncMock(side_effect=missing))

    response = client.get("/api/v1/reference-layers")

    assert response.status_code == 200
    assert response.json() == {"items": []}
