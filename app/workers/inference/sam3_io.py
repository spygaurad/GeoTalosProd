"""Raster I/O helpers for SAM3 inference.

All rasterio / numpy / shapely imports are lazy (inside functions) so this
module is safe to import from Celery task modules at worker startup without
forcing native lib loads in test environments.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _vsi_path(s3_uri: str) -> str:
    """Convert ``s3://bucket/key`` to GDAL VSI path ``/vsis3/bucket/key``."""
    return s3_uri.replace("s3://", "/vsis3/", 1)


def _split_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Expected s3:// URI, got: {s3_uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def _gdal_env() -> dict:
    from app.config import settings
    endpoint = settings.AWS_ENDPOINT_URL.replace("http://", "").replace("https://", "")
    use_https = settings.AWS_ENDPOINT_URL.startswith("https://")
    return {
        "AWS_S3_ENDPOINT": endpoint,
        "AWS_HTTPS": "YES" if use_https else "NO",
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_REGION": settings.AWS_REGION,
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
    }


def load_raster_chip(
    s3_uri: str, aoi_geom_4326: dict | None = None
) -> tuple[Any, dict]:
    """Load raster from S3, optionally clip to AOI polygon (EPSG:4326).

    Returns (array, meta):
        array: numpy ndarray (bands, H, W)
        meta: {transform, crs, width, height, bbox_4326, dtype, nodata}
    """
    import numpy as np
    import rasterio
    from rasterio.env import Env
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_bounds
    from shapely.geometry import shape as shp_shape
    from shapely.ops import transform as shp_transform
    import pyproj

    with Env(**_gdal_env()):
        with rasterio.open(_vsi_path(s3_uri)) as src:
            src_crs = src.crs

            if aoi_geom_4326 is not None:
                # Reproject AOI from EPSG:4326 to raster CRS if needed
                aoi_shape = shp_shape(aoi_geom_4326)
                if src_crs and src_crs.to_epsg() != 4326:
                    project = pyproj.Transformer.from_crs(
                        "EPSG:4326", src_crs, always_xy=True
                    ).transform
                    aoi_native = shp_transform(project, aoi_shape)
                else:
                    aoi_native = aoi_shape

                array, transform = rio_mask(
                    src,
                    [aoi_native.__geo_interface__],
                    crop=True,
                    all_touched=True,
                    filled=True,
                )
                height, width = array.shape[1], array.shape[2]
                # Native bounds of clipped array
                left = transform.c
                top = transform.f
                right = left + width * transform.a
                bottom = top + height * transform.e
                native_bounds = (min(left, right), min(top, bottom),
                                 max(left, right), max(top, bottom))
            else:
                array = src.read()
                transform = src.transform
                height, width = src.height, src.width
                native_bounds = src.bounds

            # Compute EPSG:4326 bbox for downstream use
            if src_crs and src_crs.to_epsg() != 4326:
                bbox_4326 = list(transform_bounds(src_crs, "EPSG:4326", *native_bounds))
            else:
                bbox_4326 = list(native_bounds)

            meta = {
                "transform": transform,
                "crs": src_crs,
                "width": width,
                "height": height,
                "bbox_4326": bbox_4326,
                "dtype": str(array.dtype),
                "nodata": src.nodata,
                "count": array.shape[0],
            }
            return array, meta


def array_to_b64_tiff(array: Any, meta: dict) -> str:
    """Encode array as an in-memory GeoTIFF and return base64-encoded bytes."""
    import rasterio
    from rasterio.io import MemoryFile

    profile = {
        "driver": "GTiff",
        "dtype": str(array.dtype),
        "count": array.shape[0],
        "height": array.shape[1],
        "width": array.shape[2],
        "transform": meta["transform"],
        "crs": meta["crs"],
        "compress": "deflate",
    }
    if meta.get("nodata") is not None:
        profile["nodata"] = meta["nodata"]

    with MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            dst.write(array)
        data = memfile.read()
    return base64.b64encode(data).decode("ascii")


def lonlat_to_pixel(
    points_lonlat: list[list[float]], transform: Any, crs: Any
) -> list[list[float]]:
    """Convert [[lon, lat], ...] (EPSG:4326) to [[x_px, y_px], ...] pixel space."""
    import pyproj
    from affine import Affine

    if not points_lonlat:
        return []

    # Reproject to raster CRS if needed
    if crs and crs.to_epsg() != 4326:
        transformer = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        native = [transformer.transform(lon, lat) for lon, lat in points_lonlat]
    else:
        native = [(lon, lat) for lon, lat in points_lonlat]

    inv: Affine = ~transform
    result = []
    for x, y in native:
        px, py = inv * (x, y)
        result.append([float(px), float(py)])
    return result


def bbox_to_pixel(
    bbox_4326: list[float], transform: Any, crs: Any
) -> list[float]:
    """Convert [minx, miny, maxx, maxy] in EPSG:4326 to pixel-space [x1, y1, x2, y2]."""
    corners = [
        [bbox_4326[0], bbox_4326[1]],
        [bbox_4326[2], bbox_4326[3]],
    ]
    px_corners = lonlat_to_pixel(corners, transform, crs)
    x1, y1 = px_corners[0]
    x2, y2 = px_corners[1]
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def decode_mask_png(mask_png_b64: str) -> Any:
    """Decode a base64 PNG containing a single-band uint16 instance-ID mask."""
    import numpy as np
    from PIL import Image

    raw = base64.b64decode(mask_png_b64)
    with Image.open(io.BytesIO(raw)) as img:
        arr = np.array(img)
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    return arr


def mask_to_cog(
    mask: Any,
    transform: Any,
    crs: Any,
    s3_uri: str,
) -> dict:
    """Write a uint16 instance-ID mask as a Cloud Optimized GeoTIFF to S3.

    Returns raster_config dict describing the written COG.
    """
    import numpy as np
    import boto3
    from botocore.client import Config
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import transform_bounds

    from app.config import settings

    if mask.ndim == 2:
        mask_arr = mask[np.newaxis, :, :]
    else:
        mask_arr = mask

    profile = {
        "driver": "GTiff",
        "dtype": "uint16",
        "count": 1,
        "height": mask_arr.shape[1],
        "width": mask_arr.shape[2],
        "transform": transform,
        "crs": crs,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "nodata": 0,
        "BIGTIFF": "IF_SAFER",
    }

    with MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            dst.write(mask_arr.astype("uint16"))
            # Build internal overviews for COG compliance
            dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.nearest)
            dst.update_tags(ns="rio_overview", resampling="nearest")
        data = memfile.read()

    bucket, key = _split_s3_uri(s3_uri)
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.AWS_ENDPOINT_URL or None,
        region_name=settings.AWS_REGION,
        config=Config(s3={"addressing_style": "path" if settings.AWS_S3_FORCE_PATH_STYLE else "auto"}),
    )
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/tiff")

    # Native bounds of the mask
    height, width = mask_arr.shape[1], mask_arr.shape[2]
    left = transform.c
    top = transform.f
    right = left + width * transform.a
    bottom = top + height * transform.e
    native_bounds = (min(left, right), min(top, bottom), max(left, right), max(top, bottom))
    if crs and crs.to_epsg() != 4326:
        bounds_4326 = list(transform_bounds(crs, "EPSG:4326", *native_bounds))
    else:
        bounds_4326 = list(native_bounds)

    # Count unique non-zero instance IDs
    unique_ids = [int(v) for v in np.unique(mask_arr) if v != 0]

    return {
        "s3_uri": s3_uri,
        "crs": crs.to_string() if crs else "EPSG:4326",
        "bounds_4326": bounds_4326,
        "width": int(width),
        "height": int(height),
        "dtype": "uint16",
        "instance_count": len(unique_ids),
        "instance_ids": unique_ids,
    }


def write_mask_sidecar(
    sidecar_s3_uri: str, instance_metadata: dict[int, dict]
) -> str:
    """Write `{instance_id: {label, confidence, class_id}}` JSON next to the COG.

    Returns the S3 URI of the written sidecar.
    """
    import boto3
    from botocore.client import Config

    from app.config import settings

    # JSON keys must be strings
    payload = {str(k): v for k, v in instance_metadata.items()}
    body = json.dumps(payload, default=str).encode("utf-8")

    bucket, key = _split_s3_uri(sidecar_s3_uri)
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.AWS_ENDPOINT_URL or None,
        region_name=settings.AWS_REGION,
        config=Config(s3={"addressing_style": "path" if settings.AWS_S3_FORCE_PATH_STYLE else "auto"}),
    )
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return sidecar_s3_uri
