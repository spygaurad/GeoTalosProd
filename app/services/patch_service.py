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
    ) -> tuple[list[PatchWindow], bool]:
        stride = stride_px or patch_size_px
        if stride > patch_size_px:
            raise ValueError("stride_px cannot be greater than patch_size_px")
        if max_patches < 1:
            raise ValueError("max_patches must be at least 1")

        width = int(item_width or 0)
        height = int(item_height or 0)
        if width <= 0 or height <= 0:
            patch = PatchWindow(
                patch_id=f"{item_id}:0",
                patch_index=0,
                x=0,
                y=0,
                width_px=max(width, 0),
                height_px=max(height, 0),
                bbox=item_bbox,
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
                windows.append(
                    PatchWindow(
                        patch_id=f"{item_id}:{patch_index}",
                        patch_index=patch_index,
                        x=x,
                        y=y,
                        width_px=width_px,
                        height_px=height_px,
                        bbox=cls._patch_bbox(
                            item_bbox=item_bbox,
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
