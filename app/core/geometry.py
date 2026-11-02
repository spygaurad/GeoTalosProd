from __future__ import annotations

from typing import Any

import numpy as np
from geoalchemy2.elements import WKBElement, WKTElement
from geoalchemy2.shape import from_shape, to_shape
from shapely import force_2d, get_coordinates
from shapely.geometry import mapping, shape
from shapely.validation import explain_validity


def parse_geometry(value: Any, *, srid: int = 4326) -> WKBElement | WKTElement | None:
    if value is None:
        return None
    if isinstance(value, (WKBElement, WKTElement)):
        return value
    if isinstance(value, str):
        return WKTElement(value, srid=srid)
    if isinstance(value, dict):
        # The geometry columns are 2D; a GeoJSON position may carry an altitude.
        return from_shape(force_2d(shape(value)), srid=srid)
    raise ValueError("Unsupported geometry payload")


def validate_wgs84_geometry(value: Any):
    """Parse a user-supplied GeoJSON geometry and check it can be stored and tiled.

    Returns the shapely geometry. Raises ValueError with a short reason for malformed
    or unknown types, empty geometry, non-finite or out-of-range coordinates (the
    columns are EPSG:4326) and invalid shapes such as a self-intersecting polygon.
    """
    try:
        geom = shape(value)
    except Exception as exc:  # shapely raises several types for bad input
        raise ValueError(f"Invalid geometry: {exc}") from exc
    if geom.is_empty:
        raise ValueError("Geometry is empty")
    xy = get_coordinates(geom)
    if not np.isfinite(xy).all() or (np.abs(xy[:, 0]) > 180).any() or (np.abs(xy[:, 1]) > 90).any():
        raise ValueError("Coordinates must be longitude/latitude in EPSG:4326 (x within -180..180, y within -90..90)")
    if not geom.is_valid:
        raise ValueError(f"Invalid geometry: {explain_validity(geom)}")
    return geom


def serialize_geometry(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (WKBElement, WKTElement)):
        return mapping(to_shape(value))
    return None


def serialize_bbox(value: Any) -> list[float] | None:
    """Convert a geometry value into a [minx, miny, maxx, maxy] bbox.

    Accepts a WKB/WKT element (as stored on `annotation_sets.extent_4326`),
    an already-computed bbox list, or None.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return [float(v) for v in value]
    if isinstance(value, (WKBElement, WKTElement)):
        minx, miny, maxx, maxy = to_shape(value).bounds
        return [minx, miny, maxx, maxy]
    return None
