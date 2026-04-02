"""Geometry conversion utilities.

Boundary contract:
  - API layer (request/response): plain GeoJSON dicts  ({"type": "Polygon", "coordinates": ...})
  - ORM / service layer:          geoalchemy2 elements  (WKTElement, WKBElement)

Functions here handle crossing that boundary. Never pass WKBElement to a Pydantic
schema and never write raw GeoJSON strings into a Geometry column.
"""

import json
from typing import Any

from geoalchemy2.shape import from_shape, to_shape
from geoalchemy2.types import WKBElement
from shapely.geometry import box, mapping, shape
from shapely.wkt import loads as wkt_loads


def geojson_to_wkt_element(geojson: dict[str, Any], srid: int = 4326):
    """Convert a GeoJSON geometry dict to a geoalchemy2 WKTElement.

    Raises ValueError if the geometry is not valid according to Shapely.
    """
    try:
        geom = shape(geojson)
    except Exception as exc:
        raise ValueError(f"Invalid GeoJSON geometry: {exc}") from exc
    if not geom.is_valid:
        raise ValueError(f"Geometry is not valid: {geom.wkt[:120]}")
    from geoalchemy2 import WKTElement
    return WKTElement(geom.wkt, srid=srid)


def wkb_to_geojson(wkb: WKBElement | None) -> dict[str, Any] | None:
    """Convert a geoalchemy2 WKBElement to a GeoJSON geometry dict.

    Returns None if wkb is None (nullable geometry columns).
    """
    if wkb is None:
        return None
    return mapping(to_shape(wkb))


def bbox_to_wkt(bbox: list[float], srid: int = 4326) -> str:
    """Convert [xmin, ymin, xmax, ymax] to a WKT POLYGON string."""
    xmin, ymin, xmax, ymax = bbox
    return box(xmin, ymin, xmax, ymax).wkt


def wkt_to_geojson(wkt: str) -> dict[str, Any]:
    """Convert a WKT string to a GeoJSON geometry dict."""
    return mapping(wkt_loads(wkt))


def union_bboxes_to_wkt(bboxes_4326: list[tuple[float, float, float, float]]) -> str:
    """Return WKT of the union of a list of (minx, miny, maxx, maxy) tuples."""
    from shapely.ops import unary_union
    geoms = [box(xmin, ymin, xmax, ymax) for xmin, ymin, xmax, ymax in bboxes_4326]
    return unary_union(geoms).wkt
