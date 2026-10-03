"""Worker tests for extract_raster_features."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.core.enums import DatasetStatus, JobStatus, JobType
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.annotation_set import AnnotationSet
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.job_output import JobOutput
from app.services.conversion.threshold_extract import ThresholdPersistResult
from app.workers.ingestion.tasks import extract_raster_features

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
ITEM_ID = UUID("00000000-0000-0000-0000-000000000010")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000020")
CLASS_ID = UUID("00000000-0000-0000-0000-000000000021")
USER_ID = UUID("00000000-0000-0000-0000-000000000030")
SET_ID = UUID("00000000-0000-0000-0000-000000000040")
SOURCE_URI = "s3://org-bucket/datasets/source.tif"


def _config(**overrides):
    cfg = {
        "dataset_item_id": str(ITEM_ID),
        "schema_id": str(SCHEMA_ID),
        "output_class_id": str(CLASS_ID),
        "band_index": 1,
        "threshold_min": 0.2,
        "index": "band",
    }
    cfg.update(overrides)
    return cfg


def _world():
    session = MagicMock()
    job = MagicMock()
    job.id = JOB_ID
    job.organization_id = ORG_ID
    job.created_by_user_id = USER_ID
    job.type = JobType.EXTRACT_RASTER_FEATURES
    job.config = _config()
    job.logs = None
    job.status = JobStatus.QUEUED
    job.started_at = None

    item = MagicMock()
    item.id = ITEM_ID
    item.dataset_id = SOURCE_ID
    item.organization_id = ORG_ID
    item.is_active = True
    item.s3_uri = SOURCE_URI
    item.filename = "source.tif"

    dataset = MagicMock()
    dataset.id = SOURCE_ID
    dataset.organization_id = ORG_ID
    dataset.deleted_at = None
    dataset.status = DatasetStatus.READY

    schema = MagicMock()
    schema.id = SCHEMA_ID
    schema.organization_id = ORG_ID
    schema.deleted_at = None

    ann_class = MagicMock()
    ann_class.id = CLASS_ID
    ann_class.schema_id = SCHEMA_ID

    def _get(model, pk):
        if model is Job:
            return job
        if model is DatasetItem and pk == ITEM_ID:
            return item
        if model is Dataset and pk == SOURCE_ID:
            return dataset
        if model is AnnotationSchema and pk == SCHEMA_ID:
            return schema
        if model is AnnotationClass and pk == CLASS_ID:
            return ann_class
        return None

    session.get.side_effect = _get
    executed = MagicMock()
    executed.scalar_one_or_none.return_value = None
    session.execute.return_value = executed
    session.scalar.return_value = 0

    worker_cm = MagicMock()
    worker_cm.__enter__.return_value = session
    worker_cm.__exit__.return_value = False
    return worker_cm, session, job, item, dataset


@contextmanager
def _patches(world, *, features=None, persisted=None, threshold_error=None):
    worker_cm, session, job, item, dataset = world
    threshold = MagicMock(return_value=features if features is not None else [{"type": "Feature"}])
    if threshold_error is not None:
        threshold.side_effect = threshold_error
    persist = MagicMock(
        return_value=persisted
        or ThresholdPersistResult(
            annotation_set_id=SET_ID,
            feature_count=len(threshold.return_value) if threshold_error is None else 0,
            dataset_id=SOURCE_ID,
            dataset_item_id=ITEM_ID,
        )
    )
    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={"AWS": "test"}),
        patch(
            "app.services.conversion.threshold_extract.threshold_raster_to_features",
            threshold,
        ),
        patch(
            "app.services.conversion.threshold_extract.persist_threshold_features",
            persist,
        ),
    ):
        yield session, job, item, dataset, threshold, persist


def test_worker_completes_with_annotation_set_and_leaves_source_unchanged():
    world = _world()
    item = world[3]
    dataset = world[4]
    before = (item.s3_uri, item.filename, dataset.status)

    with _patches(world) as (session, job, item, dataset, threshold, persist):
        extract_raster_features.run(str(JOB_ID))

    assert job.status == JobStatus.COMPLETED
    result = job.config["result"]
    assert result["annotation_set_id"] == str(SET_ID)
    assert result["feature_count"] == 1
    assert result["method"] == "threshold"
    assert result["index"] == "band"
    assert result["band_index"] == 1
    assert result["threshold_min"] == 0.2
    assert result["threshold_max"] is None
    assert job.config["output_annotation_set_id"] == str(SET_ID)
    outputs = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], JobOutput)]
    assert len(outputs) == 1
    assert outputs[0].output_type == "annotation_set"
    assert outputs[0].output_id == SET_ID
    threshold.assert_called_once()
    assert threshold.call_args.args[0] is item
    persist.assert_called_once()
    assert persist.call_args.kwargs["commit"] is False
    assert (item.s3_uri, item.filename, dataset.status) == before


def test_worker_empty_mask_completes_with_zero_features():
    world = _world()
    persisted = ThresholdPersistResult(
        annotation_set_id=SET_ID,
        feature_count=0,
        dataset_id=SOURCE_ID,
        dataset_item_id=ITEM_ID,
    )
    with _patches(world, features=[], persisted=persisted) as (
        _s, job, _i, _d, threshold, persist,
    ):
        extract_raster_features.run(str(JOB_ID))

    assert job.status == JobStatus.COMPLETED
    assert job.config["result"]["feature_count"] == 0
    assert persist.call_args.args[1] == []
    threshold.assert_called_once()


def test_worker_org_mismatch_is_permanent_and_skips_raster_io():
    world = _world()
    world[3].organization_id = OTHER_ORG_ID
    with _patches(world) as (_s, job, item, dataset, threshold, persist):
        extract_raster_features.run(str(JOB_ID))

    threshold.assert_not_called()
    persist.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert "organization mismatch" in job.logs
    assert item.s3_uri == SOURCE_URI
    assert dataset.status == DatasetStatus.READY


def test_worker_invalid_config_is_permanent():
    world = _world()
    world[2].config = _config(threshold_min=None)
    with _patches(world) as (_s, job, _i, _d, threshold, _p):
        extract_raster_features.run(str(JOB_ID))

    threshold.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert "invalid extract config" in job.logs


def test_worker_retry_reuses_annotation_set_without_new_polygons():
    world = _world()
    _worker_cm, session, job, _item, _dataset = world
    existing = MagicMock()
    existing.id = SET_ID
    existing.organization_id = ORG_ID
    existing.deleted_at = None
    job.config = _config(output_annotation_set_id=str(SET_ID))
    session.scalar.return_value = 2

    def _get(model, pk):
        if model is AnnotationSet and pk == SET_ID:
            return existing
        if model is Job:
            return job
        return None

    session.get.side_effect = _get
    with _patches(world) as (session, job, _i, _d, threshold, persist):
        extract_raster_features.run(str(JOB_ID))

    threshold.assert_not_called()
    persist.assert_not_called()
    assert job.status == JobStatus.COMPLETED
    assert job.config["result"]["feature_count"] == 2
    assert job.config["result"]["annotation_set_id"] == str(SET_ID)
    added_sets = [
        c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], AnnotationSet)
    ]
    assert added_sets == []


def test_worker_transient_error_retries_without_failing_job():
    world = _world()
    with _patches(world, threshold_error=RuntimeError("minio unavailable")) as (
        _s, job, _i, _d, _t, _p,
    ):
        with pytest.raises(Exception, match="minio unavailable"):
            extract_raster_features.run(str(JOB_ID))

    assert job.status != JobStatus.FAILED
