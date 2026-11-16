"""Tests for GeoJSON <-> CSV conversion."""

import csv
import io

import pytest

from app.services.conversion.csv_geojson import (
    CSV_BASE_COLUMNS,
    csv_to_feature_collection,
    features_to_csv,
)

POINT = {
    "type": "Feature",
    "id": "a1",
    "geometry": {"type": "Point", "coordinates": [-122.4, 37.8]},
    "properties": {
        "class_id": "c1",
        "confidence": 0.9,
        "label": "tree",
        "meta": {"height": 12},
    },
}
POLYGON = {
    "type": "Feature",
    "id": "b2",
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
    "properties": {"class_id": "c2", "confidence": None},
}


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def test_features_to_csv_writes_base_columns_then_properties():
    text = features_to_csv([POINT, POLYGON], {"c1": "palm", "c2": "urban"})

    header = text.splitlines()[0].split(",")
    assert tuple(header[: len(CSV_BASE_COLUMNS)]) == CSV_BASE_COLUMNS
    assert header[len(CSV_BASE_COLUMNS):] == ["label", "meta"]

    point, polygon = _rows(text)
    assert point["id"] == "a1"
    assert point["class_name"] == "palm"
    assert point["confidence"] == "0.9"
    assert point["geometry_wkt"] == "POINT (-122.4 37.8)"
    assert point["longitude"] == "-122.4"
    assert point["latitude"] == "37.8"
    assert point["label"] == "tree"
    assert point["meta"] == '{"height":12}'
    assert polygon["class_name"] == "urban"
    assert polygon["geometry_wkt"] == "POLYGON ((0 0, 1 0, 1 1, 0 0))"
    assert polygon["longitude"] == ""
    assert polygon["confidence"] == ""


def test_class_name_falls_back_to_feature_property():
    feature = {**POINT, "properties": {"class_id": "c9", "class": "water"}}

    (row,) = _rows(features_to_csv([feature]))

    assert row["class_name"] == "water"
    assert "class" not in row


def test_round_trip_keeps_geometry_class_and_properties():
    text = features_to_csv([POINT, POLYGON], {"c1": "palm", "c2": "urban"})

    fc = csv_to_feature_collection(text)

    assert fc["type"] == "FeatureCollection"
    point, polygon = fc["features"]
    assert point["id"] == "a1"
    assert point["geometry"] == {"type": "Point", "coordinates": (-122.4, 37.8)}
    assert point["properties"]["class_id"] == "c1"
    assert point["properties"]["confidence"] == "0.9"
    assert point["properties"]["label"] == "tree"
    assert "longitude" not in point["properties"]
    assert "geometry_wkt" not in point["properties"]
    assert polygon["geometry"]["type"] == "Polygon"
    assert "confidence" not in polygon["properties"]


def test_longitude_latitude_columns_are_detected():
    fc = csv_to_feature_collection("site,lon,lat\nA,10.5,20.25\n")

    (feature,) = fc["features"]
    assert feature["geometry"] == {"type": "Point", "coordinates": [10.5, 20.25]}
    assert feature["properties"] == {"site": "A"}


def test_explicit_columns_override_detection():
    text = "name,east,north,wkt\nA,1,2,POINT (9 9)\n"

    fc = csv_to_feature_collection(text, lon_column="east", lat_column="north")

    assert fc["features"][0]["geometry"]["coordinates"] == [1.0, 2.0]


def test_bad_rows_keep_null_geometry_for_the_import_job():
    text = "lon,lat\n10,20\n200,5\nabc,1\n,\n"

    features = csv_to_feature_collection(text)["features"]

    assert [f["geometry"] is not None for f in features] == [True, False, False, False]


def test_invalid_wkt_gives_null_geometry():
    fc = csv_to_feature_collection("geometry_wkt\nNOT WKT\n")

    assert fc["features"][0]["geometry"] is None


def test_utf8_bom_is_ignored():
    fc = csv_to_feature_collection("﻿longitude,latitude\n1,2\n")

    assert fc["features"][0]["geometry"]["coordinates"] == [1.0, 2.0]


@pytest.mark.parametrize(
    "text, kwargs, message",
    [
        ("", {}, "no header"),
        ("name,value\nA,1\n", {}, "WKT geometry column"),
        ("lon,lat\n1,2\n", {"wkt_column": "missing"}, "not found"),
        ("lon,lat\n1,2\n", {"lon_column": "lon"}, "Both lon_column and lat_column"),
    ],
    ids=["empty", "no-geometry-columns", "unknown-column", "half-coordinates"],
)
def test_unusable_csv_raises(text, kwargs, message):
    with pytest.raises(ValueError, match=message):
        csv_to_feature_collection(text, **kwargs)


def test_polygon_wkt_larger_than_default_csv_cell_limit_imports():
    # ~20k vertices -> WKT well above the csv module's 131072-char default.
    ring = [[i * 1e-5, 0.0] for i in range(10000)] + [[0.1 - i * 1e-5, 0.01] for i in range(10000)]
    ring.append(ring[0])
    feature = {
        "type": "Feature",
        "id": "big",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"class_id": "c1"},
    }
    text = features_to_csv([feature])
    assert len(text) > 131072

    (parsed,) = csv_to_feature_collection(text)["features"]

    assert parsed["id"] == "big"
    assert parsed["geometry"]["type"] == "Polygon"
    assert len(parsed["geometry"]["coordinates"][0]) == len(ring)


def test_csv_parse_error_becomes_value_error(monkeypatch):
    import app.services.conversion.csv_geojson as csv_geojson

    original = csv.field_size_limit()
    monkeypatch.setattr(csv_geojson, "_MAX_CELL_CHARS", 10)
    csv.field_size_limit(10)
    try:
        with pytest.raises(ValueError, match="Could not parse CSV"):
            csv_to_feature_collection("geometry_wkt\nPOINT (1.123456 2.123456)\n")
    finally:
        csv.field_size_limit(original)
