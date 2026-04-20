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
    IMPORT_ANNOTATIONS = "import_annotations"


class MapLayerSourceType(StrEnum):
    DATASET = "dataset"           # full collection mosaic (multi-item)
    STAC_ITEM = "stac_item"       # single STAC item
    TILE_SERVICE = "tile_service" # external XYZ / WMS URL
    ANNOTATION_SET = "annotation_set" # annotation set vector features


class MapLayerType(StrEnum):
    RASTER = "raster"
    VECTOR = "vector"
    ANNOTATION = "annotation"


class DatasetType(StrEnum):
    IMAGERY = "imagery"
    SEGMENTATION_MASK = "segmentation_mask"
