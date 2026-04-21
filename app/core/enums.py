"""Canonical status and type constants for all stateful columns.

Uses ``StrEnum`` (Python 3.11+) so every member compares equal to its
plain-string value — SQLAlchemy, Pydantic, and Celery workers can all use
these directly without any conversion.

  DatasetStatus.READY == "ready"   # True
  JobStatus.FAILED == "failed"     # True

Alembic migrations import this module to build CHECK constraints, ensuring
the DB constraint and the application code always stay in sync.
"""

from enum import StrEnum


class DatasetStatus(StrEnum):
    PENDING = "pending"      # record created, no file yet
    INGESTING = "ingesting"  # Celery task is running
    READY = "ready"          # STAC item registered, tiles available
    FAILED = "failed"        # ingestion or COG validation failed


class JobStatus(StrEnum):
    PENDING = "pending"      # created by API, not yet picked up
    QUEUED = "queued"        # upload complete, task submitted to Celery
    RUNNING = "running"      # worker is actively processing
    COMPLETED = "completed"  # finished successfully
    FAILED = "failed"        # terminal failure (after retries)
    CANCELLED = "cancelled"  # explicitly aborted by user


class JobType(StrEnum):
    INGEST = "ingest"
    INFERENCE = "inference"
    IMPORT_ANNOTATIONS = "import_annotations"


class MapLayerSourceType(StrEnum):
    # Canonical target-state names (unified_platform_plan v2)
    DATASET_ITEM = "dataset_item"       # single STAC item — app DB FK + pgSTAC ref
    DATASET_MOSAIC = "dataset_mosaic"   # full collection mosaic via pgSTAC search
    STAC_SEARCH = "stac_search"         # pre-registered pgSTAC search id
    ANNOTATION_SET = "annotation_set"   # raster mask or vector from annotation set
    FEATURE_LAYER = "feature_layer"     # org-curated vector layer
    TILE_SOURCE = "tile_source"         # external XYZ / MVT / WMS row
    BASEMAP = "basemap"                 # basemap promoted into MapLayer
    XARRAY_VARIABLE = "xarray_variable" # placeholder, not resolvable yet

    # Legacy aliases kept during Stage 1 transition — existing rows still use
    # these. Stage 3 will migrate rows to the canonical names and drop these.
    DATASET = "dataset"                 # legacy alias for DATASET_MOSAIC
    STAC_ITEM = "stac_item"             # legacy alias for DATASET_ITEM
    TILE_SERVICE = "tile_service"       # legacy alias for TILE_SOURCE


class MapLayerType(StrEnum):
    RASTER = "raster"
    VECTOR = "vector"
    ANNOTATION = "annotation"
    BASEMAP = "basemap"


class DatasetType(StrEnum):
    IMAGERY = "imagery"
    CONTINUOUS = "continuous"                    # NDVI, DEM, hillshade — derived continuous
    MASK = "mask"                                # canonical name for segmentation masks
    BASEMAP_TILES = "basemap_tiles"              # org basemap imagery
    EXTERNAL_REFERENCE = "external_reference"    # roads, parks, NLCD, etc.

    # Legacy alias, kept for migration compat. Stage 2+ prefers MASK.
    SEGMENTATION_MASK = "segmentation_mask"


class DatasetItemType(StrEnum):
    """Per-item classification mirroring DatasetType at item grain."""
    IMAGERY = "imagery"
    CONTINUOUS = "continuous"
    MASK = "mask"
    EXTERNAL_REFERENCE = "external_reference"


class FeatureLayerRole(StrEnum):
    REFERENCE = "reference"   # roads, boundaries — context
    AOI = "aoi"               # user-saved AOI polygon
    SKETCH = "sketch"         # transient map sketch


class DerivationKind(StrEnum):
    NDVI = "ndvi"
    EVI = "evi"
    NDWI = "ndwi"
    SAVI = "savi"
    NBR = "nbr"
    HILLSHADE = "hillshade"
    SEGMENTATION = "segmentation"   # SAM3 / ML output
    CHANGE = "change"               # pairwise diffs (future)
