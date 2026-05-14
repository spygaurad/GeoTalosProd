"""Patch window generation for dataset items.

Given a dataset item's pixel dimensions + EPSG:4326 bbox, ``PatchService``
produces a list of fixed-size windows that tile the item. ModelManager drives
inference one patch at a time so models with a fixed input size can process
arbitrarily large rasters without the platform ever decoding the whole COG.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PatchWindow:
    patch_id: str
    patch_index: int
    x: int
    y: int
    width_px: int
    height_px: int
    bbox: list[float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "patch_index": self.patch_index,
            "x": self.x,
            "y": self.y,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "bbox": self.bbox,
        }


class PatchService:
    """Generate fixed-size patch windows for a dataset item context."""

    @staticmethod
    def _intersect_bbox(a: list[float], b: list[float]) -> list[float] | None:
        minx = max(a[0], b[0])
        miny = max(a[1], b[1])
        maxx = min(a[2], b[2])
        maxy = min(a[3], b[3])
        if minx >= maxx or miny >= maxy:
            return None
        return [float(minx), float(miny), float(maxx), float(maxy)]

    @staticmethod
    def _pixel_window_for_bbox(
        *,
        item_bbox: list[float],
        item_width: int,
        item_height: int,
        window_bbox: list[float],
    ) -> tuple[int, int, int, int]:
        import math

        min_lon, min_lat, max_lon, max_lat = item_bbox
        lon_span = max_lon - min_lon
        lat_span = max_lat - min_lat
        if lon_span <= 0 or lat_span <= 0:
            return 0, 0, item_width, item_height

        win_minx, win_miny, win_maxx, win_maxy = window_bbox
        x1 = math.floor(((win_minx - min_lon) / lon_span) * item_width)
        x2 = math.ceil(((win_maxx - min_lon) / lon_span) * item_width)
        y1 = math.floor(((max_lat - win_maxy) / lat_span) * item_height)
        y2 = math.ceil(((max_lat - win_miny) / lat_span) * item_height)

        x1 = max(0, min(item_width, x1))
        x2 = max(0, min(item_width, x2))
        y1 = max(0, min(item_height, y1))
        y2 = max(0, min(item_height, y2))

        return x1, y1, max(1, x2 - x1), max(1, y2 - y1)

    @staticmethod
    def _axis_starts(length: int, patch: int, stride: int) -> list[int]:
        if length <= 0:
            return [0]
        if length <= patch:
            return [0]
        starts = list(range(0, max(1, length - patch + 1), stride))
        final_start = length - patch
        if starts[-1] != final_start:
            starts.append(final_start)
        return starts

    @staticmethod
    def _patch_bbox(
        *,
        item_bbox: list[float],
        item_width: int,
        item_height: int,
        x: int,
        y: int,
        width_px: int,
        height_px: int,
    ) -> list[float]:
        min_lon, min_lat, max_lon, max_lat = item_bbox
        lon_span = max_lon - min_lon
        lat_span = max_lat - min_lat
        x1 = min_lon + (x / item_width) * lon_span
        x2 = min_lon + ((x + width_px) / item_width) * lon_span
        y_top = max_lat - (y / item_height) * lat_span
        y_bottom = max_lat - ((y + height_px) / item_height) * lat_span
        return [float(x1), float(min(y_bottom, y_top)), float(x2), float(max(y_bottom, y_top))]

    @classmethod
    def generate(
        cls,
        *,
        item_id: str,
        item_bbox: list[float],
        item_width: int | None,
        item_height: int | None,
        patch_size_px: int,
        stride_px: int | None = None,
        max_patches: int = 1024,
        clip_bbox: list[float] | None = None,
    ) -> tuple[list[PatchWindow], bool]:
        stride = stride_px or patch_size_px
        if stride > patch_size_px:
            raise ValueError("stride_px cannot be greater than patch_size_px")
        if max_patches < 1:
            raise ValueError("max_patches must be at least 1")

        width = int(item_width or 0)
        height = int(item_height or 0)
        window_bbox = item_bbox
        origin_x = 0
        origin_y = 0

        if clip_bbox is not None:
            intersected = cls._intersect_bbox(item_bbox, clip_bbox)
            if intersected is None:
                return [], False
            window_bbox = intersected
            if width > 0 and height > 0:
                origin_x, origin_y, width, height = cls._pixel_window_for_bbox(
                    item_bbox=item_bbox,
                    item_width=width,
                    item_height=height,
                    window_bbox=window_bbox,
                )

        if width <= 0 or height <= 0:
            patch = PatchWindow(
                patch_id=f"{item_id}:0",
                patch_index=0,
                x=origin_x,
                y=origin_y,
                width_px=max(width, 0),
                height_px=max(height, 0),
                bbox=window_bbox,
            )
            return [patch], False

        x_starts = cls._axis_starts(width, patch_size_px, stride)
        y_starts = cls._axis_starts(height, patch_size_px, stride)
        windows: list[PatchWindow] = []
        capped = False
        patch_index = 0
        for y in y_starts:
            for x in x_starts:
                if len(windows) >= max_patches:
                    capped = True
                    break
                width_px = min(patch_size_px, width - x)
                height_px = min(patch_size_px, height - y)
                patch_x = origin_x + x
                patch_y = origin_y + y
                windows.append(
                    PatchWindow(
                        patch_id=f"{item_id}:{patch_index}",
                        patch_index=patch_index,
                        x=patch_x,
                        y=patch_y,
                        width_px=width_px,
                        height_px=height_px,
                        bbox=cls._patch_bbox(
                            item_bbox=window_bbox,
                            item_width=width,
                            item_height=height,
                            x=x,
                            y=y,
                            width_px=width_px,
                            height_px=height_px,
                        ),
                    )
                )
                patch_index += 1
            if capped:
                break
        return windows, capped
