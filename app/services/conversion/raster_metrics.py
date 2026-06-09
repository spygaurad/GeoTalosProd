"""Per-class segmentation metrics between two raster class masks (COGs).

Compares a ground-truth mask against a model-output mask in the *raster*
domain. The two COGs may differ in size, extent, CRS and resolution — a
ground-truth mask is often a small AOI while the model output covers a whole
scene. Per the requirement, we take the **smaller-extent** raster as the
evaluation grid and resample the larger one onto it (nearest-neighbour, to
preserve class labels), so metrics are computed only over the overlap on the
small bounds.

Class alignment is by *class id*: each mask carries its own
``value_class_map`` (pixel value -> class id), which can differ between the two
datasets, so we remap both into a shared class-id space before comparing.

Returns per-class IoU / precision / recall / F1 plus overall pixel accuracy,
foreground accuracy, and mean IoU — a ``quality_metrics`` payload the report
node can render.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.workers.ingestion.rasterio_utils import _vsi_path

logger = logging.getLogger(__name__)


def _value_to_class(class_map: dict[str, Any]) -> dict[int, str]:
    """``{pixel_value: class_id}`` from a stored class_map's value_class_map.

    Only integral pixel values are usable for label masks; non-integral keys
    are skipped (they cannot index a discrete class raster).
    """
    vcm = (class_map or {}).get("value_class_map") or {}
    out: dict[int, str] = {}
    for k, v in vcm.items():
        try:
            fk = float(str(k).strip())
        except (TypeError, ValueError):
            continue
        if fk.is_integer():
            out[int(fk)] = str(v)
    return out


@dataclass
class RasterMetricsResult:
    per_class: list[dict] = field(default_factory=list)
    overall: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)


def _read_grid_meta(uri: str, gdal_env: dict, band_index: int) -> dict:
    """Open a COG and return its grid metadata + bounds area (in 4326 deg²)."""
    import rasterio
    from rasterio.env import Env
    from rasterio.warp import transform_bounds

    with Env(**gdal_env):
        with rasterio.open(_vsi_path(uri)) as src:
            b = src.bounds
            try:
                wgs = transform_bounds(src.crs, "EPSG:4326", *b, densify_pts=21) if src.crs else b
            except Exception:  # noqa: BLE001 — fall back to native bounds for area ranking
                wgs = b
            area = abs((wgs[2] - wgs[0]) * (wgs[3] - wgs[1]))
            return {
                "crs": src.crs.to_string() if src.crs else None,
                "transform": src.transform,
                "width": src.width,
                "height": src.height,
                "bounds": tuple(b),
                "band_count": src.count,
                "area_4326": area,
            }


def _read_on_grid(uri: str, gdal_env: dict, band_index: int, dest_meta: dict):
    """Read ``uri``'s band resampled onto ``dest_meta``'s grid (nearest)."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.env import Env
    from rasterio.warp import reproject

    dest = np.zeros((dest_meta["height"], dest_meta["width"]), dtype="float64")
    with Env(**gdal_env):
        with rasterio.open(_vsi_path(uri)) as src:
            band = min(band_index, src.count)
            reproject(
                source=rasterio.band(src, band),
                destination=dest,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dest_meta["transform"],
                dst_crs=dest_meta["crs"],
                resampling=Resampling.nearest,
            )
    return dest


def compare_raster_masks(
    gt_uri: str,
    pred_uri: str,
    gdal_env: dict,
    *,
    gt_class_map: dict[str, Any],
    pred_class_map: dict[str, Any],
    class_names: dict[str, str] | None = None,
) -> RasterMetricsResult:
    """Compute per-class raster metrics between a GT and a prediction mask.

    Args:
        gt_uri / pred_uri: ``s3://`` of the ground-truth / prediction COG.
        gdal_env: rasterio ``Env`` options.
        gt_class_map / pred_class_map: each dataset's stored
            ``rendering_config.class_map`` (``{value_class_map, band_index,
            nodata_value, ...}``).
        class_names: optional ``{class_id: display_name}`` for labelling.
    """
    import numpy as np

    class_names = class_names or {}
    gt_band = int((gt_class_map or {}).get("band_index", 1) or 1)
    pred_band = int((pred_class_map or {}).get("band_index", 1) or 1)

    gt_meta = _read_grid_meta(gt_uri, gdal_env, gt_band)
    pred_meta = _read_grid_meta(pred_uri, gdal_env, pred_band)

    # Smaller-extent raster is the evaluation grid; resample the other onto it.
    if pred_meta["area_4326"] <= gt_meta["area_4326"]:
        grid_meta, grid_is = pred_meta, "prediction"
    else:
        grid_meta, grid_is = gt_meta, "ground_truth"

    gt_arr = _read_on_grid(gt_uri, gdal_env, gt_band, grid_meta)
    pred_arr = _read_on_grid(pred_uri, gdal_env, pred_band, grid_meta)

    gt_v2c = _value_to_class(gt_class_map)
    pred_v2c = _value_to_class(pred_class_map)
    gt_c2v = {cid: val for val, cid in gt_v2c.items()}
    pred_c2v = {cid: val for val, cid in pred_v2c.items()}

    # Shared class-id space (stable order for deterministic output).
    class_ids = sorted(
        set(gt_c2v) | set(pred_c2v),
        key=lambda cid: (class_names.get(cid, cid).lower(), cid),
    )

    # Canonical label arrays (0 = background/unmatched) for overall accuracy.
    gt_canon = np.zeros(gt_arr.shape, dtype="int32")
    pred_canon = np.zeros(pred_arr.shape, dtype="int32")

    per_class: list[dict] = []
    iou_values: list[float] = []
    for canon, cid in enumerate(class_ids, start=1):
        gt_val = gt_c2v.get(cid)
        pred_val = pred_c2v.get(cid)
        gt_mask = (gt_arr == gt_val) if gt_val is not None else np.zeros(gt_arr.shape, dtype=bool)
        pred_mask = (pred_arr == pred_val) if pred_val is not None else np.zeros(pred_arr.shape, dtype=bool)

        gt_canon[gt_mask] = canon
        pred_canon[pred_mask] = canon

        tp = int(np.count_nonzero(gt_mask & pred_mask))
        fp = int(np.count_nonzero(pred_mask & ~gt_mask))
        fn = int(np.count_nonzero(gt_mask & ~pred_mask))
        union = tp + fp + fn

        iou = tp / union if union > 0 else None
        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision and recall and (precision + recall) > 0
            else None
        )
        if iou is not None:
            iou_values.append(iou)

        per_class.append({
            "class_id": cid,
            "class_name": class_names.get(cid, cid),
            "gt_pixels": int(np.count_nonzero(gt_mask)),
            "pred_pixels": int(np.count_nonzero(pred_mask)),
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "iou": round(iou, 4) if iou is not None else None,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1_score": round(f1, 4) if f1 is not None else None,
            "present_in_gt": gt_val is not None,
            "present_in_prediction": pred_val is not None,
        })

    total_px = int(gt_canon.size)
    correct = int(np.count_nonzero(gt_canon == pred_canon))
    fg = (gt_canon != 0) | (pred_canon != 0)
    fg_total = int(np.count_nonzero(fg))
    fg_correct = int(np.count_nonzero((gt_canon == pred_canon) & fg))
    mean_iou = round(sum(iou_values) / len(iou_values), 4) if iou_values else None

    overall = {
        "class_count": len(class_ids),
        "pixel_accuracy": round(correct / total_px, 4) if total_px else None,
        "foreground_accuracy": round(fg_correct / fg_total, 4) if fg_total else None,
        "mean_iou": mean_iou,
        "evaluated_pixels": total_px,
        "foreground_pixels": fg_total,
    }
    grid = {
        "grid_from": grid_is,
        "crs": grid_meta["crs"],
        "width": grid_meta["width"],
        "height": grid_meta["height"],
        "gt_extent_4326_area": round(gt_meta["area_4326"], 10),
        "prediction_extent_4326_area": round(pred_meta["area_4326"], 10),
    }

    logger.info(
        "compare_raster_masks: grid=%s %dx%d classes=%d mean_iou=%s acc=%s",
        grid_is, grid_meta["width"], grid_meta["height"], len(class_ids),
        mean_iou, overall["pixel_accuracy"],
    )
    return RasterMetricsResult(per_class=per_class, overall=overall, grid=grid)
