"""Worker tests for rasterize_annotation_set."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.core.enums import DatasetStatus, DatasetType, JobStatus
from app.models.annotation_set import AnnotationSet
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.services.conversion.rasterize_mask import RasterizeResult
from app.workers.ingestion.tasks import rasterize_annotation_set

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
SET_ID = UUID("00000000-0000-0000-0000-000000000040")
REF_DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000020")
CLASS_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
USER_ID = UUID("00000000-0000-0000-0000-000000000030")
REF_URI = "s3://org-bucket/datasets/reference.tif"


def _config(**overrides):
    cfg = {
        "trigger": "api",
        "annotation_set_ids": [str(SET_ID)],
        "reference_dataset_id": str(REF_DATASET_ID),
        "resolution_m": 2.5,
        "dataset_name": "GT mask",
    }
    cfg.update(overrides)
    return cfg


def _result() -> RasterizeResult:
    return RasterizeResult(
        schema_id=SCHEMA_ID,
        value_class_map={"1": str(CLASS_ID)},
        class_names={str(CLASS_ID): "apple"},
        pixel_counts={str(CLASS_ID): 4},
        width=8,
        height=6,
        crs="EPSG:32632",
        feature_count=2,
    )


def _world(**config_overrides):
    session = MagicMock()
    job = MagicMock()
    job.id = JOB_ID
    job.organization_id = ORG_ID
    job.created_by_user_id = USER_ID
    job.type = "rasterize_annotation_set"
    job.config = _config(**config_overrides)
    job.logs = None
    job.status = JobStatus.QUEUED
    job.started_at = None

    ann_set = SimpleNamespace(id=SET_ID, organization_id=ORG_ID, name="Ground truth")
    ref_item = SimpleNamespace(s3_uri=REF_URI)
    output_item = SimpleNamespace(properties_cache=None)
    seen = {}

    def _get(model, pk):
        if model is Job and pk == JOB_ID:
            return job
        return None

    def _execute(stmt):
        entity = stmt.column_descriptions[0]["entity"]
        result = MagicMock()
        if entity is AnnotationSet:
            result.scalars.return_value.all.return_value = [ann_set]
            return result
        if entity is DatasetItem:
            seen["item_queries"] = seen.get("item_queries", 0) + 1
            if job.config.get("reference_dataset_id") and seen["item_queries"] == 1:
                result.scalar_one_or_none.return_value = ref_item
            else:
                result.scalar_one_or_none.return_value = output_item
            return result
        raise AssertionError(f"unexpected query entity {entity}")

    session.get.side_effect = _get
    session.execute.side_effect = _execute

    worker_cm = MagicMock()
    worker_cm.__enter__.return_value = session
    worker_cm.__exit__.return_value = False
    return worker_cm, session, job, ann_set, output_item, seen


@contextmanager
def _patches(world, *, rasterize_error=None):
    worker_cm, session, job, ann_set, output_item, seen = world
    raster_result = _result()
    paths = []

    def _rasterize(*args, **kwargs):
        paths.append(args[2])
        assert job.status == JobStatus.RUNNING
        assert job.started_at is not None
        if rasterize_error is not None:
            raise rasterize_error
        return raster_result

    def _upload(*args, **kwargs):
        datasets = [
            call.args[0]
            for call in session.add.call_args_list
            if isinstance(call.args[0], Dataset)
        ]
        seen["upload_status"] = datasets[0].status
        seen["upload_type"] = datasets[0].dataset_type

    storage = MagicMock()
    storage.object_key.return_value = "datasets/out.tif"
    storage.bucket_name.return_value = "org-bucket"
    storage.upload_from_path.side_effect = _upload
    resume = MagicMock()

    with (
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm),
        patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}),
        patch("app.workers.ingestion.tasks.storage_service", storage),
        patch("app.services.conversion.rasterize_annotation_sets_to_cog", _rasterize),
        patch("app.workers.ingestion.tasks._ensure_collection", return_value="col-1") as ensure,
        patch(
            "app.workers.ingestion.tasks._ingest_single_cog",
            return_value=(True, [], "stac-item-1", {"properties": {}, "geometry": None}),
        ) as ingest,
        patch("app.workers.ingestion.tasks._upsert_dataset_item") as upsert,
        patch("app.workers.ingestion.tasks._compute_aggregated_metadata", return_value=None),
        patch("app.workers.automation.tasks.resume_after_job", resume),
    ):
        yield {
            "session": session,
            "job": job,
            "ann_set": ann_set,
            "output_item": output_item,
            "seen": seen,
            "paths": paths,
            "storage": storage,
            "ensure": ensure,
            "ingest": ingest,
            "upsert": upsert,
            "resume": resume,
            "result": raster_result,
        }


def _dataset(session) -> Dataset:
    datasets = [
        call.args[0]
        for call in session.add.call_args_list
        if isinstance(call.args[0], Dataset)
    ]
    assert len(datasets) == 1
    return datasets[0]


def test_worker_completes_segmentation_mask_dataset():
    world = _world()
    with _patches(world) as ctx:
        rasterize_annotation_set.run(str(JOB_ID))
        job = ctx["job"]
        dataset = _dataset(ctx["session"])
        out_path = ctx["paths"][0]

    assert ctx["seen"]["upload_status"] == DatasetStatus.INGESTING
    assert ctx["seen"]["upload_type"] == DatasetType.SEGMENTATION_MASK.value
    assert dataset.dataset_type == DatasetType.SEGMENTATION_MASK.value
    assert dataset.status == DatasetStatus.READY
    assert dataset.name == "GT mask"
    assert dataset.organization_id == ORG_ID
    class_map = dataset.metadata_["rendering_config"]["class_map"]
    assert class_map["value_class_map"] == {"1": str(CLASS_ID)}
    assert class_map["schema_id"] == str(SCHEMA_ID)
    assert class_map["band_index"] == 1
    assert class_map["nodata_value"] == 0
    assert ctx["output_item"].properties_cache["rendering_config"]["class_map"] == class_map

    result = job.config["result"]
    assert result["dataset_id"] == str(dataset.id)
    assert result["value_class_map"] == {"1": str(CLASS_ID)}
    assert result["feature_count"] == 2
    assert result["width"] == 8
    assert result["height"] == 6
    assert result["crs"] == "EPSG:32632"
    assert job.status == JobStatus.COMPLETED
    assert job.processed_items == 1
    assert job.progress == 1.0

    ctx["storage"].ensure_org_bucket.assert_called_once_with(ORG_ID)
    ctx["storage"].upload_from_path.assert_called_once()
    assert ctx["storage"].upload_from_path.call_args.kwargs["content_type"] == "image/tiff"
    ctx["ensure"].assert_called_once()
    ctx["ingest"].assert_called_once()
    assert ctx["ingest"].call_args.args[4] == DatasetType.SEGMENTATION_MASK.value
    ctx["upsert"].assert_called_once()
    assert ctx["upsert"].call_args.kwargs["dataset_id"] == dataset.id
    assert ctx["upsert"].call_args.kwargs["stac_item_id"] == "stac-item-1"
    ctx["resume"].delay.assert_not_called()

    import os

    assert not os.path.exists(out_path)


def test_worker_failure_marks_job_failed_and_removes_temp_file():
    world = _world(
        automation_run_id="run-1",
        automation_step_id="step-1",
    )
    with _patches(world, rasterize_error=ValueError("cog upload failed")) as ctx:
        with pytest.raises(ValueError, match="cog upload failed"):
            rasterize_annotation_set.run(str(JOB_ID))
        out_path = ctx["paths"][0]

    assert ctx["job"].status == JobStatus.FAILED
    assert "cog upload failed" in ctx["job"].logs
    ctx["storage"].upload_from_path.assert_not_called()
    ctx["ensure"].assert_not_called()
    ctx["resume"].delay.assert_not_called()

    import os

    assert not os.path.exists(out_path)


def test_worker_resumes_automation_after_success():
    world = _world(automation_run_id="run-1", automation_step_id="step-1")
    with _patches(world) as ctx:
        rasterize_annotation_set.run(str(JOB_ID))
        dataset = _dataset(ctx["session"])

    ctx["resume"].delay.assert_called_once()
    job_id, output = ctx["resume"].delay.call_args.args
    assert job_id == str(JOB_ID)
    assert output["dataset"]["id"] == str(dataset.id)
    assert output["dataset"]["name"] == "GT mask"
    assert output["dataset"]["dataset_type"] == DatasetType.SEGMENTATION_MASK.value
    assert output["dataset"]["value_class_map"] == {"1": str(CLASS_ID)}
    assert output["dataset"]["class_map"]["nodata_value"] == 0


def test_worker_org_mismatch_fails_before_rasterizing():
    world = _world()
    world[3].organization_id = OTHER_ORG_ID
    with _patches(world) as ctx:
        with pytest.raises(ValueError, match="does not belong"):
            rasterize_annotation_set.run(str(JOB_ID))

    assert ctx["paths"] == []
    assert ctx["job"].status == JobStatus.FAILED
    ctx["storage"].upload_from_path.assert_not_called()
    ctx["resume"].delay.assert_not_called()
