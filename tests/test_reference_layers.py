"""Public reference-layer tile route: allowlist, bounds, auth and headers.

Martin is mocked; only allowlisted source ids may reach it.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.middleware import clerk_auth
from app.services.reference_layer_meta import LAYER_META


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
                "geometry": "polygon",
                "attributes": LAYER_META["ramsar"]["attributes"],
                "density_tile_url": None,
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


def test_large_tile_is_gzipped_when_accepted(client, monkeypatch):
    """httpx unzips Martin's gzip, so the API must zip vector tiles again on the way out."""
    body = b"mvt" * 2000
    tile = httpx.Response(200, content=body, headers={"content-type": "application/x-protobuf"})
    monkeypatch.setattr(client.app.state.martin_client, "get", AsyncMock(return_value=tile))

    zipped = client.get("/api/v1/reference-layers/ramsar/tiles/8/139/64.pbf", headers={"Accept-Encoding": "gzip"})
    assert zipped.status_code == 200
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.headers["vary"] == "Accept-Encoding"
    assert zipped.content == body  # httpx unzips it back
    assert int(zipped.headers["content-length"]) < len(body)

    raw = client.get("/api/v1/reference-layers/ramsar/tiles/8/139/64.pbf", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers
    assert raw.content == body


# --- style overrides and heatmap tiles ---------------------------------------


def test_density_tileset_is_served_but_not_listed(client, monkeypatch):
    seen = []

    async def martin_get(url):
        seen.append(url)
        if url.count("/") == 1:
            return httpx.Response(200, json={"name": url[1:], "minzoom": 0, "maxzoom": 12},
                                  request=httpx.Request("GET", "http://martin" + url))
        return httpx.Response(200, content=b"pts", headers={"content-type": "application/x-protobuf"})

    monkeypatch.setattr(client.app.state.martin_client, "get", AsyncMock(side_effect=martin_get))

    tile = client.get("/api/v1/reference-layers/raisg_mining_density/tiles/8/139/64.pbf")
    assert tile.status_code == 200 and tile.content == b"pts"

    listing = client.get("/api/v1/reference-layers").json()["items"]
    assert "raisg_mining_density" not in [i["name"] for i in listing]
    mining = next(i for i in listing if i["name"] == "raisg_mining")
    assert mining["density_tile_url"] == "/api/v1/reference-layers/raisg_mining_density/tiles/{z}/{x}/{y}.pbf"
    ramsar = next(i for i in listing if i["name"] == "ramsar")
    assert ramsar["density_tile_url"] is None


def test_put_style_stores_only_set_fields(client):
    body = {"colour": "#c0392b", "mode": "heatmap", "heatmap": {"radius": 24, "weight": "area_sig_ha"}}

    response = client.put("/api/v1/reference-layers/raisg_mining/style", json=body)

    assert response.status_code == 200, response.text
    assert response.json() == {"layer": "raisg_mining", "style": body}


@pytest.mark.parametrize(
    "name,body",
    [
        ("raisg_mining", {"colour": "red"}),
        ("raisg_mining", {"opacity": 2}),
        ("raisg_mining", {"mode": "hexbin"}),
        ("raisg_mining", {"heatmap": {"radius": 500}}),
        ("raisg_mining", {"unknown": 1}),
        ("ramsar", {"mode": "heatmap"}),  # no density tiles for this layer
        ("raisg_mining", {"heatmap": {"weight": "nombre"}}),  # not a weight attribute
    ],
)
def test_put_style_rejects_bad_input(client, name, body):
    assert client.put(f"/api/v1/reference-layers/{name}/style", json=body).status_code == 422


def test_put_style_unknown_layer_is_404(client):
    assert client.put("/api/v1/reference-layers/raisg_mining_density/style", json={}).status_code == 404
    assert client.put("/api/v1/reference-layers/nope/style", json={}).status_code == 404


def test_get_styles_returns_org_overrides(client):
    from app.api.deps import get_session
    from unittest.mock import MagicMock

    async def session():
        db = MagicMock()
        result = MagicMock()
        result.all.return_value = [("raisg_mining", {"colour": "#112233"})]
        db.execute = AsyncMock(return_value=result)
        yield db

    client.app.dependency_overrides[get_session] = session

    response = client.get("/api/v1/reference-layers/styles")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": {"raisg_mining": {"colour": "#112233"}}}


def test_delete_style_resets(client):
    assert client.delete("/api/v1/reference-layers/raisg_mining/style").status_code == 204
