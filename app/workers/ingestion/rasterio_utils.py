"""
COG validation, metadata extraction, and STAC object builders.

All rasterio / shapely imports are inside the functions (lazy) so that
importing this module at API startup does not load native libs.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _vsi_path(s3_uri: str) -> str:
    """Convert ``s3://bucket/key`` to GDAL VSI path ``/vsis3/bucket/key``."""
    return s3_uri.replace("s3://", "/vsis3/", 1)


def _extract_datetime(tags: dict, filename: str) -> str:
    """Return an ISO-8601 UTC datetime string for the image acquisition time.

    Sources tried in order:
    1. TIFFTAG_DATETIME (format ``YYYY:MM:DD HH:MM:SS``)
    2. ACQUISITIONDATETIME or DATE metadata tags
    3. ISO date ``YYYY-MM-DD`` anywhere in the filename
    4. Current UTC time (logged as a warning)
    """
    # 1 + 2: GDAL / TIFF metadata tags
    for key in ("TIFFTAG_DATETIME", "ACQUISITIONDATETIME", "DATE"):
        raw = tags.get(key, "").strip()
        if not raw:
            continue
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue

    # 3: ISO date in filename
    match = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(filename))
    if match:
        try:
            dt = datetime.fromisoformat(match.group(1))
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass

    # 4: fallback
    logger.warning("datetime_not_found filename=%s — using current UTC", filename)
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────────────────────

def validate_cog(s3_uri: str, s3_config: dict) -> tuple[bool, list[str]]:
    """Validate that the file at *s3_uri* can be ingested as a raster.

    Hard failures (returns False — file is rejected):
    - File cannot be opened via GDAL VSI
    - File has no raster bands

    Soft warnings (returns True with non-empty issues — file is ingested
    but the issues are logged so operators know it is not COG-optimised):
    - No block (tile) structure (non-tiled TIFF)
    - Block size > 512 px
    - No overview levels (tiles will be slow at low zoom)
    - No compression (file will be large in storage)

    This follows industry practice: accept any valid GeoTIFF for ingestion,
    but flag files that are not Cloud Optimized so they can be re-uploaded
    as COGs for better tile performance.

    Returns ``(is_valid, issues)`` where *issues* lists all problems found.
    *is_valid* is False only on hard failures.
    """
    import rasterio
    from rasterio.env import Env

    vsi = _vsi_path(s3_uri)
    issues: list[str] = []
    hard_failure = False

    with Env(**s3_config):
        try:
            with rasterio.open(vsi) as src:
                if not src.indexes:
                    issues.append("File has no raster bands")
                    hard_failure = True
                elif src.crs is None:
                    issues.append("File has no coordinate reference system — not a valid georeferenced raster")
                    hard_failure = True
                else:
                    # Soft: tiling
                    block_shapes = src.block_shapes
                    if not block_shapes or all(bs is None for bs in block_shapes):
                        issues.append("No tile block structure — not COG-optimised (will render slowly)")
                    else:
                        for bs in block_shapes:
                            if bs is not None and (bs[0] > 512 or bs[1] > 512):
                                issues.append(
                                    f"Block size {bs} > 512 px — consider retiling at 256 or 512"
                                )
                                break

                    # Soft: overviews
                    if not any(src.overviews(i) for i in src.indexes):
                        issues.append("No overview levels — low-zoom tiles will be slow")

                    # Soft: compression
                    if src.profile.get("compress") is None:
                        issues.append("No compression — storage size will be larger than necessary")

        except Exception as exc:
            issues.append(f"Could not open file: {exc}")
            hard_failure = True

    return not hard_failure, issues


def extract_cog_metadata(s3_uri: str, s3_config: dict) -> dict:
    """Extract spatial and radiometric metadata from a COG.

    Returns a dict with keys:
        bbox            [west, south, east, north]  EPSG:4326
        native_crs      CRS string, e.g. "EPSG:32637"
        width           pixel columns
        height          pixel rows
        gsd_meters      ground sample distance in metres
        bands           list of {index, dtype, nodata}
        datetime        ISO-8601 UTC string
        file_size_bytes int or None
    """
    import rasterio
    from rasterio.env import Env
    from rasterio.warp import transform_bounds

    vsi = _vsi_path(s3_uri)

    with Env(**s3_config):
        with rasterio.open(vsi) as src:
            # Reproject bounds to EPSG:4326
            west, south, east, north = transform_bounds(src.crs, "EPSG:4326", *src.bounds)

            # GSD: native pixel size in metres (approximate for geographic CRS)
            res_x = abs(src.res[0])
            if src.crs and src.crs.is_geographic:
                gsd_meters = round(res_x * 111_000, 4)
            else:
                gsd_meters = round(res_x, 4)

            bands = [
                {
                    "index": i,
                    "dtype": str(src.dtypes[i - 1]),
                    "nodata": src.nodata,
                }
                for i in src.indexes
            ]

            tags = src.tags()
            item_datetime = _extract_datetime(tags, vsi)

            # File size: attempt GDAL VSIStatL, fall back to None
            file_size_bytes: int | None = None
            try:
                from osgeo import gdal  # optional; only available with GDAL Python bindings

                stat = gdal.VSIStatL(vsi)
                if stat is not None:
                    file_size_bytes = stat.size
            except Exception:
                pass

            return {
                "bbox": [west, south, east, north],
                "native_crs": src.crs.to_string() if src.crs else None,
                "width": src.width,
                "height": src.height,
                "gsd_meters": gsd_meters,
                "bands": bands,
                "datetime": item_datetime,
                "file_size_bytes": file_size_bytes,
            }


def build_stac_item(
    item_id: str,
    collection_id: str,
    s3_uri: str,
    metadata: dict,
) -> dict:
    """Build a STAC 1.0 Item dict for a single COG.

    The asset ``href`` is the raw ``s3://`` URI.  titiler-pgstac resolves it
    via its own MinIO / S3 environment configuration.
    """
    west, south, east, north = metadata["bbox"]

    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "collection": collection_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
        "bbox": [west, south, east, north],
        "properties": {
            "datetime": metadata["datetime"],
            "gsd": metadata.get("gsd_meters"),
            "proj:epsg": _epsg_code(metadata.get("native_crs")),
            "proj:shape": [metadata.get("height"), metadata.get("width")],
            "native_crs": metadata.get("native_crs"),
            "file_size_bytes": metadata.get("file_size_bytes"),
            # Store band dtypes as a flat list for collection-level aggregation
            "bands": [b["dtype"] for b in metadata.get("bands", [])],
        },
        "assets": {
            "data": {
                "href": s3_uri,
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data", "visual"],
                "title": metadata.get("filename", "COG"),
            }
        },
        "links": [],
    }


def build_stac_collection(
    collection_id: str,
    org_id: str,
    dataset_name: str,
) -> dict:
    """Build a minimal STAC 1.0 Collection shell.

    pgSTAC will update ``extent`` incrementally as items are upserted, so
    the initial values are placeholders.
    """
    return {
        "type": "Collection",
        "id": collection_id,
        "stac_version": "1.0.0",
        "title": dataset_name,
        "description": f"Imagery collection for dataset '{dataset_name}' (org {org_id})",
        "license": "proprietary",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [[None, None]]},
        },
        "links": [],
    }


def _epsg_code(crs_string: str | None) -> int | None:
    """Extract numeric EPSG code from a CRS string like ``EPSG:32637``."""
    if not crs_string:
        return None
    match = re.search(r":(\d+)$", crs_string)
    return int(match.group(1)) if match else None
