"""Tests for raster and vector reprojection."""

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from app.services.conversion import parse_crs, reproject_features, reproject_raster_to_cog


def _write_geotiff(path: Path, data: np.ndarray, *, crs="EPSG:4326", nodata=None) -> str:
    count = data.shape[0]
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype=str(data.dtype),
        count=count,
        height=data.shape[1],
        width=data.shape[2],
        crs=crs,
        transform=from_origin(-10.0, 10.0, 0.2, 0.2),
        nodata=nodata,
    ) as dst:
        dst.write(data)
    return str(path)


def _image(bands=3, size=100) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.integers(1, 255, size=(bands, size, size), dtype=np.uint8)


# ── parse_crs ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [("EPSG:3857", "EPSG:3857"), ("3857", "EPSG:3857"), (" epsg:4326 ", "EPSG:4326")],
)
def test_parse_crs_normalizes(value, expected):
    assert parse_crs(value) == expected


@pytest.mark.parametrize("value", ["", "EPSG:999999", "not-a-crs"])
def test_parse_crs_rejects_unknown(value):
    with pytest.raises(ValueError):
        parse_crs(value)


# ── raster ───────────────────────────────────────────────────────────────────


def test_raster_is_reprojected_to_a_cog(tmp_path: Path):
    src = _write_geotiff(tmp_path / "src.tif", _image())
    out = tmp_path / "out.tif"

    reproject_raster_to_cog(src, str(out), "EPSG:3857", resampling="bilinear")

    with rasterio.open(src) as s, rasterio.open(out) as d:
        assert d.crs.to_string() == "EPSG:3857"
        assert d.count == s.count
        assert d.dtypes == s.dtypes
        assert d.profile.get("tiled") is True
        assert d.overviews(1) or (d.width <= 512 and d.height <= 512)
        # The warped grid may extend by up to one output pixel at each edge.
        pixel = max(d.res)
        expected = transform_bounds(s.crs, d.crs, *s.bounds)
        for got, want in zip(d.bounds, expected):
            assert got == pytest.approx(want, abs=pixel)


def test_nearest_keeps_mask_class_values_and_nodata(tmp_path: Path):
    mask = np.zeros((1, 100, 100), dtype=np.uint8)
    mask[0, 10:40, 10:40] = 1
    mask[0, 50:90, 50:90] = 2
    src = _write_geotiff(tmp_path / "mask.tif", mask, nodata=0)
    out = tmp_path / "mask_3857.tif"

    reproject_raster_to_cog(src, str(out), "3857", resampling="nearest")

    with rasterio.open(out) as d:
        assert d.nodata == 0
        values = set(np.unique(d.read(1)).tolist())
    assert values <= {0, 1, 2}
    assert {1, 2} <= values


def test_unknown_resampling_is_rejected(tmp_path: Path):
    src = _write_geotiff(tmp_path / "src.tif", _image())

    with pytest.raises(ValueError, match="unknown resampling"):
        reproject_raster_to_cog(src, str(tmp_path / "o.tif"), "EPSG:3857", resampling="lanczos")


def test_unknown_crs_is_rejected(tmp_path: Path):
    src = _write_geotiff(tmp_path / "src.tif", _image())

    with pytest.raises(ValueError, match="Unsupported CRS"):
        reproject_raster_to_cog(src, str(tmp_path / "o.tif"), "EPSG:999999")


def test_source_without_crs_is_rejected(tmp_path: Path):
    src = _write_geotiff(tmp_path / "nocrs.tif", _image(), crs=None)

    with pytest.raises(ValueError, match="Reprojection failed: source raster has no coordinate"):
        reproject_raster_to_cog(src, str(tmp_path / "o.tif"), "EPSG:3857")


def test_unreadable_source_is_rejected(tmp_path: Path):
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"not a tiff")

    with pytest.raises(ValueError, match="unreadable raster"):
        reproject_raster_to_cog(str(bad), str(tmp_path / "o.tif"), "EPSG:3857")


def test_same_input_and_output_path_rejected(tmp_path: Path):
    src = _write_geotiff(tmp_path / "src.tif", _image())

    with pytest.raises(ValueError, match="must be different"):
        reproject_raster_to_cog(src, src, "EPSG:3857")


# ── vector ───────────────────────────────────────────────────────────────────


def _point(lon, lat, **props):
    return {
        "type": "Feature",
        "id": "p1",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def test_features_are_transformed_to_web_mercator():
    (feature,) = reproject_features([_point(10.0, 0.0, label="a")], "EPSG:3857")

    x, y = feature["geometry"]["coordinates"]
    assert x == pytest.approx(1113194.9, abs=1)
    assert y == pytest.approx(0.0, abs=1e-6)
    assert feature["properties"] == {"label": "a"}
    assert feature["id"] == "p1"


def test_polygon_is_transformed_to_utm():
    polygon = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-75.0, 40.0], [-74.99, 40.0], [-74.99, 40.01], [-75.0, 40.0]]],
        },
        "properties": {},
    }

    (feature,) = reproject_features([polygon], "EPSG:32618")

    x, y = feature["geometry"]["coordinates"][0][0]
    assert x == pytest.approx(500000, abs=1)
    assert y == pytest.approx(4427757, abs=5)


def test_wgs84_target_returns_unchanged_copies():
    original = _point(1.0, 2.0)

    (feature,) = reproject_features([original], "4326")

    assert feature == original
    assert feature is not original


def test_feature_without_geometry_is_kept():
    feature = {"type": "Feature", "geometry": None, "properties": {}}

    assert reproject_features([feature], "EPSG:3857") == [feature]


def test_invalid_target_crs_raises():
    with pytest.raises(ValueError):
        reproject_features([_point(1.0, 2.0)], "EPSG:999999")
