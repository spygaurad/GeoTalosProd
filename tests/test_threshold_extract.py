"""Unit tests for band threshold → uint8 mask and annotation persistence."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.models.annotation import Annotation
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.services.conversion.raster_mask import raster_mask_to_features
from app.services.conversion.threshold_extract import (
    compute_ndvi,
    persist_threshold_features,
    threshold_band_to_mask,
    threshold_raster_to_features,
    threshold_raster_to_mask,
)


def test_threshold_min_only_selects_values_at_or_above_min():
    values = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.float32)

    mask = threshold_band_to_mask(values, 3)

    assert mask.dtype == np.uint8
    assert mask.tolist() == [[0, 0, 0], [1, 1, 1]]


def test_threshold_min_and_max_select_inclusive_range():
    values = np.array([[0, 2, 5], [8, 10, 12]], dtype=np.float32)

    mask = threshold_band_to_mask(values, 2, 10)

    assert mask.dtype == np.uint8
    assert mask.tolist() == [[0, 1, 1], [1, 1, 0]]


def test_nodata_pixels_are_ignored():
    values = np.array([[1, -9999, 4], [9, 2, -9999]], dtype=np.float32)

    mask = threshold_band_to_mask(values, 1, nodata=-9999)

    assert mask.tolist() == [[1, 0, 1], [1, 1, 0]]


def test_all_pixels_rejected_when_none_meet_threshold():
    values = np.array([[1, 2], [3, 4]], dtype=np.uint8)

    mask = threshold_band_to_mask(values, 10)

    assert mask.dtype == np.uint8
    assert mask.tolist() == [[0, 0], [0, 0]]


def test_all_pixels_selected_when_every_value_passes():
    values = np.array([[5, 6], [7, 8]], dtype=np.int16)

    mask = threshold_band_to_mask(values, 5)

    assert mask.dtype == np.uint8
    assert mask.tolist() == [[1, 1], [1, 1]]


_TRANSFORM = from_origin(10.0, 20.0, 0.5, 0.5)


def _write_small_raster(
    path: Path,
    data: np.ndarray,
    *,
    crs: str = "EPSG:4326",
    transform=_TRANSFORM,
    nodata=None,
) -> Path:
    """Write a tiny GeoTIFF. ``data`` is 2D (one band) or 3D (band, row, col)."""
    if data.ndim == 2:
        bands = data[np.newaxis, ...]
    elif data.ndim == 3:
        bands = data
    else:
        raise ValueError("fixture data must be 2D or 3D")
    count, height, width = bands.shape
    profile = {
        "driver": "GTiff",
        "dtype": str(bands.dtype),
        "count": count,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(bands)
    return path


def test_raster_band_threshold_preserves_grid_and_leaves_source_unchanged(tmp_path: Path):
    path = _write_small_raster(
        tmp_path / "band.tif",
        np.array([[0, 2, 5], [8, 1, 9]], dtype=np.uint8),
    )
    before = path.read_bytes()

    result = threshold_raster_to_mask(str(path), {}, threshold_min=2, threshold_max=8)

    assert path.read_bytes() == before
    assert result.mask.dtype == np.uint8
    assert result.mask.tolist() == [[0, 1, 1], [1, 0, 0]]
    assert result.transform == _TRANSFORM
    assert result.src_crs is not None
    assert result.src_crs.to_epsg() == 4326
    assert result.nodata is None
    with rasterio.open(path) as src:
        np.testing.assert_array_equal(src.read(1), np.array([[0, 2, 5], [8, 1, 9]], dtype=np.uint8))


def test_raster_band_threshold_accepts_dataset_item_and_file_nodata(tmp_path: Path):
    data = np.array([[1.0, -9999.0], [4.0, 0.0]], dtype=np.float32)
    path = _write_small_raster(tmp_path / "item.tif", data, nodata=-9999)
    item = SimpleNamespace(s3_uri=str(path))

    result = threshold_raster_to_mask(item, {}, threshold_min=-10000)

    assert result.nodata == -9999.0
    assert result.mask.tolist() == [[1, 0], [1, 1]]
    assert result.src_crs.to_epsg() == 4326


def test_raster_band_index_selects_requested_band(tmp_path: Path):
    bands = np.array(
        [
            [[0, 0], [0, 0]],
            [[1, 9], [4, 2]],
        ],
        dtype=np.uint8,
    )
    path = _write_small_raster(tmp_path / "two.tif", bands)

    result = threshold_raster_to_mask(str(path), {}, threshold_min=3, band_index=2)

    assert result.mask.tolist() == [[0, 1], [1, 0]]


def test_raster_band_index_out_of_range(tmp_path: Path):
    path = _write_small_raster(
        tmp_path / "one.tif",
        np.array([[1, 2], [3, 4]], dtype=np.uint8),
    )

    with pytest.raises(ValueError, match=r"band_index 2 out of range \(raster has 1 bands\)"):
        threshold_raster_to_mask(str(path), {}, threshold_min=0, band_index=2)


def test_connected_block_becomes_one_polygon(tmp_path: Path):
    data = np.zeros((4, 4), dtype=np.uint8)
    data[1:3, 1:3] = 9
    path = _write_small_raster(tmp_path / "block.tif", data)

    features = threshold_raster_to_features(
        str(path), {}, threshold_min=1, output_class_id="class-a"
    )

    assert len(features) == 1
    assert features[0]["geometry"]["type"] == "Polygon"
    assert features[0]["properties"]["class_id"] == "class-a"
    assert features[0]["properties"]["raster_value"] == 1.0
    assert features[0]["properties"]["source"] == "threshold_extract"


def test_min_area_px_drops_small_blobs(tmp_path: Path):
    data = np.zeros((6, 6), dtype=np.uint8)
    data[0:3, 0:3] = 5
    data[5, 5] = 5
    path = _write_small_raster(tmp_path / "blobs.tif", data)

    features = threshold_raster_to_features(
        str(path), {}, threshold_min=1, output_class_id="class-a", min_area_px=2
    )

    assert len(features) == 1


def test_empty_mask_returns_no_features(tmp_path: Path):
    path = _write_small_raster(
        tmp_path / "empty.tif",
        np.zeros((3, 3), dtype=np.uint8),
    )

    features = threshold_raster_to_features(
        str(path), {}, threshold_min=1, output_class_id="class-a"
    )

    assert features == []


def test_reproject_matches_raster_mask_features(tmp_path: Path):
    transform = from_origin(500000.0, 4000000.0, 10.0, 10.0)
    data = np.zeros((4, 4), dtype=np.uint8)
    data[1:3, 1:3] = 8
    path = _write_small_raster(
        tmp_path / "utm.tif", data, crs="EPSG:32632", transform=transform
    )
    features = threshold_raster_to_features(
        str(path), {}, threshold_min=1, output_class_id="class-a"
    )

    mask_path = _write_small_raster(
        tmp_path / "utm-mask.tif",
        np.array(
            [
                [0, 0, 0, 0],
                [0, 1, 1, 0],
                [0, 1, 1, 0],
                [0, 0, 0, 0],
            ],
            dtype=np.uint8,
        ),
        crs="EPSG:32632",
        transform=transform,
    )
    legacy = raster_mask_to_features(
        str(mask_path),
        {},
        value_class_map={"1": "class-a"},
    )

    assert len(features) == 1
    assert len(legacy) == 1
    assert features[0]["geometry"] == legacy[0]["geometry"]
    assert features[0]["properties"]["class_id"] == legacy[0]["properties"]["class_id"]
    ring = features[0]["geometry"]["coordinates"][0]
    assert max(abs(point[0]) for point in ring) < 180


def test_ndvi_matches_formula_and_reuses_threshold():
    red = np.array([[1, 4], [2, 1]], dtype=np.float32)
    nir = np.array([[3, 1], [2, 3]], dtype=np.float32)

    ndvi = compute_ndvi(red, nir)

    expected = (nir.astype(np.float64) - red) / (nir.astype(np.float64) + red)
    np.testing.assert_allclose(ndvi, expected)
    mask = threshold_band_to_mask(ndvi, 0.3)
    assert mask.dtype == np.uint8
    assert mask.tolist() == [[1, 0], [0, 1]]


def test_ndvi_divide_by_zero_stays_background():
    red = np.array([[1.0, 2.0]], dtype=np.float64)
    nir = np.array([[-1.0, 2.0]], dtype=np.float64)

    ndvi = compute_ndvi(red, nir)

    assert np.isnan(ndvi[0, 0])
    assert ndvi[0, 1] == 0.0
    mask = threshold_band_to_mask(ndvi, -1.0, 1.0)
    assert mask.tolist() == [[0, 1]]


def test_ndvi_nodata_on_either_band_is_background():
    red = np.array([[1.0, -9999.0, 1.0], [1.0, 1.0, 1.0]], dtype=np.float32)
    nir = np.array([[3.0, 5.0, -9999.0], [3.0, 1.0, 3.0]], dtype=np.float32)

    ndvi = compute_ndvi(red, nir, red_nodata=-9999, nir_nodata=-9999)

    assert np.isnan(ndvi[0, 1])
    assert np.isnan(ndvi[0, 2])
    mask = threshold_band_to_mask(ndvi, 0.2)
    assert mask.tolist() == [[1, 0, 0], [1, 0, 1]]


def test_ndvi_raster_uses_configured_bands_and_file_nodata(tmp_path: Path):
    unused = np.full((2, 2), 100, dtype=np.float32)
    red = np.array([[1.0, -9999.0], [1.0, 1.0]], dtype=np.float32)
    nir = np.array([[3.0, 5.0], [-9999.0, 3.0]], dtype=np.float32)
    path = _write_small_raster(
        tmp_path / "ndvi.tif",
        np.stack([unused, red, nir]),
        nodata=-9999,
    )
    before = path.read_bytes()

    result = threshold_raster_to_mask(
        str(path),
        {},
        threshold_min=0.2,
        index="ndvi",
        band_red=2,
        band_nir=3,
    )

    assert path.read_bytes() == before
    assert result.mask.tolist() == [[1, 0], [0, 1]]
    assert result.transform == _TRANSFORM
    with rasterio.open(path) as src:
        np.testing.assert_array_equal(src.read(2), red)
        np.testing.assert_array_equal(src.read(3), nir)


def test_ndvi_requires_red_and_nir_bands(tmp_path: Path):
    path = _write_small_raster(
        tmp_path / "two.tif",
        np.zeros((2, 2, 2), dtype=np.float32),
    )

    with pytest.raises(ValueError, match="band_red and band_nir are required"):
        threshold_raster_to_mask(str(path), {}, threshold_min=0.2, index="ndvi")
    with pytest.raises(ValueError, match=r"band_nir 3 out of range \(raster has 2 bands\)"):
        threshold_raster_to_mask(
            str(path),
            {},
            threshold_min=0.2,
            index="ndvi",
            band_red=1,
            band_nir=3,
        )


def test_ndvi_polygon_reuses_polygonizer_and_annotation_writer(tmp_path: Path):
    red = np.ones((4, 4), dtype=np.float32)
    nir = np.ones((4, 4), dtype=np.float32)
    nir[1:3, 1:3] = 3
    path = _write_small_raster(tmp_path / "ndvi-block.tif", np.stack([red, nir]))

    features = threshold_raster_to_features(
        str(path),
        {},
        threshold_min=0.2,
        output_class_id="class-a",
        index="ndvi",
        band_red=1,
        band_nir=2,
    )

    assert len(features) == 1
    assert features[0]["geometry"]["type"] == "Polygon"
    assert features[0]["properties"]["class_id"] == "class-a"
    assert features[0]["properties"]["source"] == "threshold_extract"

    item = _imagery_item()
    schema = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=item.organization_id,
        deleted_at=None,
    )
    ann_class = SimpleNamespace(id=uuid.uuid4(), schema_id=schema.id)
    session = _persist_session(schema, ann_class)
    result = persist_threshold_features(
        session,
        features,
        item,
        schema_id=schema.id,
        output_class_id=ann_class.id,
        created_by_user_id=uuid.uuid4(),
        commit=False,
    )

    added = [call.args[0] for call in session.add.call_args_list]
    annotations = [obj for obj in added if isinstance(obj, Annotation)]
    assert result.feature_count == 1
    assert len(annotations) == 1
    assert annotations[0].class_id == ann_class.id


def _polygon_feature() -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
        },
        "properties": {"raster_value": 1.0, "source": "threshold_extract"},
    }


def _persist_session(schema, ann_class):
    """Sync session double that satisfies collection sync without a database."""
    session = MagicMock()

    def _get(model, ident, *_args, **_kwargs):
        if model is AnnotationSchema and ident == schema.id:
            return schema
        if model is AnnotationClass and ident == ann_class.id:
            return ann_class
        return None

    def _flush():
        for call in session.add.call_args_list:
            obj = call.args[0]
            if getattr(obj, "id", None) is None and hasattr(obj, "name"):
                obj.id = uuid.uuid4()

    session.get.side_effect = _get
    session.flush.side_effect = _flush
    executed = MagicMock()
    executed.scalars.return_value.first.return_value = None
    executed.scalar_one_or_none.return_value = "Schema"
    session.execute.return_value = executed
    return session


def _imagery_item():
    return SimpleNamespace(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        s3_uri="s3://bucket/scene.tif",
        filename="scene.tif",
    )


def test_persist_creates_analysis_set_linked_to_imagery():
    item = _imagery_item()
    before = (item.id, item.dataset_id, item.organization_id, item.s3_uri, item.filename)
    schema = SimpleNamespace(id=uuid.uuid4(), organization_id=item.organization_id, deleted_at=None)
    ann_class = SimpleNamespace(id=uuid.uuid4(), schema_id=schema.id)
    user_id = uuid.uuid4()
    session = _persist_session(schema, ann_class)

    result = persist_threshold_features(
        session,
        [_polygon_feature()],
        item,
        schema_id=schema.id,
        output_class_id=ann_class.id,
        created_by_user_id=user_id,
        commit=False,
    )

    added = [call.args[0] for call in session.add.call_args_list]
    sets = [obj for obj in added if isinstance(obj, AnnotationSet)]
    annotations = [obj for obj in added if isinstance(obj, Annotation)]
    assert len(sets) == 1
    assert sets[0].source_type == "analysis"
    assert sets[0].raster_config is None
    assert sets[0].schema_id == schema.id
    assert sets[0].organization_id == item.organization_id
    assert sets[0].dataset_id == item.dataset_id
    assert sets[0].dataset_item_id == item.id
    assert len(annotations) == 1
    assert annotations[0].class_id == ann_class.id
    assert annotations[0].annotation_set_id == sets[0].id
    assert result.feature_count == 1
    assert result.dataset_id == item.dataset_id
    assert result.dataset_item_id == item.id
    assert (item.id, item.dataset_id, item.organization_id, item.s3_uri, item.filename) == before
    assert all(call.args[0] is not item for call in session.add.call_args_list)


def test_persist_zero_features_creates_set_without_annotations():
    item = _imagery_item()
    schema = SimpleNamespace(id=uuid.uuid4(), organization_id=item.organization_id, deleted_at=None)
    ann_class = SimpleNamespace(id=uuid.uuid4(), schema_id=schema.id)
    session = _persist_session(schema, ann_class)

    result = persist_threshold_features(
        session,
        [],
        item,
        schema_id=schema.id,
        output_class_id=ann_class.id,
        created_by_user_id=uuid.uuid4(),
        commit=False,
    )

    added = [call.args[0] for call in session.add.call_args_list]
    sets = [obj for obj in added if isinstance(obj, AnnotationSet)]
    annotations = [obj for obj in added if isinstance(obj, Annotation)]
    assert len(sets) == 1
    assert annotations == []
    assert result.feature_count == 0
    assert result.annotation_set_id == sets[0].id
