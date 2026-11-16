"""Unit tests for ``app.services.conversion.cog.convert_geotiff_to_cog``."""

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.services.conversion import convert_geotiff_to_cog
from app.services.conversion.cog import convert_geotiff_to_cog as convert_direct

_TRANSFORM = from_origin(-10.0, 10.0, 1.0, 1.0)
_CRS = "EPSG:4326"


def _write_geotiff(
    path: Path,
    data: np.ndarray,
    *,
    crs=_CRS,
    transform=_TRANSFORM,
    nodata=None,
    tiled: bool = False,
) -> Path:
    """Write a small (optionally non-tiled) GeoTIFF fixture."""
    if data.ndim == 2:
        count = 1
        height, width = data.shape
        bands = data[np.newaxis, ...]
    elif data.ndim == 3:
        count, height, width = data.shape
        bands = data
    else:
        raise ValueError("fixture data must be 2D or 3D")

    profile = {
        "driver": "GTiff",
        "dtype": str(data.dtype),
        "count": count,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": transform,
        "tiled": tiled,
    }
    if nodata is not None:
        profile["nodata"] = nodata

    with rasterio.open(path, "w", **profile) as dst:
        dst.write(bands)
    return path


def test_valid_geotiff_converts_and_preserves_grid(tmp_path: Path):
    src_path = tmp_path / "src.tif"
    dst_path = tmp_path / "out.tif"
    data = np.arange(20, dtype=np.uint8).reshape(4, 5)
    _write_geotiff(src_path, data)

    result = convert_geotiff_to_cog(str(src_path), str(dst_path))

    assert result == str(dst_path)
    assert dst_path.is_file()
    assert dst_path.stat().st_size > 0

    with rasterio.open(dst_path) as dst:
        assert dst.crs is not None
        assert dst.crs.to_string() == "EPSG:4326"
        assert dst.width == 5
        assert dst.height == 4
        assert dst.count == 1
        assert dst.dtypes[0] == "uint8"
        np.testing.assert_array_equal(dst.read(1), data)
        assert dst.profile.get("driver") == "GTiff" or dst.driver == "GTiff"


def test_package_export_is_the_same_function():
    assert convert_geotiff_to_cog is convert_direct


def test_nodata_is_preserved(tmp_path: Path):
    src_path = tmp_path / "nodata.tif"
    dst_path = tmp_path / "nodata_cog.tif"
    data = np.array([[1, 2], [255, 4]], dtype=np.uint8)
    _write_geotiff(src_path, data, nodata=255)

    convert_geotiff_to_cog(str(src_path), str(dst_path))

    with rasterio.open(dst_path) as dst:
        assert dst.nodata == 255
        np.testing.assert_array_equal(dst.read(1), data)


def test_multiband_geotiff_converts(tmp_path: Path):
    src_path = tmp_path / "rgb.tif"
    dst_path = tmp_path / "rgb_cog.tif"
    data = np.stack(
        [
            np.full((3, 3), 10, dtype=np.uint16),
            np.full((3, 3), 20, dtype=np.uint16),
            np.full((3, 3), 30, dtype=np.uint16),
        ],
        axis=0,
    )
    _write_geotiff(src_path, data, nodata=0)

    convert_geotiff_to_cog(str(src_path), str(dst_path))

    with rasterio.open(dst_path) as dst:
        assert dst.count == 3
        assert dst.width == 3
        assert dst.height == 3
        assert dst.crs.to_string() == "EPSG:4326"
        np.testing.assert_array_equal(dst.read(), data)
        assert dst.nodata == 0


def test_missing_crs_raises_value_error(tmp_path: Path):
    src_path = tmp_path / "no_crs.tif"
    dst_path = tmp_path / "out.tif"
    data = np.ones((2, 2), dtype=np.uint8)
    _write_geotiff(src_path, data, crs=None)

    with pytest.raises(ValueError, match="no coordinate reference system"):
        convert_geotiff_to_cog(str(src_path), str(dst_path))

    assert not dst_path.exists()


def test_corrupt_input_raises_value_error(tmp_path: Path):
    src_path = tmp_path / "corrupt.tif"
    src_path.write_bytes(b"this is not a geotiff")
    dst_path = tmp_path / "out.tif"

    with pytest.raises(ValueError, match="unreadable raster"):
        convert_geotiff_to_cog(str(src_path), str(dst_path))


def test_same_input_and_output_path_rejected(tmp_path: Path):
    src_path = tmp_path / "same.tif"
    _write_geotiff(src_path, np.ones((2, 2), dtype=np.uint8))

    with pytest.raises(ValueError, match="must be different"):
        convert_geotiff_to_cog(str(src_path), str(src_path))


def test_cog_has_tiled_blocks(tmp_path: Path):
    """Converted output should be tiled (COG-style), unlike the untiled source."""
    src_path = tmp_path / "untiled.tif"
    dst_path = tmp_path / "tiled.tif"
    data = np.arange(64, dtype=np.uint8).reshape(8, 8)
    _write_geotiff(src_path, data, tiled=False)

    convert_geotiff_to_cog(str(src_path), str(dst_path))

    with rasterio.open(src_path) as src:
        assert src.profile.get("tiled") in (False, None) or src.block_shapes[0] == (
            src.height,
            src.width,
        )

    with rasterio.open(dst_path) as dst:
        block_h, block_w = dst.block_shapes[0]
        assert block_h <= 512
        assert block_w <= 512
        assert dst.profile.get("compress") in ("deflate", "DEFLATE")
