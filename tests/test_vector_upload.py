"""Uploaded vector layers: file parsing, the upload route and the org-scoped tile proxy."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.api.deps import get_session
from app.services.vector_upload import UploadError, parse_upload

SQUARE = {"type": "Polygon", "coordinates": [[[-70, -12], [-69.9, -12], [-69.9, -11.9], [-70, -11.9], [-70, -12]]]}


def fc(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)}).encode()


def feat(geom, **props):
    return {"type": "Feature", "geometry": geom, "properties": props}


def test_geojson_keeps_flat_properties_and_skips_bad_features():
    data = fc(
        feat(SQUARE, name="A", ha=1.5, tags=["x"]),
        feat({"type": "Point", "coordinates": [500, 95]}, name="bad"),
        {"type": "Feature", "geometry": None, "properties": {}},
    )

    features, skipped, reasons = parse_upload("a.geojson", data)

    assert len(features) == 1 and skipped == 2 and len(reasons) == 2
    assert features[0]["properties"] == {"name": "A", "ha": 1.5, "tags": '["x"]'}  # lists become JSON text


def test_single_feature_and_bare_geometry_are_accepted():
    assert len(parse_upload("a.json", json.dumps(feat(SQUARE)).encode())[0]) == 1
    assert len(parse_upload("a.json", json.dumps(SQUARE).encode())[0]) == 1


def test_csv_with_lon_lat_columns():
    features, skipped, _ = parse_upload("p.csv", b"name,lon,lat\nA,-70,-12\nB,-69,-11\n")

    assert len(features) == 2 and skipped == 0
    assert features[0]["geometry"]["coordinates"] == [-70.0, -12.0]


def test_other_crs_is_reprojected_to_wgs84():
    # UTM 18S easting/northing near Puerto Maldonado
    data = fc(feat({"type": "Point", "coordinates": [480000, 8570000]}))

    features, _, _ = parse_upload("a.geojson", data, crs="EPSG:32719")

    lon, lat = features[0]["geometry"]["coordinates"]
    assert -73 < lon < -68 and -14 < lat < -10


@pytest.mark.parametrize(
    "name,data",
    [
        ("a.zip", b"PK"),
        ("a.geojson", b"not json"),
        ("a.geojson", b'{"type": "Nope"}'),
        ("a.geojson", fc()),  # no features
        ("a.csv", b"a,b\n1,2\n"),  # no geometry columns
    ],
)
def test_unusable_files_raise_upload_error(name, data):
    with pytest.raises(UploadError):
        parse_upload(name, data)


# --- routes ----------------------------------------------------------------


@pytest.fixture()
def db(client):
    mock = MagicMock()
    mock.scalar = AsyncMock(return_value=None)  # no layer with that name yet
    mock.execute = AsyncMock()
    mock.flush = AsyncMock()
    mock.commit = AsyncMock()

    async def session():
        yield mock

    client.app.dependency_overrides[get_session] = session
    return mock


def test_upload_creates_rows(client, db):
    r = client.post("/api/v1/feature-layers/upload?filename=roads.geojson", content=fc(feat(SQUARE, name="A")))

    assert r.status_code == 201, r.text
    assert r.json()["layer"] == "roads" and r.json()["created"] == 1
    assert db.add_all.call_count == 1


def test_upload_existing_name_is_409(client, db):
    db.scalar = AsyncMock(return_value=1)

    r = client.post("/api/v1/feature-layers/upload?filename=roads.geojson", content=fc(feat(SQUARE)))

    assert r.status_code == 409


@pytest.mark.parametrize("query", ["filename=a.zip", "filename=a.geojson&layer_name=bad/name%00"])
def test_upload_rejects_bad_input(client, db, query):
    assert client.post(f"/api/v1/feature-layers/upload?{query}", content=b"{}").status_code == 422


def test_tile_proxy_passes_org_from_session_not_from_request(client, monkeypatch):
    get = AsyncMock(return_value=httpx.Response(200, content=b"mvt", headers={"content-type": "application/x-protobuf"}))
    monkeypatch.setattr(client.app.state.martin_client, "get", get)

    r = client.get(
        "/api/v1/feature-layers/layers/My%20roads/tiles/8/78/136.pbf?org_id=11111111-1111-1111-1111-111111111111"
    )

    assert r.status_code == 200 and r.content == b"mvt"
    assert r.headers["cache-control"] == "private, no-store"
    url = get.await_args.args[0]
    assert url.startswith("/feature_layer_mvt/8/78/136?org_id=00000000-0000-0000-0000-000000000001&layer=My%20roads")
    assert "1111" not in url


def test_tile_out_of_bounds_never_reaches_martin(client, monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(client.app.state.martin_client, "get", get)

    assert client.get("/api/v1/feature-layers/layers/x/tiles/8/256/0.pbf").status_code == 422
    get.assert_not_called()


def test_upload_over_the_cap_is_413_without_reading_it_all(client, db, monkeypatch):
    monkeypatch.setattr("app.api.v1.endpoints.feature_layers.MAX_UPLOAD_BYTES", 10)

    r = client.post("/api/v1/feature-layers/upload?filename=a.geojson", content=b"x" * 50)

    assert r.status_code == 413
    db.add_all.assert_not_called()
