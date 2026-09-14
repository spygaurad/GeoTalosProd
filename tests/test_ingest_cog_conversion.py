"""Ingest-job integration for automatic GeoTIFF → COG conversion (Milestone 1)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.core.enums import DatasetStatus, JobStatus
from app.models.dataset import Dataset
from app.models.job import Job
from app.services.conversion import convert_geotiff_to_cog
from app.workers.ingestion.rasterio_utils import is_cloud_optimized_geotiff, validate_cog
from app.workers.ingestion.tasks import (
    PermanentTaskError,
    _maybe_convert_non_cog,
    _processed_cog_object_key,
    _safe_raster_stem,
    ingest_dataset,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
S3_KEY = f"datasets/{DATASET_ID}/source.tif"
BUCKET = f"org-{ORG_ID}"
SOURCE_URI = f"s3://{BUCKET}/{S3_KEY}"

_TRANSFORM = from_origin(-10.0, 10.0, 1.0, 1.0)
_STAC_ITEM = {"geometry": None, "properties": {"datetime": "2020-01-01T00:00:00Z"}}
_AGG = {
    "metadata": {"band_count": ["uint8"], "file_count": 1},
    "wkt": None,
    "start_date": None,
    "end_date": None,
}


def _write_geotiff(path: Path, data: np.ndarray, *, crs="EPSG:4326", tiled: bool = False) -> Path:
    if data.ndim == 2:
        bands = data[np.newaxis, ...]
        height, width = data.shape
        count = 1
    else:
        bands = data
        count, height, width = data.shape
    profile = {
        "driver": "GTiff",
        "dtype": str(data.dtype),
        "count": count,
        "height": height,
        "width": width,
        "crs": crs,
        "transform": _TRANSFORM,
        "tiled": tiled,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(bands)
    return path


def _mock_session(*, dataset_org_id=ORG_ID):
    session = MagicMock()
    job = MagicMock()
    job.id = JOB_ID
    job.organization_id = ORG_ID
    job.config = {}
    job.logs = None
    job.status = JobStatus.PENDING
    dataset = MagicMock()
    dataset.id = DATASET_ID
    dataset.organization_id = dataset_org_id
    dataset.dataset_type = "imagery"
    dataset.stac_collection_id = "col-1"
    dataset.status = DatasetStatus.PENDING

    def _get(model, _pk):
        if model is Job:
            return job
        if model is Dataset:
            return dataset
        return None

    session.get.side_effect = _get
    worker_cm = MagicMock()
    worker_cm.__enter__.return_value = session
    worker_cm.__exit__.return_value = False
    return worker_cm, session, job, dataset


# ── Detection ────────────────────────────────────────────────────────────────


def test_safe_stem_strips_path_and_unsafe_chars():
    assert _safe_raster_stem("../../evil name.tif") == "evil_name"
    assert _safe_raster_stem("") == "raster"
    assert _processed_cog_object_key(DATASET_ID, "My File.tif") == (
        f"datasets/{DATASET_ID}/processed/My_File_cog.tif"
    )


def test_untiled_geotiff_is_valid_but_not_cog(tmp_path: Path):
    path = tmp_path / "plain.tif"
    _write_geotiff(path, np.arange(64, dtype=np.uint8).reshape(8, 8), tiled=False)

    is_valid, is_cog, issues = is_cloud_optimized_geotiff(str(path), {})

    assert is_valid is True
    assert is_cog is False
    assert issues


def test_missing_crs_is_invalid_raster(tmp_path: Path):
    path = tmp_path / "no_crs.tif"
    _write_geotiff(path, np.ones((2, 2), dtype=np.uint8), crs=None)

    is_valid, is_cog, issues = is_cloud_optimized_geotiff(str(path), {})

    assert is_valid is False
    assert is_cog is False
    assert any("coordinate reference system" in i for i in issues)


def test_converted_output_passes_validate_cog_hard_checks(tmp_path: Path):
    src = tmp_path / "plain.tif"
    dst = tmp_path / "cog.tif"
    _write_geotiff(src, np.arange(64, dtype=np.uint8).reshape(8, 8), tiled=False)
    convert_geotiff_to_cog(str(src), str(dst))

    is_valid, _is_cog, issues = is_cloud_optimized_geotiff(str(dst), {})
    valid_cog, cog_issues = validate_cog(str(dst), {})

    assert is_valid is True
    assert valid_cog is True
    assert not any("Could not open" in i for i in issues)
    assert not any("Could not open" in i for i in cog_issues)


# ── _maybe_convert_non_cog ───────────────────────────────────────────────────


def test_already_cog_skips_conversion_and_upload():
    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, True, []),
        ) as inspect,
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
    ):
        result = _maybe_convert_non_cog(
            organization_id=ORG_ID,
            dataset_id=DATASET_ID,
            s3_uri=SOURCE_URI,
            filename="source.tif",
            gdal_env={"AWS_S3_ENDPOINT": "minio:9000"},
        )

    assert result == SOURCE_URI
    inspect.assert_called_once_with(SOURCE_URI, {"AWS_S3_ENDPOINT": "minio:9000"})
    convert.assert_not_called()
    upload.assert_not_called()


def test_non_cog_converts_uploads_and_returns_processed_uri(tmp_path: Path):
    src = tmp_path / "source.tif"
    _write_geotiff(src, np.arange(20, dtype=np.uint8).reshape(4, 5))
    uploaded: dict = {}

    def _upload(org_id, key, path, content_type="image/tiff"):
        uploaded["org_id"] = org_id
        uploaded["key"] = key
        uploaded["exists"] = os.path.isfile(path)
        uploaded["content_type"] = content_type
        uploaded["path"] = path

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No compression — storage size will be larger than necessary"]),
        ),
        patch(
            "app.workers.ingestion.tasks.storage_service.upload_from_path",
            side_effect=_upload,
        ),
        patch(
            "app.workers.ingestion.tasks.storage_service.bucket_name",
            return_value=BUCKET,
        ),
    ):
        result = _maybe_convert_non_cog(
            organization_id=ORG_ID,
            dataset_id=DATASET_ID,
            s3_uri=str(src),
            filename="My File.tif",
            gdal_env={},
        )

    assert uploaded["org_id"] == ORG_ID
    assert uploaded["key"] == f"datasets/{DATASET_ID}/processed/My_File_cog.tif"
    assert uploaded["exists"] is True
    assert uploaded["content_type"] == "image/tiff"
    assert result == f"s3://{BUCKET}/datasets/{DATASET_ID}/processed/My_File_cog.tif"
    assert not os.path.exists(uploaded["path"])


def test_invalid_raster_raises_permanent_error_without_conversion():
    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(False, False, ["File has no coordinate reference system — not a valid georeferenced raster"]),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
    ):
        with pytest.raises(PermanentTaskError, match="coordinate reference system"):
            _maybe_convert_non_cog(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                s3_uri=SOURCE_URI,
                filename="source.tif",
                gdal_env={},
            )

    convert.assert_not_called()
    upload.assert_not_called()


def test_converter_failure_is_permanent_and_does_not_upload():
    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No tile block structure — not COG-optimised (will render slowly)"]),
        ),
        patch(
            "app.workers.ingestion.tasks.convert_geotiff_to_cog",
            side_effect=ValueError("COG creation failed: Rasterio COG driver could not write /tmp/x: boom"),
        ),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        with pytest.raises(PermanentTaskError, match="GeoTIFF to COG conversion failed"):
            _maybe_convert_non_cog(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                s3_uri=SOURCE_URI,
                filename="source.tif",
                gdal_env={},
            )

    upload.assert_not_called()


def test_upload_failure_propagates_and_cleans_temp():
    recorded: dict = {}

    def _convert(_inp, out, **_kw):
        recorded["out"] = out
        Path(out).write_bytes(b"cog-bytes")

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No compression — storage size will be larger than necessary"]),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", side_effect=_convert),
        patch(
            "app.workers.ingestion.tasks.storage_service.upload_from_path",
            side_effect=RuntimeError("minio unavailable"),
        ),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        with pytest.raises(RuntimeError, match="minio unavailable"):
            _maybe_convert_non_cog(
                organization_id=ORG_ID,
                dataset_id=DATASET_ID,
                s3_uri=SOURCE_URI,
                filename="source.tif",
                gdal_env={},
            )

    assert recorded["out"]
    assert not os.path.exists(recorded["out"])


# ── ingest_dataset single-GeoTIFF path ───────────────────────────────────────


def test_ingest_already_cog_keeps_original_uri_and_skips_converter():
    worker_cm, _session, job, dataset = _mock_session()
    ingest_cog = MagicMock(return_value=(True, [], "stac-item-1", _STAC_ITEM))
    upsert = MagicMock()
    convert = MagicMock()
    upload = MagicMock()

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}),
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, True, []),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", convert),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", upload),
        patch("app.workers.ingestion.tasks._ingest_single_cog", ingest_cog),
        patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert),
        patch("app.workers.ingestion.tasks._compute_aggregated_metadata", return_value=_AGG),
        patch("app.workers.ingestion.tasks._publish_job_event"),
        patch("app.automation.event_dispatcher.dispatch_event_sync"),
    ):
        ingest_dataset.run(str(JOB_ID), str(DATASET_ID), S3_KEY, "source.tif")

    convert.assert_not_called()
    upload.assert_not_called()
    ingest_cog.assert_called_once()
    assert ingest_cog.call_args.args[0] == SOURCE_URI
    assert upsert.call_args.kwargs["s3_uri"] == SOURCE_URI
    assert upsert.call_args.kwargs["organization_id"] == ORG_ID
    assert job.status == JobStatus.COMPLETED
    assert dataset.status == DatasetStatus.READY


def test_ingest_non_cog_uses_generated_uri_for_stac_and_item():
    worker_cm, _session, job, dataset = _mock_session()
    ingest_cog = MagicMock(return_value=(True, [], "stac-item-1", _STAC_ITEM))
    upsert = MagicMock()
    processed_uri = f"s3://{BUCKET}/datasets/{DATASET_ID}/processed/source_cog.tif"

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={"AWS_S3_ENDPOINT": "minio:9000"}),
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No compression — storage size will be larger than necessary"]),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", return_value="/tmp/out.tif") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
        patch("app.workers.ingestion.tasks._ingest_single_cog", ingest_cog),
        patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert),
        patch("app.workers.ingestion.tasks._compute_aggregated_metadata", return_value=_AGG),
        patch("app.workers.ingestion.tasks._publish_job_event"),
        patch("app.automation.event_dispatcher.dispatch_event_sync"),
    ):
        ingest_dataset.run(str(JOB_ID), str(DATASET_ID), S3_KEY, "source.tif")

    convert.assert_called_once()
    assert convert.call_args.kwargs["gdal_env"] == {"AWS_S3_ENDPOINT": "minio:9000"}
    upload.assert_called_once()
    assert upload.call_args.args[0] == ORG_ID
    assert upload.call_args.args[1] == f"datasets/{DATASET_ID}/processed/source_cog.tif"
    ingest_cog.assert_called_once()
    assert ingest_cog.call_args.args[0] == processed_uri
    assert upsert.call_args.kwargs["s3_uri"] == processed_uri
    assert upsert.call_args.kwargs["organization_id"] == ORG_ID
    assert job.status == JobStatus.COMPLETED
    assert dataset.status == DatasetStatus.READY


def test_ingest_converter_failure_fails_job_and_skips_stac():
    worker_cm, _session, job, dataset = _mock_session()
    ingest_cog = MagicMock()
    upsert = MagicMock()

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}),
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No tile block structure — not COG-optimised (will render slowly)"]),
        ),
        patch(
            "app.workers.ingestion.tasks.convert_geotiff_to_cog",
            side_effect=ValueError("COG creation failed: unreadable raster"),
        ),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
        patch("app.workers.ingestion.tasks._ingest_single_cog", ingest_cog),
        patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert),
        patch("app.workers.ingestion.tasks._publish_job_event"),
    ):
        ingest_dataset.run(str(JOB_ID), str(DATASET_ID), S3_KEY, "source.tif")

    upload.assert_not_called()
    ingest_cog.assert_not_called()
    upsert.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert dataset.status == DatasetStatus.FAILED
    assert "GeoTIFF to COG conversion failed" in (job.logs or "")


def test_ingest_upload_failure_retries_without_stac():
    worker_cm, session, job, dataset = _mock_session()
    ingest_cog = MagicMock()
    upsert = MagicMock()

    def _retry(*, exc=None, **_kwargs):
        raise exc

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}),
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No compression — storage size will be larger than necessary"]),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", return_value="/tmp/out.tif"),
        patch(
            "app.workers.ingestion.tasks.storage_service.upload_from_path",
            side_effect=RuntimeError("minio unavailable"),
        ),
        patch("app.workers.ingestion.tasks._ingest_single_cog", ingest_cog),
        patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert),
        patch.object(ingest_dataset, "retry", side_effect=_retry),
    ):
        with pytest.raises(RuntimeError, match="minio unavailable"):
            ingest_dataset.run(str(JOB_ID), str(DATASET_ID), S3_KEY, "source.tif")

    ingest_cog.assert_not_called()
    upsert.assert_not_called()
    session.rollback.assert_called()
    assert job.status != JobStatus.COMPLETED
    assert dataset.status != DatasetStatus.READY


def test_ingest_rejects_organization_mismatch():
    worker_cm, _session, job, dataset = _mock_session(dataset_org_id=OTHER_ORG_ID)
    convert = MagicMock()
    ingest_cog = MagicMock()

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog", convert),
        patch("app.workers.ingestion.tasks._ingest_single_cog", ingest_cog),
        patch("app.workers.ingestion.tasks._publish_job_event"),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}),
    ):
        ingest_dataset.run(str(JOB_ID), str(DATASET_ID), S3_KEY, "source.tif")

    convert.assert_not_called()
    ingest_cog.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert dataset.status == DatasetStatus.FAILED
    assert "organization mismatch" in (job.logs or "")
