"""Rasterize vector annotation sets into a segmentation-mask COG.

The inverse of ``raster_mask`` (mask -> vector): this turns one or more *vector*
annotation sets into a single-band class raster (COG) so a hand-drawn /
imported ground-truth set can be compared against a model-output mask in the
*raster* domain (per-class IoU / precision / recall over pixels), and rendered
on the map with class colors like any other ``segmentation_mask`` dataset.

Layers, smallest-useful first:
- ``build_value_class_map`` — pure-ish: schema + classes present -> deterministic
  pixel-value (1..N) -> class-id map (0 = background / nodata).
- ``read_annotation_burn_shapes`` — read polygons (EPSG:4326) + their pixel value.
- ``rasterize_annotation_sets_to_cog`` — service: union bounds, grid from a
  *reference dataset* (its native CRS + resolution, so the GT mask aligns
  pixel-for-pixel with the model output it will be compared to), burn a uint8
  COG to a local path, and report the value->class map for ingestion.

The caller (the ``rasterize_annotation_set`` Celery task) owns S3 upload and
dataset/STAC ingestion — this module only produces the COG + metadata so it
stays free of the worker-only ingestion machinery and is independently testable.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.workers.ingestion.rasterio_utils import _vsi_path

logger = logging.getLogger(__name__)

# Background / nodata pixel value. Mapped classes start at 1.
BACKGROUND_VALUE = 0
# Fallback resolution (metres/pixel in a metric CRS) when no reference dataset
# is given and no explicit resolution is configured.
DEFAULT_RESOLUTION_M = 1.0


def _normalize_value(value: float) -> str:
    """Match the key normalization used elsewhere for value->class maps."""
    return str(int(value)) if float(value).is_integer() else str(float(value))


@dataclass
class RasterizeResult:
    """What the rasterizer produced, for the caller to ingest."""
    schema_id: uuid.UUID | None
    value_class_map: dict[str, str]          # "1" -> class-id (str)
    class_names: dict[str, str]              # class-id (str) -> name
    pixel_counts: dict[str, int]             # class-id (str) -> burned pixel count
    width: int
    height: int
    crs: str                                 # e.g. "EPSG:32735"
    feature_count: int
    band_index: int = 1
    nodata_value: int = BACKGROUND_VALUE
    bounds: tuple[float, float, float, float] | None = None  # in `crs` units


def build_value_class_map(
    session,
    set_ids: list[uuid.UUID],
    *,
    class_filter: set[str] | None = None,
) -> tuple[uuid.UUID | None, dict[str, str], dict[str, str]]:
    """Build a deterministic value->class map from the classes present in the sets.

    Pixel values are assigned ``1..N`` ordered by class name (stable across
    runs). ``0`` is reserved for background. Returns
    ``(schema_id, value_class_map, class_names)`` where ``value_class_map`` is
    ``{"1": class_id, ...}`` and ``class_names`` is ``{class_id: name}``.
    """
    from sqlalchemy import select

    from app.models.annotation import Annotation
    from app.models.annotation_class import AnnotationClass
    from app.models.annotation_set import AnnotationSet

    rows = session.execute(
        select(AnnotationClass.id, AnnotationClass.name)
        .join(Annotation, Annotation.class_id == AnnotationClass.id)
        .where(
            Annotation.annotation_set_id.in_(set_ids),
            Annotation.deleted_at.is_(None),
        )
        .distinct()
    ).all()

    classes = [(str(cid), name or str(cid)) for cid, name in rows]
    if class_filter:
        classes = [(cid, name) for cid, name in classes if cid in class_filter]
    # Stable order: by name, then id — so the same sets always map to the same
    # pixel values (important when comparing two masks built independently).
    classes.sort(key=lambda c: (c[1].lower(), c[0]))

    value_class_map: dict[str, str] = {}
    class_names: dict[str, str] = {}
    for idx, (cid, name) in enumerate(classes, start=1):
        value_class_map[str(idx)] = cid
        class_names[cid] = name

    # Schema id: take it from the first set that has one (all sets compared
    # together are expected to share a schema).
    schema_id = session.execute(
        select(AnnotationSet.schema_id)
        .where(AnnotationSet.id.in_(set_ids), AnnotationSet.schema_id.isnot(None))
        .limit(1)
    ).scalar_one_or_none()

    return schema_id, value_class_map, class_names


def read_annotation_burn_shapes(
    session,
    set_ids: list[uuid.UUID],
    value_class_map: dict[str, str],
) -> list[tuple[dict, int]]:
    """Read annotation polygons as ``(geojson_geometry_4326, pixel_value)`` pairs.

    Only annotations whose class is in ``value_class_map`` are returned. Later
    classes (higher pixel values) win on overlap, because rasterize burns
    shapes in order.
    """
    import json

    from sqlalchemy import func, select

    from app.models.annotation import Annotation

    class_to_value = {cid: int(val) for val, cid in value_class_map.items()}

    rows = session.execute(
        select(
            Annotation.class_id,
            func.ST_AsGeoJSON(Annotation.geometry).label("geojson"),
        ).where(
            Annotation.annotation_set_id.in_(set_ids),
            Annotation.deleted_at.is_(None),
        )
    ).all()

    shapes: list[tuple[dict, int]] = []
    for class_id, geojson in rows:
        value = class_to_value.get(str(class_id))
        if value is None or not geojson:
            continue
        try:
            geom = json.loads(geojson)
        except (TypeError, ValueError):
            continue
        shapes.append((geom, value))
    return shapes


def _reference_grid(
    reference_item_uri: str | None,
    gdal_env: dict,
    resolution_m: float | None,
) -> tuple[str, float, float]:
    """Resolve target ``(crs, xres, yres)`` for the output mask.

    Prefers the reference dataset's native CRS + pixel size so the GT mask
    aligns with the raster it will be compared against. Falls back to a metric
    CRS (Web Mercator) at ``resolution_m`` (or the default) when no reference is
    available.
    """
    if reference_item_uri:
        import rasterio
        from rasterio.env import Env

        with Env(**gdal_env):
            with rasterio.open(_vsi_path(reference_item_uri)) as src:
                crs = src.crs.to_string() if src.crs else "EPSG:3857"
                xres, yres = src.res  # absolute pixel size in CRS units
                if resolution_m:
                    # Caller forced a resolution; keep the reference CRS but
                    # honour the requested ground sampling distance.
                    return crs, float(resolution_m), float(resolution_m)
                return crs, abs(float(xres)), abs(float(yres))

    res = float(resolution_m or DEFAULT_RESOLUTION_M)
    return "EPSG:3857", res, res


def rasterize_annotation_sets_to_cog(
    session,
    set_ids: list[uuid.UUID],
    out_path: str,
    *,
    gdal_env: dict,
    reference_item_uri: str | None = None,
    resolution_m: float | None = None,
    class_filter: set[str] | None = None,
    pad_pixels: int = 1,
    all_touched: bool = False,
) -> RasterizeResult:
    """Burn one or more vector annotation sets into a single-band uint8 COG.

    Args:
        session: synchronous SQLAlchemy session.
        set_ids: annotation sets to rasterize (all share a schema/grid).
        out_path: local filesystem path for the output COG.
        gdal_env: rasterio ``Env`` options for reading the reference COG.
        reference_item_uri: ``s3://`` of a dataset item whose CRS + resolution
            define the output grid (typically the model-output mask the GT will
            be compared to). ``None`` -> Web Mercator at ``resolution_m``.
        resolution_m: explicit ground sampling distance; overrides the
            reference's native resolution when given.
        class_filter: restrict to these class-ids (strings).
        pad_pixels: border of background pixels around the burned bounds.
        all_touched: rasterize every pixel a polygon touches (vs. cell-centre).
    """
    import numpy as np
    import rasterio
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from rasterio.warp import transform_geom
    from shapely.geometry import shape as shapely_shape

    if not set_ids:
        raise ValueError("rasterize_annotation_sets_to_cog requires at least one set id")

    schema_id, value_class_map, class_names = build_value_class_map(
        session, set_ids, class_filter=class_filter
    )
    if not value_class_map:
        raise ValueError(
            "No annotation classes found in the input set(s) — nothing to rasterize"
        )

    burn_shapes_4326 = read_annotation_burn_shapes(session, set_ids, value_class_map)
    if not burn_shapes_4326:
        raise ValueError("Input set(s) contain no annotations to rasterize")

    crs, xres, yres = _reference_grid(reference_item_uri, gdal_env, resolution_m)

    # Reproject every geometry from 4326 into the target CRS, tracking bounds.
    burn_shapes: list[tuple[dict, int]] = []
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for geom_4326, value in burn_shapes_4326:
        geom = transform_geom("EPSG:4326", crs, geom_4326) if crs != "EPSG:4326" else geom_4326
        shp = shapely_shape(geom)
        if shp.is_empty:
            continue
        b = shp.bounds  # (minx, miny, maxx, maxy)
        minx, miny = min(minx, b[0]), min(miny, b[1])
        maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
        burn_shapes.append((geom, value))

    if not burn_shapes or not math.isfinite(minx):
        raise ValueError("No valid geometries after reprojection")

    # Snap bounds outward to whole pixels and pad with a background border.
    minx -= pad_pixels * xres
    maxy += pad_pixels * yres
    maxx += pad_pixels * xres
    miny -= pad_pixels * yres
    width = max(1, int(math.ceil((maxx - minx) / xres)))
    height = max(1, int(math.ceil((maxy - miny) / yres)))
    transform = from_origin(minx, maxy, xres, yres)

    # Sort by pixel value so higher class values are burned last (win overlaps).
    burn_shapes.sort(key=lambda s: s[1])
    arr = rasterize(
        burn_shapes,
        out_shape=(height, width),
        transform=transform,
        fill=BACKGROUND_VALUE,
        all_touched=all_touched,
        dtype="uint8",
    )

    pixel_counts: dict[str, int] = {}
    for val_str, cid in value_class_map.items():
        count = int(np.count_nonzero(arr == int(val_str)))
        if count:
            pixel_counts[cid] = count

    profile = {
        "driver": "COG",
        "dtype": "uint8",
        "count": 1,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": transform,
        "nodata": BACKGROUND_VALUE,
        "compress": "deflate",
        "blocksize": 512,
    }
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(arr, 1)

    logger.info(
        "rasterize_annotation_sets: sets=%s crs=%s size=%dx%d classes=%d features=%d",
        [str(s) for s in set_ids], crs, width, height, len(value_class_map), len(burn_shapes),
    )

    return RasterizeResult(
        schema_id=schema_id,
        value_class_map=value_class_map,
        class_names=class_names,
        pixel_counts=pixel_counts,
        width=width,
        height=height,
        crs=crs,
        feature_count=len(burn_shapes),
        bounds=(minx, miny, maxx, maxy),
    )
