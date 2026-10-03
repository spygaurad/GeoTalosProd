"""Polygonize an in-memory class-mask array into GeoJSON features."""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("app.services.conversion.raster_mask")


def _normalize_value(value: float) -> str:
    """Normalize a pixel value to the string key used in ``value_class_map``."""
    return str(int(value)) if float(value).is_integer() else str(float(value))


def _prepare_value_class_map(
    value_class_map: dict[str, Any] | None,
    connectivity: int,
) -> tuple[dict[str, str], list[float]] | None:
    """Normalize the value map, or return ``None`` when it is empty."""
    norm_map: dict[str, str] = {
        str(k).strip(): str(v) for k, v in (value_class_map or {}).items()
    }
    if not norm_map:
        return None
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")

    mapped_numeric: list[float] = []
    for key in norm_map:
        try:
            mapped_numeric.append(float(key))
        except ValueError:
            _log.warning("raster_mask: non-numeric value_class_map key skipped key=%r", key)
    return norm_map, mapped_numeric


def _polygonize_prepared(
    band,
    transform,
    src_crs,
    norm_map: dict[str, str],
    mapped_numeric: list[float],
    *,
    nodata: float | None,
    simplify_tolerance: float | None,
    min_area_px: float,
    connectivity: int,
    source_tag: str,
) -> list[dict]:
    """Polygonize a band whose value map has already been normalized."""
    import numpy as np
    from rasterio.features import shapes
    from rasterio.warp import transform_geom
    from shapely.geometry import shape as shapely_shape

    pixel_area = abs(transform.a * transform.e) or 1.0

    valid = np.isin(band, mapped_numeric) if mapped_numeric else np.zeros(band.shape, dtype=bool)
    if nodata is not None:
        valid &= band != nodata
    if not valid.any():
        return []

    need_reproject = src_crs is not None and src_crs.to_epsg() != 4326

    features: list[dict] = []
    for geom, raster_value in shapes(
        band, mask=valid, transform=transform, connectivity=connectivity
    ):
        class_id = norm_map.get(_normalize_value(float(raster_value)))
        if class_id is None:
            continue

        shp = shapely_shape(geom)
        if min_area_px > 0 and (shp.area / pixel_area) < min_area_px:
            continue
        if simplify_tolerance and simplify_tolerance > 0:
            shp = shp.simplify(simplify_tolerance, preserve_topology=True)
        if shp.is_empty:
            continue
        if not shp.is_valid:
            shp = shp.buffer(0)
            if shp.is_empty:
                continue

        geom_out = shp.__geo_interface__
        if need_reproject:
            geom_out = transform_geom(src_crs, "EPSG:4326", geom_out)

        features.append(
            {
                "type": "Feature",
                "geometry": geom_out,
                "properties": {
                    "class_id": class_id,
                    "raster_value": float(raster_value),
                    "source": source_tag,
                },
            }
        )

    return features


def mask_array_to_features(
    band,
    transform,
    src_crs,
    *,
    value_class_map: dict[str, Any],
    nodata: float | None = None,
    simplify_tolerance: float | None = None,
    min_area_px: float = 0.0,
    connectivity: int = 4,
    source_tag: str = "raster_mask_vectorize",
) -> list[dict]:
    """Vectorize a 2D class-mask array into GeoJSON Features (EPSG:4326).

    Contiguous regions of mapped pixel values become Features. Unmapped values
    and nodata are skipped. Output is reprojected to EPSG:4326 when needed.
    """
    prepared = _prepare_value_class_map(value_class_map, connectivity)
    if prepared is None:
        return []
    norm_map, mapped_numeric = prepared
    return _polygonize_prepared(
        band,
        transform,
        src_crs,
        norm_map,
        mapped_numeric,
        nodata=nodata,
        simplify_tolerance=simplify_tolerance,
        min_area_px=min_area_px,
        connectivity=connectivity,
        source_tag=source_tag,
    )
