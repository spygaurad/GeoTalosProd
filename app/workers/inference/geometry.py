from __future__ import annotations

from typing import Any

import numpy as np
from affine import Affine
from rasterio.features import shapes
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union


def bbox_to_polygon(bbox: list[float]) -> dict[str, Any]:
    if len(bbox) != 4:
        raise ValueError("bbox must be [minx, miny, maxx, maxy]")
    minx, miny, maxx, maxy = bbox
    return mapping(box(minx, miny, maxx, maxy))


def mask_to_polygon(
    mask: list[list[int]] | list[list[bool]],
    bbox: list[float],
    *,
    min_area_ratio: float = 0.0005,
) -> dict[str, Any] | None:
    if len(bbox) != 4:
        raise ValueError("bbox must be [minx, miny, maxx, maxy]")
    array = np.asarray(mask).astype(np.uint8)
    if array.ndim != 2:
        raise ValueError("mask must be a 2D matrix")
    if not np.any(array):
        return None

    minx, miny, maxx, maxy = bbox
    height, width = array.shape
    if width == 0 or height == 0:
        return None

    xres = (maxx - minx) / width
    yres = (maxy - miny) / height
    transform = Affine.translation(minx, maxy) * Affine.scale(xres, -yres)

    polygons = []
    for geom, value in shapes(array, mask=array > 0, transform=transform):
        if int(value) != 1:
            continue
        polygons.append(shape(geom))

    if not polygons:
        return None

    merged = unary_union(polygons)
    if merged.is_empty:
        return None

    full_bbox_area = max((maxx - minx) * (maxy - miny), 0.0)
    if full_bbox_area > 0:
        min_area = full_bbox_area * min_area_ratio
        if merged.geom_type == "Polygon":
            if merged.area < min_area:
                return None
        elif merged.geom_type == "MultiPolygon":
            kept = [geom for geom in merged.geoms if geom.area >= min_area]
            if not kept:
                return None
            merged = unary_union(kept)

    return mapping(merged)
