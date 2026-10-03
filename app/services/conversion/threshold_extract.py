"""Threshold imagery into a uint8 mask, then polygons and an AnnotationSet."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any


def threshold_band_to_mask(
    values,
    threshold_min: float,
    threshold_max: float | None = None,
    *,
    nodata: float | None = None,
    index: str = "band",
):
    """Return a uint8 mask (1 = selected, 0 = background) for a 2D band."""
    import numpy as np

    if index != "band":
        raise ValueError("only index='band' is supported")
    if threshold_min is None:
        raise ValueError("threshold_min is required")
    try:
        minimum = float(threshold_min)
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold_min must be a number") from exc

    maximum: float | None = None
    if threshold_max is not None:
        try:
            maximum = float(threshold_max)
        except (TypeError, ValueError) as exc:
            raise ValueError("threshold_max must be a number") from exc
        if maximum < minimum:
            raise ValueError("threshold_max must be greater than or equal to threshold_min")

    if np.ma.isMaskedArray(values):
        invalid = np.ma.getmaskarray(values)
        data = np.ma.getdata(values)
    else:
        data = np.asarray(values)
        invalid = np.zeros(data.shape, dtype=bool)

    if data.ndim != 2:
        raise ValueError("values must be a 2D array")
    if not np.issubdtype(data.dtype, np.number):
        raise ValueError("values must be numeric")

    if np.issubdtype(data.dtype, np.floating):
        invalid = invalid | np.isnan(data)
    if nodata is not None:
        nodata_value = float(nodata)
        if math.isnan(nodata_value):
            if np.issubdtype(data.dtype, np.floating):
                invalid = invalid | np.isnan(data)
        else:
            invalid = invalid | (data == nodata_value)

    selected = data >= minimum
    if maximum is not None:
        selected = selected & (data <= maximum)
    selected = selected & ~invalid
    return selected.astype(np.uint8)


@dataclass(frozen=True)
class ThresholdMaskResult:
    mask: Any
    transform: Any
    src_crs: Any
    nodata: float | None = None


def compute_ndvi(red, nir, *, red_nodata: float | None = None, nir_nodata: float | None = None):
    """Return NDVI ``(nir - red) / (nir + red)`` as float64 (NaN where invalid)."""
    import numpy as np

    red_data, red_invalid = _index_band(red, red_nodata)
    nir_data, nir_invalid = _index_band(nir, nir_nodata)
    if red_data.shape != nir_data.shape:
        raise ValueError("red and nir bands must have the same shape")

    invalid = red_invalid | nir_invalid
    denominator = red_data + nir_data
    invalid = invalid | (denominator == 0)
    ndvi = np.full(red_data.shape, np.nan, dtype=np.float64)
    np.divide(nir_data - red_data, denominator, out=ndvi, where=~invalid)
    return ndvi


def _index_band(values, nodata: float | None):
    import numpy as np

    if np.ma.isMaskedArray(values):
        invalid = np.array(np.ma.getmaskarray(values), dtype=bool, copy=True)
        data = np.asarray(np.ma.getdata(values), dtype=np.float64)
    else:
        data = np.asarray(values, dtype=np.float64)
        invalid = np.zeros(data.shape, dtype=bool)
    if data.ndim != 2:
        raise ValueError("NDVI bands must be 2D arrays")
    invalid = invalid | ~np.isfinite(data)
    if nodata is not None:
        nodata_value = float(nodata)
        if not math.isnan(nodata_value):
            invalid = invalid | (data == nodata_value)
    return data, invalid


def _band_in_range(band_number: int, count: int, label: str) -> int:
    if isinstance(band_number, bool) or not isinstance(band_number, int) or band_number < 1:
        raise ValueError(f"{label} must be an integer >= 1")
    if band_number > count:
        raise ValueError(f"{label} {band_number} out of range (raster has {count} bands)")
    return band_number


def _raster_uri(source: str | Any) -> str:
    if isinstance(source, str):
        uri = source.strip()
        if not uri:
            raise ValueError("raster URI is required")
        return uri
    uri = getattr(source, "s3_uri", None)
    if isinstance(uri, str) and uri.strip():
        return uri.strip()
    raise ValueError("source must be a raster URI or an imagery DatasetItem with s3_uri")


def threshold_raster_to_mask(
    source: str | Any,
    gdal_env: dict,
    *,
    threshold_min: float,
    threshold_max: float | None = None,
    band_index: int = 1,
    nodata_value: float | None = None,
    index: str = "band",
    band_red: int | None = None,
    band_nir: int | None = None,
) -> ThresholdMaskResult:
    """Threshold one band (or NDVI) from a raster into a uint8 mask."""
    import rasterio
    from rasterio.env import Env

    from app.workers.ingestion.rasterio_utils import _vsi_path

    if index not in ("band", "ndvi"):
        raise ValueError("only index='band' or index='ndvi' is supported")
    if index == "ndvi" and (band_red is None or band_nir is None):
        raise ValueError("band_red and band_nir are required for index='ndvi'")

    uri = _raster_uri(source)
    vsi = _vsi_path(uri)
    with Env(**gdal_env):
        with rasterio.open(vsi, "r") as src:
            transform = src.transform
            src_crs = src.crs
            if index == "band":
                if band_index < 1 or band_index > src.count:
                    raise ValueError(
                        f"band_index {band_index} out of range (raster has {src.count} bands)"
                    )
                band = src.read(band_index)
                file_nodata = src.nodata
            else:
                red_index = _band_in_range(band_red, src.count, "band_red")
                nir_index = _band_in_range(band_nir, src.count, "band_nir")
                red = src.read(red_index, masked=True)
                nir = src.read(nir_index, masked=True)

    if index == "band":
        nodata = nodata_value if nodata_value is not None else file_nodata
        if nodata is not None:
            nodata = float(nodata)
        mask = threshold_band_to_mask(
            band,
            threshold_min,
            threshold_max,
            nodata=nodata,
            index="band",
        )
        return ThresholdMaskResult(
            mask=mask,
            transform=transform,
            src_crs=src_crs,
            nodata=nodata,
        )

    values = compute_ndvi(red, nir, red_nodata=nodata_value, nir_nodata=nodata_value)
    mask = threshold_band_to_mask(values, threshold_min, threshold_max)
    return ThresholdMaskResult(
        mask=mask,
        transform=transform,
        src_crs=src_crs,
        nodata=None,
    )


def threshold_raster_to_features(
    source: str | Any,
    gdal_env: dict,
    *,
    threshold_min: float,
    output_class_id: str,
    threshold_max: float | None = None,
    band_index: int = 1,
    nodata_value: float | None = None,
    simplify_tolerance: float | None = None,
    min_area_px: float = 0.0,
    connectivity: int = 4,
    index: str = "band",
    band_red: int | None = None,
    band_nir: int | None = None,
) -> list[dict]:
    """Threshold a band or NDVI and polygonize selected pixels to EPSG:4326."""
    from app.services.conversion.polygonize import mask_array_to_features

    class_id = str(output_class_id).strip()
    if not class_id:
        raise ValueError("output_class_id is required")

    extracted = threshold_raster_to_mask(
        source,
        gdal_env,
        threshold_min=threshold_min,
        threshold_max=threshold_max,
        band_index=band_index,
        nodata_value=nodata_value,
        index=index,
        band_red=band_red,
        band_nir=band_nir,
    )
    return mask_array_to_features(
        extracted.mask,
        extracted.transform,
        extracted.src_crs,
        value_class_map={"1": class_id},
        nodata=0,
        simplify_tolerance=simplify_tolerance,
        min_area_px=min_area_px,
        connectivity=connectivity,
        source_tag="threshold_extract",
    )


@dataclass(frozen=True)
class ThresholdPersistResult:
    annotation_set_id: uuid.UUID
    feature_count: int
    dataset_id: uuid.UUID
    dataset_item_id: uuid.UUID


def persist_threshold_features(
    session,
    features: list[dict],
    dataset_item,
    *,
    schema_id: uuid.UUID,
    output_class_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    name: str | None = None,
    confidence: float | None = 1.0,
    commit: bool = True,
) -> ThresholdPersistResult:
    """Write threshold polygons into a new analysis AnnotationSet."""
    from app.core.geometry import parse_geometry
    from app.models.annotation import Annotation
    from app.models.annotation_class import AnnotationClass
    from app.models.annotation_schema import AnnotationSchema
    from app.models.annotation_set import AnnotationSet
    from app.services.annotation_set_grouping import ensure_schema_collection_sync

    if dataset_item is None:
        raise ValueError("dataset_item is required")
    if schema_id is None:
        raise ValueError("schema_id is required")
    if output_class_id is None:
        raise ValueError("output_class_id is required")
    if created_by_user_id is None:
        raise ValueError("created_by_user_id is required")

    item_org = getattr(dataset_item, "organization_id", None)
    item_dataset_id = getattr(dataset_item, "dataset_id", None)
    item_id = getattr(dataset_item, "id", None)
    if item_org is None or item_dataset_id is None or item_id is None:
        raise ValueError("dataset_item is missing organization, dataset, or id")

    schema_uuid = uuid.UUID(str(schema_id))
    class_uuid = uuid.UUID(str(output_class_id))
    creator = uuid.UUID(str(created_by_user_id))

    schema = session.get(AnnotationSchema, schema_uuid)
    if schema is None or getattr(schema, "deleted_at", None) is not None:
        raise ValueError("schema_id does not resolve to an annotation schema")
    if schema.organization_id != item_org:
        raise ValueError("schema does not belong to the imagery organization")

    ann_class = session.get(AnnotationClass, class_uuid)
    if ann_class is None or ann_class.schema_id != schema_uuid:
        raise ValueError("output_class_id does not belong to the schema")

    filename = getattr(dataset_item, "filename", None) or "imagery"
    target_set = AnnotationSet(
        organization_id=item_org,
        schema_id=schema_uuid,
        dataset_id=item_dataset_id,
        dataset_item_id=item_id,
        source_type="analysis",
        name=name or f"{filename} · threshold",
        description=f"Threshold extract from dataset item {item_id}",
        created_by_user_id=creator,
    )
    session.add(target_set)
    session.flush()

    ensure_schema_collection_sync(session, target_set)

    written = 0
    for feat in features or []:
        geometry = parse_geometry((feat or {}).get("geometry"))
        if geometry is None:
            continue
        props = dict((feat or {}).get("properties") or {})
        props.pop("class_id", None)
        session.add(
            Annotation(
                annotation_set_id=target_set.id,
                class_id=class_uuid,
                geometry=geometry,
                confidence=confidence,
                properties=props or None,
                created_by_user_id=creator,
            )
        )
        written += 1

    if commit:
        session.commit()
    else:
        session.flush()

    return ThresholdPersistResult(
        annotation_set_id=target_set.id,
        feature_count=written,
        dataset_id=item_dataset_id,
        dataset_item_id=item_id,
    )
