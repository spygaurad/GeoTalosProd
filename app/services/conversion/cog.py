"""GeoTIFF -> Cloud Optimized GeoTIFF."""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext

logger = logging.getLogger(__name__)

# Match rasterize_mask COG write settings.
_COG_COMPRESS = "deflate"
_COG_BLOCKSIZE = 512
_BOUNDS_ATOL = 1e-6


def _gdal_path(path: str) -> str:
    if path.startswith("s3://"):
        return path.replace("s3://", "/vsis3/", 1)
    return path


def _bounds_match(a, b, *, atol: float = _BOUNDS_ATOL) -> bool:
    return all(abs(float(x) - float(y)) <= atol for x, y in zip(a, b, strict=True))


def _validate_source(src, input_path: str) -> None:
    if not src.indexes:
        raise ValueError(
            f"COG creation failed: source raster has no bands ({input_path})"
        )
    if src.crs is None:
        raise ValueError(
            "COG creation failed: source raster has no coordinate reference system "
            f"({input_path})"
        )
    if src.width <= 0 or src.height <= 0:
        raise ValueError(
            f"COG creation failed: source raster has invalid dimensions "
            f"{src.width}x{src.height} ({input_path})"
        )


def _validate_output(output_path: str, src_meta: dict) -> None:
    import rasterio

    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise ValueError(
            f"COG creation failed: output file is missing or empty ({output_path})"
        )

    try:
        dst = rasterio.open(output_path)
    except Exception as exc:
        raise ValueError(
            f"COG creation failed: output could not be opened ({output_path}): {exc}"
        ) from exc

    try:
        if not dst.indexes:
            raise ValueError(
                f"COG creation failed: output has no bands ({output_path})"
            )
        if dst.crs is None:
            raise ValueError(
                f"COG creation failed: output has no coordinate reference system "
                f"({output_path})"
            )
        if dst.width <= 0 or dst.height <= 0:
            raise ValueError(
                f"COG creation failed: output has invalid dimensions "
                f"{dst.width}x{dst.height} ({output_path})"
            )
        if dst.crs != src_meta["crs"]:
            raise ValueError(
                f"COG creation failed: output CRS {dst.crs} does not match source "
                f"CRS {src_meta['crs']} ({output_path})"
            )
        if dst.width != src_meta["width"] or dst.height != src_meta["height"]:
            raise ValueError(
                f"COG creation failed: output dimensions {dst.width}x{dst.height} "
                f"do not match source {src_meta['width']}x{src_meta['height']} "
                f"({output_path})"
            )
        if dst.count != src_meta["count"]:
            raise ValueError(
                f"COG creation failed: output band count {dst.count} does not match "
                f"source {src_meta['count']} ({output_path})"
            )
        if dst.dtypes != src_meta["dtypes"]:
            raise ValueError(
                f"COG creation failed: output dtypes {dst.dtypes} do not match "
                f"source {src_meta['dtypes']} ({output_path})"
            )
        if not _bounds_match(dst.bounds, src_meta["bounds"]):
            raise ValueError(
                f"COG creation failed: output bounds {tuple(dst.bounds)} are not "
                f"consistent with source {src_meta['bounds']} ({output_path})"
            )
    finally:
        dst.close()


def convert_geotiff_to_cog(
    input_path: str,
    output_path: str,
    *,
    gdal_env: dict | None = None,
) -> str:
    import rasterio
    from rasterio.env import Env
    from rasterio.shutil import copy as rio_copy

    if not input_path:
        raise ValueError("COG creation failed: input_path is empty")
    if not output_path:
        raise ValueError("COG creation failed: output_path is empty")
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError(
            "COG creation failed: input_path and output_path must be different"
        )

    src_path = _gdal_path(input_path)
    env_ctx = Env(**gdal_env) if gdal_env else nullcontext()

    with env_ctx:
        try:
            src = rasterio.open(src_path)
        except Exception as exc:
            raise ValueError(
                f"COG creation failed: unreadable raster ({input_path}): {exc}"
            ) from exc

        try:
            _validate_source(src, input_path)
            src_meta = {
                "crs": src.crs,
                "bounds": tuple(src.bounds),
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtypes": src.dtypes,
            }
            try:
                rio_copy(
                    src,
                    output_path,
                    driver="COG",
                    compress=_COG_COMPRESS,
                    blocksize=_COG_BLOCKSIZE,
                )
            except Exception as exc:
                if os.path.isfile(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        logger.warning(
                            "could not remove incomplete COG %s", output_path
                        )
                raise ValueError(
                    f"COG creation failed: Rasterio COG driver could not write "
                    f"{output_path}: {exc}"
                ) from exc
        finally:
            src.close()

        try:
            _validate_output(output_path, src_meta)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                f"COG creation failed: output validation failed ({output_path}): {exc}"
            ) from exc

    logger.info(
        "convert_geotiff_to_cog: %s -> %s (%dx%d, %d bands, crs=%s)",
        input_path,
        output_path,
        src_meta["width"],
        src_meta["height"],
        src_meta["count"],
        src_meta["crs"],
    )
    return output_path
