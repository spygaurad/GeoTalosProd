"""Tests for annotation-set rasterization."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from sqlalchemy.sql.elements import BooleanClauseList

from app.services.conversion.rasterize_mask import (
    BACKGROUND_VALUE,
    DEFAULT_RESOLUTION_M,
    build_value_class_map,
    rasterize_annotation_sets_to_cog,
)

SET_ID = uuid.UUID("00000000-0000-0000-0000-000000000040")
SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000020")
CLASS_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
CLASS_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2")
CLASS_GHOST = uuid.UUID("cccccccc-cccc-cccc-cccc-ccccccccccc3")


def _square(minx, miny, maxx, maxy) -> str:
    return json.dumps(
        {
            "type": "Polygon",
            "coordinates": [[
                [minx, miny],
                [maxx, miny],
                [maxx, maxy],
                [minx, maxy],
                [minx, miny],
            ]],
        }
    )


def _has_live_filter(clause) -> bool:
    if clause is None:
        return False
    op_name = getattr(getattr(clause, "operator", None), "__name__", "")
    clauses = getattr(clause, "clauses", None)
    if isinstance(clause, BooleanClauseList) or (
        clauses is not None and op_name in ("and_", "or_")
    ):
        return any(_has_live_filter(part) for part in clause.clauses)
    left = getattr(clause.left, "key", None) or getattr(clause.left, "name", None)
    return left == "deleted_at" and op_name == "is_"


class _Result:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self, classes, shapes, schema_id=SCHEMA_ID):
        self.classes = list(classes)
        self.shapes = list(shapes)
        self.schema_id = schema_id
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        names = [col.get("name") for col in stmt.column_descriptions]
        live_only = _has_live_filter(stmt.whereclause)
        if "schema_id" in names:
            return _Result(scalar=self.schema_id)
        if "geojson" in names:
            shapes = self.shapes
            if live_only:
                shapes = [row for row in shapes if row["deleted_at"] is None]
            return _Result(rows=[(row["class_id"], row["geojson"]) for row in shapes])
        classes = self.classes
        if live_only:
            classes = [row for row in classes if row["deleted_at"] is None]
        return _Result(rows=[(row["id"], row["name"]) for row in classes])


def _class(class_id, name, *, deleted_at=None):
    return {"id": class_id, "name": name, "deleted_at": deleted_at}


def _shape(class_id, geojson, *, deleted_at=None):
    return {"class_id": class_id, "geojson": geojson, "deleted_at": deleted_at}


def _write_reference(path: Path, *, crs: str, res: float) -> str:
    data = np.zeros((2, 2), dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="uint8",
        count=1,
        height=2,
        width=2,
        crs=crs,
        transform=from_origin(0.0, 1.0, res, res),
    ) as dst:
        dst.write(data, 1)
    return str(path)


def _sample(path: Path, x: float, y: float) -> int:
    with rasterio.open(path) as src:
        row, col = src.index(x, y)
        return int(src.read(1)[row, col])


def test_build_value_class_map_is_stable_and_reserves_background():
    session = _Session(
        classes=[
            _class(CLASS_B, "Zebra"),
            _class(CLASS_A, "apple"),
            _class(CLASS_GHOST, "mango"),
        ],
        shapes=[],
    )

    schema_id, value_map, names = build_value_class_map(session, [SET_ID])
    again = build_value_class_map(session, [SET_ID])

    assert BACKGROUND_VALUE == 0
    assert "0" not in value_map
    assert schema_id == SCHEMA_ID
    assert value_map == {
        "1": str(CLASS_A),
        "2": str(CLASS_GHOST),
        "3": str(CLASS_B),
    }
    assert names[str(CLASS_A)] == "apple"
    assert again[1] == value_map


def test_build_value_class_map_breaks_name_ties_by_id():
    first = uuid.UUID("11111111-1111-1111-1111-111111111111")
    second = uuid.UUID("22222222-2222-2222-2222-222222222222")
    session = _Session(
        classes=[_class(second, "Tree"), _class(first, "Tree")],
        shapes=[],
    )

    _schema, value_map, _names = build_value_class_map(session, [SET_ID])

    assert value_map == {"1": str(first), "2": str(second)}


def test_class_filter_limits_the_value_map():
    session = _Session(
        classes=[_class(CLASS_A, "apple"), _class(CLASS_B, "Zebra")],
        shapes=[],
    )

    _schema, value_map, names = build_value_class_map(
        session, [SET_ID], class_filter={str(CLASS_B)}
    )

    assert value_map == {"1": str(CLASS_B)}
    assert names == {str(CLASS_B): "Zebra"}


def test_live_polygons_burn_uint8_with_background_zero(tmp_path: Path):
    ref = _write_reference(tmp_path / "ref.tif", crs="EPSG:4326", res=0.05)
    session = _Session(
        classes=[
            _class(CLASS_A, "apple"),
            _class(CLASS_B, "beta"),
            _class(CLASS_GHOST, "ghost", deleted_at=datetime.now(timezone.utc)),
        ],
        shapes=[
            _shape(CLASS_A, _square(0.0, 0.0, 0.4, 0.4)),
            _shape(CLASS_B, _square(0.2, 0.0, 0.6, 0.4)),
            _shape(
                CLASS_A,
                _square(1.0, 0.0, 1.2, 0.2),
                deleted_at=datetime.now(timezone.utc),
            ),
            _shape(
                CLASS_GHOST,
                _square(2.0, 0.0, 2.2, 0.2),
                deleted_at=datetime.now(timezone.utc),
            ),
        ],
    )
    out = tmp_path / "mask.tif"

    result = rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(out),
        gdal_env={},
        reference_item_uri=ref,
    )

    with rasterio.open(out) as src:
        data = src.read(1)
        assert src.count == 1
        assert src.dtypes[0] == "uint8"
        assert src.nodata == BACKGROUND_VALUE
        assert src.crs.to_string() == "EPSG:4326"
        assert src.res[0] == pytest.approx(0.05)
        assert src.res[1] == pytest.approx(0.05)

    assert result.nodata_value == BACKGROUND_VALUE
    assert result.band_index == 1
    assert result.value_class_map == {"1": str(CLASS_A), "2": str(CLASS_B)}
    assert str(CLASS_GHOST) not in result.value_class_map.values()
    assert result.feature_count == 2
    assert result.bounds[2] < 0.8
    assert _sample(out, 0.1, 0.2) == 1
    assert _sample(out, 0.3, 0.2) == 2
    assert _sample(out, 0.5, 0.2) == 2
    assert 0 in data
    assert not np.any(data == 3)
    assert any("deleted_at" in str(stmt) for stmt in session.statements)


def test_reference_resolution_is_reused_until_overridden(tmp_path: Path):
    ref = _write_reference(tmp_path / "utm.tif", crs="EPSG:32632", res=15.0)
    session = _Session(
        classes=[_class(CLASS_A, "apple")],
        shapes=[_shape(CLASS_A, _square(0.0, 0.0, 0.01, 0.01))],
    )

    native = rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(tmp_path / "native.tif"),
        gdal_env={},
        reference_item_uri=ref,
    )
    forced = rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(tmp_path / "forced.tif"),
        gdal_env={},
        reference_item_uri=ref,
        resolution_m=40.0,
    )

    assert native.crs == "EPSG:32632"
    assert forced.crs == "EPSG:32632"
    with rasterio.open(tmp_path / "native.tif") as src:
        assert src.crs.to_string() == "EPSG:32632"
        assert src.res[0] == pytest.approx(15.0)
        assert src.res[1] == pytest.approx(15.0)
    with rasterio.open(tmp_path / "forced.tif") as src:
        assert src.crs.to_string() == "EPSG:32632"
        assert src.res[0] == pytest.approx(40.0)
        assert src.res[1] == pytest.approx(40.0)


def test_no_reference_uses_web_mercator_and_default_resolution(tmp_path: Path):
    session = _Session(
        classes=[_class(CLASS_A, "apple")],
        shapes=[_shape(CLASS_A, _square(0.0, 0.0, 0.001, 0.001))],
    )

    default = rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(tmp_path / "default.tif"),
        gdal_env={},
    )
    explicit = rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(tmp_path / "explicit.tif"),
        gdal_env={},
        resolution_m=25.0,
    )

    assert default.crs == "EPSG:3857"
    assert explicit.crs == "EPSG:3857"
    with rasterio.open(tmp_path / "default.tif") as src:
        assert src.crs.to_string() == "EPSG:3857"
        assert src.res[0] == pytest.approx(DEFAULT_RESOLUTION_M)
        assert src.res[1] == pytest.approx(DEFAULT_RESOLUTION_M)
    with rasterio.open(tmp_path / "explicit.tif") as src:
        assert src.res[0] == pytest.approx(25.0)
        assert src.res[1] == pytest.approx(25.0)


def test_empty_set_list_raises():
    with pytest.raises(ValueError, match="at least one set"):
        rasterize_annotation_sets_to_cog(_Session([], []), [], "unused.tif", gdal_env={})


def test_no_live_annotations_raise():
    deleted = datetime.now(timezone.utc)
    session = _Session(
        classes=[_class(CLASS_A, "apple", deleted_at=deleted)],
        shapes=[_shape(CLASS_A, _square(0, 0, 1, 1), deleted_at=deleted)],
    )

    with pytest.raises(ValueError, match="No annotation classes"):
        rasterize_annotation_sets_to_cog(
            session, [SET_ID], "unused.tif", gdal_env={}
        )


def test_all_touched_burns_pixels_the_polygon_only_clips(tmp_path: Path):
    ref = _write_reference(tmp_path / "coarse.tif", crs="EPSG:4326", res=1.0)
    session = _Session(
        classes=[_class(CLASS_A, "apple")],
        shapes=[_shape(CLASS_A, _square(0.0, 0.0, 0.05, 0.05))],
    )

    center_path = tmp_path / "center.tif"
    touched_path = tmp_path / "touched.tif"
    rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(center_path),
        gdal_env={},
        reference_item_uri=ref,
        all_touched=False,
    )
    rasterize_annotation_sets_to_cog(
        session,
        [SET_ID],
        str(touched_path),
        gdal_env={},
        reference_item_uri=ref,
        all_touched=True,
    )

    with rasterio.open(center_path) as center, rasterio.open(touched_path) as touched:
        center_count = int(np.count_nonzero(center.read(1)))
        touched_count = int(np.count_nonzero(touched.read(1)))

    assert center_count == 0
    assert touched_count > center_count
