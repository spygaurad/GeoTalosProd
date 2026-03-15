from __future__ import annotations

from typing import Any

from geoalchemy2.elements import WKBElement, WKTElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import mapping, shape


def parse_geometry(value: Any, *, srid: int = 4326) -> WKBElement | WKTElement | None:
    if value is None:
        return None
    if isinstance(value, (WKBElement, WKTElement)):
        return value
    if isinstance(value, str):
        return WKTElement(value, srid=srid)
    if isinstance(value, dict):
        return from_shape(shape(value), srid=srid)
    raise ValueError("Unsupported geometry payload")


def serialize_geometry(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (WKBElement, WKTElement)):
        return mapping(to_shape(value))
    return None
