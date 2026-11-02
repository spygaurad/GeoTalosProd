"""Raster and vector reprojection between EPSG coordinate reference systems.

- ``parse_crs``: validate a user-supplied CRS (``"EPSG:3857"``, ``"3857"``…)
  and return its canonical string.
- ``reproject_raster_to_cog``: warp a GeoTIFF/COG into another CRS with
  Rasterio, then write the result through ``convert_geotiff_to_cog`` so the
  output is a validated COG.
- ``reproject_features``: transform GeoJSON feature geometries with pyproj +
  Shapely. Annotations are stored in EPSG:4326, so this is used on export
  only; the database is never written in another CRS.
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import nullcontext

from app.services.conversion.cog import _gdal_path, _validate_source, convert_geotiff_to_cog

logger = logging.getLogger(__name__)

WGS84 = "EPSG:4326"
RESAMPLING_METHODS = ("nearest", "bilinear", "cubic")


def parse_crs(value: str) -> str:
    """Return the canonical string for a CRS, e.g. ``"3857"`` -> ``"EPSG:3857"``.

    Raises ``ValueError`` when the CRS cannot be resolved.
    """
    from rasterio.crs import CRS
    from rasterio.errors import CRSError

    text = (value or "").strip()
    if not text:
        raise ValueError("CRS is required")
    if text.isdigit():
        text = f"EPSG:{text}"
    try:
        crs = CRS.from_user_input(text)
    except CRSError as exc:
        raise ValueError(f"Unsupported CRS {value!r}: {exc}") from exc
    return crs.to_string()


def reproject_raster_to_cog(
    input_path: str,
    output_path: str,
    dst_crs: str,
    *,
    resampling: str = "nearest",
    gdal_env: dict | None = None,
) -> str:
    """Warp ``input_path`` into ``dst_crs`` and write a COG to ``output_path``.

    Band count, dtypes and nodata are preserved. Pixels outside the source
    footprint are filled with the source nodata value (0 when none is set).
    Raises ``ValueError`` for unreadable input, an unsupported CRS or an
    unknown resampling method.
    """
    import rasterio
    from rasterio.env import Env
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    if resampling not in RESAMPLING_METHODS:
        raise ValueError(
            f"Reprojection failed: unknown resampling {resampling!r}; "
            f"use one of {', '.join(RESAMPLING_METHODS)}"
        )
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError(
            "Reprojection failed: input_path and output_path must be different"
        )
    dst_crs = parse_crs(dst_crs)

    env_ctx = Env(**gdal_env) if gdal_env else nullcontext()
    with env_ctx, tempfile.TemporaryDirectory() as tmpdir:
        warped_path = os.path.join(tmpdir, "warped.tif")
        try:
            src = rasterio.open(_gdal_path(input_path))
        except Exception as exc:
            raise ValueError(
                f"Reprojection failed: unreadable raster ({input_path}): {exc}"
            ) from exc

        try:
            try:
                _validate_source(src, input_path)
            except ValueError as exc:
                raise ValueError(str(exc).replace("COG creation", "Reprojection")) from exc

            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds
            )
            profile = src.profile.copy()
            profile.update(
                driver="GTiff",
                crs=dst_crs,
                transform=transform,
                width=width,
                height=height,
                tiled=True,
                blockxsize=512,
                blockysize=512,
                compress="deflate",
            )
            with rasterio.open(warped_path, "w", **profile) as dst:
                for band in src.indexes:
                    reproject(
                        source=rasterio.band(src, band),
                        destination=rasterio.band(dst, band),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=dst_crs,
                        src_nodata=src.nodata,
                        dst_nodata=src.nodata,
                        resampling=Resampling[resampling],
                    )
        finally:
            src.close()

        try:
            convert_geotiff_to_cog(warped_path, output_path)
        except ValueError as exc:
            raise ValueError(f"Reprojection failed: {exc}") from exc

    logger.info(
        "raster_reprojected src=%s dst_crs=%s size=%dx%d resampling=%s",
        input_path, dst_crs, width, height, resampling,
    )
    return output_path


def reproject_features(features: list[dict], dst_crs: str) -> list[dict]:
    """Return copies of EPSG:4326 GeoJSON features transformed to ``dst_crs``.

    Features without geometry are returned unchanged. Coordinates are kept in
    x/y (longitude/latitude) order.
    """
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform

    dst_crs = parse_crs(dst_crs)
    if dst_crs == WGS84:
        return [dict(f) for f in features]

    transformer = Transformer.from_crs(WGS84, dst_crs, always_xy=True)
    out = []
    for feature in features:
        copy = dict(feature)
        geom = feature.get("geometry")
        if geom:
            copy["geometry"] = mapping(transform(transformer.transform, shape(geom)))
        out.append(copy)
    return out
