"""User-supplied geometry checks (app/core/geometry.py) and the annotation confidence range."""

import pytest
from pydantic import ValidationError

from app.core.geometry import parse_geometry, validate_wgs84_geometry
from app.schemas.annotation import AnnotationCreate, AnnotationUpdate

SQUARE = [[[90.4, 23.78], [90.41, 23.78], [90.41, 23.79], [90.4, 23.79], [90.4, 23.78]]]


def test_valid_polygon_and_point_pass():
    assert validate_wgs84_geometry({"type": "Polygon", "coordinates": SQUARE}).geom_type == "Polygon"
    assert validate_wgs84_geometry({"type": "Point", "coordinates": [90.4, 23.78]}).geom_type == "Point"


def test_unclosed_ring_is_closed_by_the_parser():
    ring = [SQUARE[0][:-1]]
    assert validate_wgs84_geometry({"type": "Polygon", "coordinates": ring}).is_valid


@pytest.mark.parametrize(
    "geometry, reason",
    [
        ({"type": "Blob", "coordinates": []}, "Invalid geometry"),
        ({"type": "Polygon", "coordinates": None}, "empty"),
        ({"coordinates": [1, 2]}, "Invalid geometry"),
        ({"type": "Polygon", "coordinates": []}, "empty"),
        ({"type": "Polygon", "coordinates": [[[90.4, 23.78], [90.41, 23.78], [90.4, 23.78]]]}, "Invalid geometry"),
        ({"type": "Polygon", "coordinates": [[[500, 23], [501, 23], [501, 24], [500, 24], [500, 23]]]}, "EPSG:4326"),
        ({"type": "Polygon", "coordinates": [[[90, 95], [91, 95], [91, 96], [90, 96], [90, 95]]]}, "EPSG:4326"),
        ({"type": "Point", "coordinates": [float("nan"), 23.0]}, "EPSG:4326"),
        ({"type": "Point", "coordinates": [float("inf"), 23.0]}, "EPSG:4326"),
        (
            {"type": "Polygon", "coordinates": [[[90.4, 23.78], [90.41, 23.79], [90.41, 23.78], [90.4, 23.79], [90.4, 23.78]]]},
            "Self-intersection",
        ),
    ],
)
def test_invalid_geometry_is_rejected_with_a_reason(geometry, reason):
    with pytest.raises(ValueError, match=reason):
        validate_wgs84_geometry(geometry)


def test_altitude_is_dropped_before_storing():
    stored = parse_geometry({"type": "Point", "coordinates": [90.4, 23.78, 10]})
    assert stored.desc  # a WKB element; 2D, so the geometry column accepts it
    assert not parse_geometry({"type": "Point", "coordinates": [90.4, 23.78, 10]}).data.hex().startswith("0101000080")


@pytest.mark.parametrize("confidence", [-0.1, 1.5, 100])
def test_confidence_outside_zero_to_one_is_rejected(confidence):
    body = {"class_id": "6ec2bb0c-fb75-4f89-81e0-c4adbdcf959c", "geometry": {"type": "Point", "coordinates": [1, 2]}}
    with pytest.raises(ValidationError):
        AnnotationCreate(**body, confidence=confidence)
    with pytest.raises(ValidationError):
        AnnotationUpdate(confidence=confidence)


@pytest.mark.parametrize("confidence", [0, 0.5, 1, None])
def test_confidence_inside_zero_to_one_is_accepted(confidence):
    body = {"class_id": "6ec2bb0c-fb75-4f89-81e0-c4adbdcf959c", "geometry": {"type": "Point", "coordinates": [1, 2]}}
    assert AnnotationCreate(**body, confidence=confidence).confidence == confidence
