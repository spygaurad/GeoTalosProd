"""Data conversion & formatting utilities.

A home for reusable converters between the platform's data representations —
rasters, vector annotations, model outputs, and exchange formats. Keep each
utility small, pure where possible, and independently testable; add new ones
here as conversion/formatting needs arise (this module is expected to grow).

Currently provides:
- ``cog``: GeoTIFF -> COG
- ``polygonize``: in-memory class-mask array -> GeoJSON features
- ``raster_mask``: raster segmentation mask (COG) -> vector annotations, so a
  raster ground-truth set becomes comparable to vector model predictions for
  IoU / precision / recall metrics.
- ``rasterize_mask``: vector annotation sets -> segmentation-mask COG, the
  inverse direction, so a vector ground truth can be compared against a model
  mask in the raster domain and rendered with class colors.
- ``raster_metrics``: per-class IoU / precision / recall / F1 + accuracy between
  two raster class masks (handles differing size/extent/CRS).
"""

from app.services.conversion.cog import convert_geotiff_to_cog
from app.services.conversion.polygonize import mask_array_to_features
from app.services.conversion.raster_mask import (
    RasterMaskVectorizeResult,
    dissolve_features_by_class,
    raster_mask_to_features,
    vectorize_raster_mask_set,
)
from app.services.conversion.raster_metrics import (
    RasterMetricsResult,
    compare_raster_masks,
)
from app.services.conversion.rasterize_mask import (
    RasterizeResult,
    build_value_class_map,
    rasterize_annotation_sets_to_cog,
)

__all__ = [
    "RasterMaskVectorizeResult",
    "RasterMetricsResult",
    "RasterizeResult",
    "build_value_class_map",
    "compare_raster_masks",
    "convert_geotiff_to_cog",
    "dissolve_features_by_class",
    "mask_array_to_features",
    "raster_mask_to_features",
    "rasterize_annotation_sets_to_cog",
    "vectorize_raster_mask_set",
]
