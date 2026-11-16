"""Tests for raster-mask vectorization."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.models.annotation import Annotation
from app.models.annotation_set import AnnotationSet
from app.models.dataset_item import DatasetItem
from app.services.conversion.raster_mask import (
    dissolve_features_by_class,
    raster_mask_to_features,
    vectorize_raster_mask_set,
)

_TRANSFORM = from_origin(10.0, 20.0, 0.5, 0.5)
_CLASS_A = "11111111-1111-1111-1111-111111111111"
_CLASS_B = "22222222-2222-2222-2222-222222222222"


def _write_mask(
    path: Path,
    data: np.ndarray,
    *,
    crs: str = "EPSG:4326",
    transform=_TRANSFORM,
    nodata=None,
) -> Path:
    if data.ndim != 2:
        raise ValueError("mask fixture must be 2D")
    height, width = data.shape
    profile = {
        "driver": "GTiff",
        "dtype": str(data.dtype),
        "count": 1,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def test_raster_mask_to_features_produces_expected_polygons(tmp_path: Path):
    data = np.zeros((4, 4), dtype=np.uint8)
    data[1:3, 1:3] = 1
    path = _write_mask(tmp_path / "block.tif", data)

    features = raster_mask_to_features(
        str(path), {}, value_class_map={"1": _CLASS_A}
    )

    assert len(features) == 1
    feat = features[0]
    assert feat["type"] == "Feature"
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["class_id"] == _CLASS_A
    assert feat["properties"]["raster_value"] == 1.0
    assert feat["properties"]["source"] == "raster_mask_vectorize"
    ring = feat["geometry"]["coordinates"][0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    assert min(xs) == pytest.approx(10.5)
    assert max(xs) == pytest.approx(11.5)
    assert min(ys) == pytest.approx(18.5)
    assert max(ys) == pytest.approx(19.5)


def test_mapped_raster_values_map_to_expected_class_ids(tmp_path: Path):
    data = np.array(
        [
            [0, 1, 1, 0],
            [0, 1, 2, 2],
            [0, 0, 2, 3],
            [0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    path = _write_mask(tmp_path / "classes.tif", data)

    features = raster_mask_to_features(
        str(path),
        {},
        value_class_map={"1": _CLASS_A, "2": _CLASS_B},
    )

    class_ids = sorted(f["properties"]["class_id"] for f in features)
    raster_values = sorted(f["properties"]["raster_value"] for f in features)
    assert class_ids == sorted([_CLASS_A, _CLASS_B])
    assert raster_values == [1.0, 2.0]
    assert all(f["properties"]["raster_value"] != 3.0 for f in features)


def test_nodata_pixels_are_excluded(tmp_path: Path):
    data = np.array(
        [
            [0, 0, 0, 0],
            [0, 1, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    path = _write_mask(tmp_path / "nodata.tif", data, nodata=0)

    features = raster_mask_to_features(
        str(path), {}, value_class_map={"0": _CLASS_B, "1": _CLASS_A}
    )

    assert len(features) == 1
    assert features[0]["properties"]["class_id"] == _CLASS_A
    assert features[0]["properties"]["raster_value"] == 1.0

    path_override = _write_mask(tmp_path / "nodata-override.tif", data, nodata=None)
    features_override = raster_mask_to_features(
        str(path_override),
        {},
        value_class_map={"1": _CLASS_A},
        nodata_value=1,
    )
    assert features_override == []


def test_connectivity_4_keeps_diagonal_components_separate(tmp_path: Path):
    data = np.zeros((3, 3), dtype=np.uint8)
    data[0, 0] = 1
    data[1, 1] = 1
    path = _write_mask(tmp_path / "diag.tif", data)

    features_4 = raster_mask_to_features(
        str(path), {}, value_class_map={"1": _CLASS_A}, connectivity=4
    )
    features_8 = raster_mask_to_features(
        str(path), {}, value_class_map={"1": _CLASS_A}, connectivity=8
    )

    assert len(features_4) == 2
    assert len(features_8) == 1


def test_min_area_px_drops_small_regions(tmp_path: Path):
    data = np.zeros((6, 6), dtype=np.uint8)
    data[0:3, 0:3] = 1  # 9 px
    data[5, 5] = 1  # 1 px
    path = _write_mask(tmp_path / "blobs.tif", data)

    features = raster_mask_to_features(
        str(path), {}, value_class_map={"1": _CLASS_A}, min_area_px=2
    )

    assert len(features) == 1
    assert features[0]["properties"]["class_id"] == _CLASS_A


def test_reprojection_to_epsg_4326_is_preserved(tmp_path: Path):
    transform = from_origin(500000.0, 4000000.0, 10.0, 10.0)
    data = np.zeros((4, 4), dtype=np.uint8)
    data[1:3, 1:3] = 1
    path = _write_mask(
        tmp_path / "utm.tif", data, crs="EPSG:32632", transform=transform
    )

    features = raster_mask_to_features(
        str(path), {}, value_class_map={"1": _CLASS_A}
    )

    assert len(features) == 1
    ring = features[0]["geometry"]["coordinates"][0]
    assert max(abs(p[0]) for p in ring) < 180
    assert max(abs(p[1]) for p in ring) <= 90
    assert all(abs(p[0]) < 1000 for p in ring)


def test_empty_value_class_map_returns_no_features(tmp_path: Path):
    path = _write_mask(
        tmp_path / "empty-map.tif",
        np.ones((2, 2), dtype=np.uint8),
    )

    assert raster_mask_to_features(str(path), {}, value_class_map={}) == []


def test_invalid_connectivity_raises(tmp_path: Path):
    path = _write_mask(
        tmp_path / "conn.tif",
        np.ones((2, 2), dtype=np.uint8),
    )

    with pytest.raises(ValueError, match="connectivity must be 4 or 8"):
        raster_mask_to_features(
            str(path), {}, value_class_map={"1": _CLASS_A}, connectivity=6
        )


def test_dissolve_features_by_class_merges_components():
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
            },
            "properties": {"class_id": _CLASS_A, "source": "raster_mask_vectorize"},
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 2]]],
            },
            "properties": {"class_id": _CLASS_A, "source": "raster_mask_vectorize"},
        },
    ]

    dissolved = dissolve_features_by_class(features)

    assert len(dissolved) == 1
    assert dissolved[0]["properties"]["class_id"] == _CLASS_A
    assert dissolved[0]["properties"]["dissolved"] is True
    assert dissolved[0]["properties"]["source"] == "raster_mask_vectorize"


def _vectorize_session(item: SimpleNamespace):
    session = MagicMock()

    def _get(model, ident, *_args, **_kwargs):
        if model is DatasetItem and ident == item.id:
            return item
        return None

    def _flush():
        for call in session.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, AnnotationSet) and getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    session.get.side_effect = _get
    session.flush.side_effect = _flush
    executed = MagicMock()
    executed.scalars.return_value.first.return_value = None
    executed.scalar_one_or_none.return_value = "Schema"
    session.execute.return_value = executed
    return session


def test_vectorize_raster_mask_set_creates_import_annotations(tmp_path: Path):
    data = np.zeros((4, 4), dtype=np.uint8)
    data[1:3, 1:3] = 1
    path = _write_mask(tmp_path / "mask-set.tif", data, nodata=0)

    org_id = uuid.uuid4()
    schema_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    class_a = uuid.UUID(_CLASS_A)

    item = SimpleNamespace(id=item_id, s3_uri=str(path))
    raster_set = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=org_id,
        schema_id=schema_id,
        dataset_id=dataset_id,
        dataset_item_id=item_id,
        name="source-mask",
        created_by_user_id=user_id,
        raster_config={
            "dataset_item_id": str(item_id),
            "value_class_map": {"1": str(class_a)},
            "band_index": 1,
            "nodata_value": 0,
        },
    )
    source_snapshot = (
        raster_set.id,
        raster_set.name,
        dict(raster_set.raster_config),
        raster_set.organization_id,
        raster_set.schema_id,
    )
    session = _vectorize_session(item)

    result = vectorize_raster_mask_set(
        session,
        raster_set,
        gdal_env={},
        commit=False,
    )

    added = [call.args[0] for call in session.add.call_args_list]
    sets = [obj for obj in added if isinstance(obj, AnnotationSet)]
    annotations = [obj for obj in added if isinstance(obj, Annotation)]

    assert len(sets) == 1
    assert sets[0].source_type == "import"
    assert sets[0].organization_id == org_id
    assert sets[0].schema_id == schema_id
    assert sets[0].dataset_id == dataset_id
    assert sets[0].dataset_item_id == item_id
    assert sets[0].name == "source-mask · vectorized"
    assert sets[0].created_by_user_id == user_id
    assert sets[0].raster_config is None or not getattr(sets[0], "raster_config", None)

    assert len(annotations) == 1
    assert annotations[0].class_id == class_a
    assert annotations[0].annotation_set_id == sets[0].id
    assert annotations[0].created_by_user_id == user_id
    assert annotations[0].confidence == 1.0

    assert result.annotation_set_id == sets[0].id
    assert result.feature_count == 1
    assert result.class_counts == {str(class_a): 1}
    assert (
        raster_set.id,
        raster_set.name,
        dict(raster_set.raster_config),
        raster_set.organization_id,
        raster_set.schema_id,
    ) == source_snapshot
    assert all(call.args[0] is not raster_set for call in session.add.call_args_list)
    session.commit.assert_not_called()
    session.flush.assert_called()


def test_vectorize_raster_mask_set_source_type_remains_import(tmp_path: Path):
    path = _write_mask(
        tmp_path / "src.tif",
        np.array([[1, 1], [1, 1]], dtype=np.uint8),
    )
    item_id = uuid.uuid4()
    item = SimpleNamespace(id=item_id, s3_uri=str(path))
    raster_set = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        schema_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_item_id=item_id,
        name="mask",
        created_by_user_id=uuid.uuid4(),
        raster_config={
            "dataset_item_id": str(item_id),
            "value_class_map": {"1": _CLASS_A},
        },
    )
    session = _vectorize_session(item)

    vectorize_raster_mask_set(session, raster_set, gdal_env={}, commit=False)

    sets = [
        call.args[0]
        for call in session.add.call_args_list
        if isinstance(call.args[0], AnnotationSet)
    ]
    assert len(sets) == 1
    assert sets[0].source_type == "import"
