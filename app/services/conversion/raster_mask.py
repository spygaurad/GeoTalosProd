"""Convert raster segmentation masks (COG) into vector annotations.

A raster-backed annotation set stores a class mask as a COG plus a
``raster_config.value_class_map`` (pixel value -> annotation class id). Metrics
nodes (Ground Truth Comparison, area/IoU) intersect polygons in PostGIS, so the
ground truth must be *vector* geometry. This module turns such a mask into
per-component polygons in EPSG:4326, mapped to the schema's annotation classes —
directly comparable to vector model outputs (e.g. the SAM3 adapter masks).

Two layers, so callers can use as much as they need:
- ``raster_mask_to_features``  — pure: COG -> list of GeoJSON Features (4326).
- ``dissolve_features_by_class`` — pure: per-component -> one (Multi)Polygon/class.
- ``vectorize_raster_mask_set`` — service: materialize a NEW vector AnnotationSet
  from a raster-mask AnnotationSet, ready for comparison nodes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.workers.ingestion.rasterio_utils import _vsi_path

logger = logging.getLogger(__name__)


def _normalize_value(value: float) -> str:
    """Match the key normalization used when the value->class map was stored.

    Mirrors ``_coerce_value_map`` in the annotation-sets endpoint: integral
    values become ``"5"``, non-integral stay ``"5.5"`` — so lookups against a
    stored ``value_class_map`` hit the same keys.
    """
    return str(int(value)) if float(value).is_integer() else str(float(value))


def raster_mask_to_features(
    s3_uri: str,
    gdal_env: dict,
    *,
    value_class_map: dict[str, Any],
    band_index: int = 1,
    nodata_value: float | None = None,
    simplify_tolerance: float | None = None,
    min_area_px: float = 0.0,
    connectivity: int = 4,
) -> list[dict]:
    """Vectorize a raster class mask into GeoJSON Features (EPSG:4326).

    Each contiguous region of a mapped pixel value becomes one Feature whose
    ``properties.class_id`` is the mapped annotation-class id. Unmapped values
    and nodata are skipped. Polygon holes are preserved (rasterio emits interior
    rings where a region encloses a different value).

    Args:
        s3_uri: ``s3://bucket/key`` of the COG mask.
        gdal_env: rasterio ``Env`` options (endpoint/region/path-style); see the
            project's ``_gdal_env_for_worker``.
        value_class_map: normalized pixel-value string -> annotation class id.
        band_index: 1-based band holding the class mask.
        nodata_value: overrides the file's nodata when excluding background.
        simplify_tolerance: Douglas-Peucker tolerance in *source CRS* units,
            applied before reprojection. ``None`` keeps full detail.
        min_area_px: drop regions smaller than this many pixels.
        connectivity: 4 or 8 — pixel connectivity for region growing.

    Returns:
        A list of GeoJSON Feature dicts in EPSG:4326.
    """
    import numpy as np
    import rasterio
    from rasterio.env import Env
    from rasterio.features import shapes
    from rasterio.warp import transform_geom
    from shapely.geometry import shape as shapely_shape

    norm_map: dict[str, str] = {str(k).strip(): str(v) for k, v in (value_class_map or {}).items()}
    if not norm_map:
        return []
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")

    mapped_numeric: list[float] = []
    for key in norm_map:
        try:
            mapped_numeric.append(float(key))
        except ValueError:
            logger.warning("raster_mask: non-numeric value_class_map key skipped key=%r", key)

    vsi = _vsi_path(s3_uri)
    features: list[dict] = []

    with Env(**gdal_env):
        with rasterio.open(vsi) as src:
            if band_index < 1 or band_index > src.count:
                raise ValueError(
                    f"band_index {band_index} out of range (raster has {src.count} bands)"
                )
            band = src.read(band_index)
            src_crs = src.crs
            transform = src.transform
            file_nodata = src.nodata

    pixel_area = abs(transform.a * transform.e) or 1.0
    nodata = nodata_value if nodata_value is not None else file_nodata

    # Only vectorize mapped values; exclude nodata background.
    valid = np.isin(band, mapped_numeric) if mapped_numeric else np.zeros(band.shape, dtype=bool)
    if nodata is not None:
        valid &= band != nodata
    if not valid.any():
        return []

    need_reproject = src_crs is not None and src_crs.to_epsg() != 4326

    for geom, raster_value in shapes(band, mask=valid, transform=transform, connectivity=connectivity):
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
                    "source": "raster_mask_vectorize",
                },
            }
        )

    return features


def dissolve_features_by_class(features: list[dict]) -> list[dict]:
    """Merge per-component features into one (Multi)Polygon per class id.

    Use when a single semantic/area IoU per class is wanted instead of
    instance-level comparison.
    """
    from shapely.geometry import shape as shapely_shape
    from shapely.ops import unary_union

    by_class: dict[str, list] = {}
    for feat in features:
        cid = feat.get("properties", {}).get("class_id")
        if not cid:
            continue
        by_class.setdefault(str(cid), []).append(shapely_shape(feat["geometry"]))

    out: list[dict] = []
    for cid, geoms in by_class.items():
        merged = unary_union(geoms)
        if merged.is_empty:
            continue
        if not merged.is_valid:
            merged = merged.buffer(0)
        out.append(
            {
                "type": "Feature",
                "geometry": merged.__geo_interface__,
                "properties": {"class_id": cid, "source": "raster_mask_vectorize", "dissolved": True},
            }
        )
    return out


@dataclass
class RasterMaskVectorizeResult:
    annotation_set_id: uuid.UUID
    feature_count: int
    class_counts: dict[str, int] = field(default_factory=dict)


def vectorize_raster_mask_set(
    session,
    raster_set,
    *,
    gdal_env: dict,
    name: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
    simplify_tolerance: float | None = None,
    min_area_px: float = 0.0,
    connectivity: int = 4,
    dissolve_by_class: bool = False,
    confidence: float | None = 1.0,
    class_filter: set[str] | None = None,
    commit: bool = True,
) -> RasterMaskVectorizeResult:
    """Materialize a raster-mask annotation set as a NEW vector annotation set.

    Reads ``raster_set.raster_config`` + the backing COG, vectorizes per class,
    and writes ``Annotation`` rows into a fresh set (same org + schema) so the
    result is directly usable by Ground Truth Comparison and area/IoU nodes.
    The source raster set is left untouched.

    Args:
        session: a synchronous SQLAlchemy session (e.g. ``WorkerSession()``).
        raster_set: an ``AnnotationSet`` carrying ``raster_config``.
        gdal_env: rasterio ``Env`` options for reading the COG from object store.
        created_by_user_id: creator for the new set; falls back to the source
            set's creator (an annotation set requires a user or job creator).
        dissolve_by_class: store one merged geometry per class instead of one per
            connected component.
        confidence: value written to each annotation (``1.0`` for ground truth).
        class_filter: when given, only extract these annotation-class ids (as
            strings); ``value_class_map`` entries pointing at other classes are
            dropped before vectorizing. ``None`` extracts every mapped class.
        commit: ``True`` (script/task) commits the new rows; ``False`` only
            flushes so a caller that owns the transaction (e.g. an automation
            node) commits later.
    """
    from app.core.geometry import parse_geometry
    from app.models.annotation import Annotation
    from app.models.annotation_set import AnnotationSet
    from app.models.dataset_item import DatasetItem

    cfg = raster_set.raster_config or {}
    if not cfg:
        raise ValueError("Annotation set has no raster_config; not a raster mask set")
    value_class_map = cfg.get("value_class_map") or {}
    if not value_class_map:
        raise ValueError("raster_config has no value_class_map")

    if class_filter:
        value_class_map = {
            k: v for k, v in value_class_map.items() if str(v) in class_filter
        }
        if not value_class_map:
            raise ValueError(
                "No value→class entries remain after applying the class filter"
            )

    item_id = cfg.get("dataset_item_id")
    item = session.get(DatasetItem, uuid.UUID(str(item_id))) if item_id else None
    if item is None:
        raise ValueError("raster_config.dataset_item_id does not resolve to a dataset item")

    creator = created_by_user_id or raster_set.created_by_user_id
    if creator is None:
        raise ValueError(
            "No creator available: pass created_by_user_id (the new annotation set "
            "requires a user or job creator)"
        )

    features = raster_mask_to_features(
        item.s3_uri,
        gdal_env,
        value_class_map=value_class_map,
        band_index=int(cfg.get("band_index", 1)),
        nodata_value=cfg.get("nodata_value"),
        simplify_tolerance=simplify_tolerance,
        min_area_px=min_area_px,
        connectivity=connectivity,
    )
    if dissolve_by_class:
        features = dissolve_features_by_class(features)

    target_set = AnnotationSet(
        organization_id=raster_set.organization_id,
        schema_id=raster_set.schema_id,
        dataset_id=raster_set.dataset_id,
        dataset_item_id=raster_set.dataset_item_id,
        source_type="import",
        name=name or f"{raster_set.name} · vectorized",
        description=f"Vectorized from raster mask set {raster_set.id}",
        created_by_user_id=creator,
    )
    session.add(target_set)
    session.flush()

    from app.services.annotation_set_grouping import ensure_schema_collection_sync
    ensure_schema_collection_sync(session, target_set)

    class_counts: dict[str, int] = {}
    for feat in features:
        props = dict(feat.get("properties") or {})
        class_id = props.pop("class_id", None)
        if not class_id:
            continue
        session.add(
            Annotation(
                annotation_set_id=target_set.id,
                class_id=uuid.UUID(str(class_id)),
                geometry=parse_geometry(feat["geometry"]),
                confidence=confidence,
                properties=props or None,
                created_by_user_id=creator,
            )
        )
        class_counts[str(class_id)] = class_counts.get(str(class_id), 0) + 1

    if commit:
        session.commit()
    else:
        session.flush()
    logger.info(
        "raster_mask vectorized: source_set=%s target_set=%s features=%s classes=%s",
        raster_set.id, target_set.id, len(features), class_counts,
    )
    return RasterMaskVectorizeResult(
        annotation_set_id=target_set.id,
        feature_count=len(features),
        class_counts=class_counts,
    )
