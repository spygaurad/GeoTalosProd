"""Raster reprojection job: object key, materialize helper, worker and API."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.endpoints.jobs import create_reproject_raster_job
from app.core.enums import DatasetStatus, DatasetType, JobStatus, JobType
from app.models.dataset import Dataset
from app.models.job import Job
from app.models.job_output import JobOutput
from app.models.project import Project
from app.schemas.job import ReprojectRasterJobCreate
from app.workers.ingestion.tasks import (
    PermanentTaskError,
    _materialize_reprojected_cog,
    _reprojected_cog_object_key,
    reproject_raster,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000022")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
ITEM_A = UUID("00000000-0000-0000-0000-000000000010")
ITEM_B = UUID("00000000-0000-0000-0000-000000000011")
BUCKET = f"org-{ORG_ID}"
SOURCE_URI = f"s3://{BUCKET}/datasets/{SOURCE_ID}/source.tif"

_STAC_ITEM = {
    "geometry": None,
    "properties": {"datetime": "2020-01-01T00:00:00Z"},
}
_AGG = {
    "metadata": {"band_count": ["uint8"], "file_count": 1},
    "wkt": None,
    "start_date": None,
    "end_date": None,
}


def _item(*, item_id=ITEM_A, filename="source.tif", rendering=None):
    item = MagicMock()
    item.id = item_id
    item.dataset_id = SOURCE_ID
    item.organization_id = ORG_ID
    item.filename = filename
    item.s3_uri = SOURCE_URI
    item.is_active = True
    item.properties_cache = (
        {"rendering_config": rendering} if rendering is not None else {}
    )
    return item


def _mock_session(*, item_ids, output_id=OUTPUT_ID):
    session = MagicMock()
    job = MagicMock()
    job.id = JOB_ID
    job.organization_id = ORG_ID
    job.created_by_user_id = None
    job.config = {
        "source_dataset_id": str(SOURCE_ID),
        "output_dataset_id": str(output_id),
        "dataset_item_ids": [str(i) for i in item_ids],
        "dst_crs": "EPSG:3857",
        "resampling": "bilinear",
    }
    job.logs = None
    job.status = JobStatus.QUEUED
    job.started_at = None
    job.processed_items = 0
    job.total_items = 0
    job.failed_items = 0
    job.progress = 0.0

    source = MagicMock()
    source.id = SOURCE_ID
    source.organization_id = ORG_ID
    source.dataset_type = "imagery"
    source.name = "Source"
    source.description = None
    source.status = DatasetStatus.READY
    source.deleted_at = None
    source.metadata_ = {"rendering_config": {"rescale": ["0,255"]}}

    output = MagicMock()
    output.id = output_id
    output.organization_id = ORG_ID
    output.dataset_type = "imagery"
    output.name = "Source · EPSG:3857"
    output.status = DatasetStatus.INGESTING
    output.stac_collection_id = "col-out"
    output.metadata_ = None
    output.deleted_at = None

    def _get(model, pk):
        if model is Job:
            return job
        if model is Dataset:
            return {SOURCE_ID: source, output_id: output}.get(pk)
        return None

    session.get.side_effect = _get
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute.return_value = exec_result

    worker_cm = MagicMock()
    worker_cm.__enter__.return_value = session
    worker_cm.__exit__.return_value = False
    return worker_cm, session, job, source, output


@contextmanager
def _reproject_patches(*, items, already_done=None, materialize=None, prepare=None, extra=None):
    worker_cm, session, job, source, output = _mock_session(item_ids=[it.id for it in items])
    if materialize is None:
        materialize = MagicMock(
            return_value=f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_source_EPSG_3857.tif"
        )
    if prepare is None:
        prepare = MagicMock(return_value=(True, [], "stac-out-1", dict(_STAC_ITEM)))
    stack = ExitStack()
    tasks = "app.workers.ingestion.tasks"
    stack.enter_context(patch(f"{tasks}.WorkerSession", return_value=worker_cm))
    stack.enter_context(patch(f"{tasks}.storage_service.bucket_name", return_value=BUCKET))
    stack.enter_context(patch(f"{tasks}.storage_service.ensure_org_bucket"))
    stack.enter_context(patch(f"{tasks}._gdal_env_for_worker", return_value={}))
    stack.enter_context(patch(f"{tasks}._active_source_items", return_value=list(items)))
    stack.enter_context(
        patch(f"{tasks}._output_items_by_source_id", return_value=already_done or {})
    )
    stack.enter_context(patch(f"{tasks}._materialize_reprojected_cog", materialize))
    stack.enter_context(patch(f"{tasks}._prepare_single_cog", prepare))
    stack.enter_context(patch(f"{tasks}.batch_upsert_stac_items"))
    stack.enter_context(patch(f"{tasks}._upsert_dataset_item"))
    stack.enter_context(patch(f"{tasks}._compute_aggregated_metadata", return_value=_AGG))
    stack.enter_context(patch(f"{tasks}._publish_job_event"))
    for extra_patch in extra or []:
        stack.enter_context(extra_patch)
    with stack:
        yield session, job, source, output, materialize


# ── helpers ──────────────────────────────────────────────────────────────────


def test_reprojected_object_key_includes_source_item_and_crs():
    key = _reprojected_cog_object_key(OUTPUT_ID, ITEM_A, "My Scene.tif", "EPSG:3857")

    assert key.startswith(f"datasets/{OUTPUT_ID}/")
    assert key.endswith(f"processed/{ITEM_A}_My_Scene_EPSG_3857.tif")


def test_materialize_reprojects_uploads_and_cleans_temp():
    seen = {}

    def _reproject(src, out, dst_crs, *, resampling, gdal_env):
        seen.update(src=src, out=out, dst_crs=dst_crs, resampling=resampling)
        with open(out, "wb") as f:
            f.write(b"cog")
        return out

    upload = MagicMock()
    with (
        patch("app.workers.ingestion.tasks.reproject_raster_to_cog", _reproject),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", upload),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        dest_uri = _materialize_reprojected_cog(
            organization_id=ORG_ID,
            output_dataset_id=OUTPUT_ID,
            source_item_id=ITEM_A,
            s3_uri=SOURCE_URI,
            filename="source.tif",
            dst_crs="EPSG:3857",
            resampling="nearest",
            gdal_env={},
        )

    assert seen["src"] == SOURCE_URI
    assert seen["dst_crs"] == "EPSG:3857"
    assert seen["resampling"] == "nearest"
    upload.assert_called_once()
    assert upload.call_args.kwargs["content_type"] == "image/tiff"
    assert dest_uri == f"s3://{BUCKET}/{upload.call_args.args[1]}"
    import os

    assert not os.path.exists(seen["out"])


def test_materialize_conversion_error_is_permanent():
    upload = MagicMock()
    with (
        patch(
            "app.workers.ingestion.tasks.reproject_raster_to_cog",
            side_effect=ValueError("Reprojection failed: unreadable raster"),
        ),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", upload),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        with pytest.raises(PermanentTaskError, match="unreadable raster"):
            _materialize_reprojected_cog(
                organization_id=ORG_ID,
                output_dataset_id=OUTPUT_ID,
                source_item_id=ITEM_A,
                s3_uri=SOURCE_URI,
                filename="source.tif",
                dst_crs="EPSG:3857",
                resampling="nearest",
                gdal_env={},
            )
    upload.assert_not_called()


# ── worker ───────────────────────────────────────────────────────────────────


def test_worker_reprojects_into_new_dataset_and_does_not_mutate_source():
    with _reproject_patches(items=[_item()]) as (session, job, source, output, materialize):
        reproject_raster.run(str(JOB_ID))

    materialize.assert_called_once()
    kwargs = materialize.call_args.kwargs
    assert kwargs["output_dataset_id"] == OUTPUT_ID
    assert kwargs["source_item_id"] == ITEM_A
    assert kwargs["dst_crs"] == "EPSG:3857"
    assert kwargs["resampling"] == "bilinear"
    assert source.status == DatasetStatus.READY
    assert output.status == DatasetStatus.READY
    assert job.status == JobStatus.COMPLETED
    result = job.config["result"]
    assert result["dataset_id"] == str(OUTPUT_ID)
    assert result["dst_crs"] == "EPSG:3857"
    assert result["reprojected_count"] == 1
    assert output.metadata_["rendering_config"]["rescale"] == ["0,255"]
    assert any(isinstance(c.args[0], JobOutput) for c in session.add.call_args_list)


def test_worker_multi_item_registers_all_before_ready():
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]
    prepare_calls = []

    def _prepare(s3_uri, filename, collection, s3_config, dataset_type="imagery"):
        prepare_calls.append(filename)
        return True, [], f"stac-{filename}", dict(_STAC_ITEM)

    materialize = MagicMock(side_effect=lambda **kw: f"s3://{BUCKET}/{kw['source_item_id']}.tif")
    with _reproject_patches(items=items, materialize=materialize, prepare=_prepare) as (
        _s, job, _src, output, _m,
    ):
        reproject_raster.run(str(JOB_ID))

    assert prepare_calls == ["a.tif", "b.tif"]
    assert job.status == JobStatus.COMPLETED
    assert job.processed_items == 2
    assert output.status == DatasetStatus.READY
    assert len(job.config["result"]["items"]) == 2


def test_worker_partial_failure_marks_output_failed_not_ready():
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]

    def _mat(**kwargs):
        if kwargs["source_item_id"] == ITEM_B:
            raise PermanentTaskError("Reprojection failed: boom")
        return f"s3://{BUCKET}/a.tif"

    batch = MagicMock()
    with _reproject_patches(
        items=items,
        materialize=_mat,
        extra=[patch("app.workers.ingestion.tasks.batch_upsert_stac_items", batch)],
    ) as (_s, job, source, output, _m):
        reproject_raster.run(str(JOB_ID))

    batch.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert output.status == DatasetStatus.FAILED
    assert source.status == DatasetStatus.READY


def test_worker_retry_reuses_output_and_skips_registered_items():
    done_item = MagicMock()
    done_item.s3_uri = f"s3://{BUCKET}/a.tif"
    done_item.stac_item_id = "stac-a"
    done_item.geometry = None
    done_item.properties_cache = {"source_item_id": str(ITEM_A)}
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]
    materialize = MagicMock(return_value=f"s3://{BUCKET}/b.tif")
    upsert = MagicMock()
    with _reproject_patches(
        items=items,
        already_done={str(ITEM_A): done_item},
        materialize=materialize,
        extra=[patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert)],
    ) as (session, job, _src, _out, _m):
        reproject_raster.run(str(JOB_ID))

    materialize.assert_called_once()
    assert materialize.call_args.kwargs["source_item_id"] == ITEM_B
    upsert.assert_called_once()
    assert job.status == JobStatus.COMPLETED
    assert [c for c in session.add.call_args_list if isinstance(c.args[0], Dataset)] == []


def test_worker_creates_output_named_after_target_crs():
    with _reproject_patches(
        items=[_item()],
        extra=[patch("app.workers.ingestion.tasks._ensure_collection", return_value="col-new")],
    ) as (session, job, source, _out, _m):
        job.config.pop("output_dataset_id")
        reproject_raster.run(str(JOB_ID))

    created = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], Dataset)]
    assert len(created) == 1
    assert created[0].name == "Source · EPSG:3857"
    assert created[0].dataset_type == source.dataset_type


def test_worker_missing_target_crs_is_permanent():
    materialize = MagicMock()
    with _reproject_patches(items=[_item()], materialize=materialize) as (_s, job, _src, _o, _m):
        job.config.pop("dst_crs")
        reproject_raster.run(str(JOB_ID))

    materialize.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert "dst_crs is required" in job.logs


def test_worker_org_mismatch_is_permanent_and_skips_io():
    materialize = MagicMock()
    with _reproject_patches(items=[_item()], materialize=materialize) as (_s, job, source, _o, _m):
        source.organization_id = OTHER_ORG_ID
        reproject_raster.run(str(JOB_ID))

    materialize.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert "organization mismatch" in job.logs


def test_worker_resume_after_job_when_automation_keys_present():
    delay = MagicMock()
    with _reproject_patches(
        items=[_item()],
        extra=[patch("app.workers.automation.tasks.resume_after_job.delay", delay)],
    ) as (_s, job, _src, _o, _m):
        job.config["automation_run_id"] = str(uuid4())
        job.config["automation_step_id"] = str(uuid4())
        reproject_raster.run(str(JOB_ID))

    delay.assert_called_once()
    assert delay.call_args.args[0] == str(JOB_ID)
    assert delay.call_args.args[1]["dataset"]["id"] == str(OUTPUT_ID)


def test_worker_transient_error_retries_without_failing_job():
    materialize = MagicMock(side_effect=RuntimeError("minio unavailable"))
    with _reproject_patches(items=[_item()], materialize=materialize) as (_s, job, _src, output, _m):
        with pytest.raises(Exception, match="minio unavailable"):
            reproject_raster.run(str(JOB_ID))

    assert job.status != JobStatus.FAILED
    assert output.status == DatasetStatus.INGESTING


# ── API ──────────────────────────────────────────────────────────────────────


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeDB:
    def __init__(self, dataset, items, project=None):
        self.dataset = dataset
        self.items = items
        self.project = project
        self.added = []
        self.committed = False

    async def scalar(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Dataset:
            return self.dataset
        if entity is Project:
            return self.project
        return None

    async def scalars(self, stmt):
        return _ScalarListResult(self.items)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


def _dataset(*, status=DatasetStatus.READY, dataset_type=DatasetType.IMAGERY):
    return SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=status,
        dataset_type=dataset_type,
    )


def _api_item():
    return SimpleNamespace(id=ITEM_A, organization_id=ORG_ID, dataset_id=SOURCE_ID, is_active=True)


async def _create(db, **payload):
    return await create_reproject_raster_job(
        payload=ReprojectRasterJobCreate(dataset_id=SOURCE_ID, **payload),
        org_id=ORG_ID,
        db=db,
        current_user=SimpleNamespace(id=uuid4()),
    )


@pytest.fixture()
def enqueue(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.reproject_raster.apply_async",
        lambda *a, **kw: calls.append((a, kw)),
    )
    return calls


def test_schema_requires_target_crs():
    with pytest.raises(ValidationError):
        ReprojectRasterJobCreate(dataset_id=SOURCE_ID)
    with pytest.raises(ValidationError):
        ReprojectRasterJobCreate(dataset_id=SOURCE_ID, dst_crs="EPSG:3857", resampling="lanczos")


@pytest.mark.asyncio
async def test_api_creates_queued_job_after_commit(monkeypatch):
    db = _FakeDB(dataset=_dataset(), items=[_api_item()])
    calls = []

    def _apply_async(*args, **kwargs):
        assert db.committed is True
        calls.append(kwargs)

    monkeypatch.setattr("app.workers.ingestion.tasks.reproject_raster.apply_async", _apply_async)

    job = await _create(db, dst_crs="3857", dataset_name="  Web Mercator  ")

    assert job.type == JobType.REPROJECT_RASTER
    assert job.status == JobStatus.QUEUED
    assert job.config["trigger"] == "api"
    assert job.config["source_dataset_id"] == str(SOURCE_ID)
    assert job.config["dataset_item_ids"] == [str(ITEM_A)]
    assert job.config["dst_crs"] == "EPSG:3857"
    assert job.config["resampling"] == "bilinear"
    assert job.config["dataset_name"] == "Web Mercator"
    assert job.input_refs == [{"type": "dataset", "id": str(SOURCE_ID)}]
    assert calls[0]["args"] == [str(job.id)]


@pytest.mark.asyncio
async def test_api_mask_defaults_to_nearest(enqueue):
    db = _FakeDB(dataset=_dataset(dataset_type=DatasetType.SEGMENTATION_MASK), items=[_api_item()])

    job = await _create(db, dst_crs="EPSG:3857")

    assert job.config["resampling"] == "nearest"
    assert enqueue


@pytest.mark.asyncio
async def test_api_mask_with_bilinear_is_422(enqueue):
    db = _FakeDB(dataset=_dataset(dataset_type=DatasetType.SEGMENTATION_MASK), items=[_api_item()])

    with pytest.raises(HTTPException) as exc:
        await _create(db, dst_crs="EPSG:3857", resampling="bilinear")

    assert exc.value.status_code == 422
    assert enqueue == []


@pytest.mark.asyncio
async def test_api_invalid_crs_is_422(enqueue):
    db = _FakeDB(dataset=_dataset(), items=[_api_item()])

    with pytest.raises(HTTPException) as exc:
        await _create(db, dst_crs="EPSG:999999")

    assert exc.value.status_code == 422
    assert "Unsupported CRS" in exc.value.detail
    assert enqueue == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dataset, items, payload, status_code",
    [
        (None, [], {}, 404),
        (_dataset(status=DatasetStatus.INGESTING), [_api_item()], {}, 409),
        (_dataset(), [], {}, 422),
        (_dataset(), [_api_item()], {"dataset_item_ids": [ITEM_A, ITEM_B]}, 404),
        (_dataset(), [_api_item()], {"project_id": uuid4()}, 404),
    ],
    ids=["missing-dataset", "ingesting", "no-items", "missing-subset-item", "unknown-project"],
)
async def test_api_rejections(enqueue, dataset, items, payload, status_code):
    db = _FakeDB(dataset=dataset, items=items)

    with pytest.raises(HTTPException) as exc:
        await _create(db, dst_crs="EPSG:3857", **payload)

    assert exc.value.status_code == status_code
    assert enqueue == []
    assert db.committed is False
