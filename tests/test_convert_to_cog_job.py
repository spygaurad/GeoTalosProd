"""Standalone GeoTIFF → COG job (Milestone 2, PR 2)."""

from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from pydantic import ValidationError
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.api.v1.endpoints.jobs import create_convert_to_cog_job
from app.core.enums import DatasetStatus, JobStatus, JobType
from app.models.dataset import Dataset
from app.models.job import Job
from app.models.job_output import JobOutput
from app.models.project import Project
from app.schemas.job import ConvertToCogJobCreate
from app.services import storage_service
from app.workers.ingestion.tasks import (
    PermanentTaskError,
    _materialize_standalone_cog,
    _s3_key_from_uri,
    _standalone_cog_object_key,
    convert_dataset_to_cog,
)

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
SOURCE_ID = UUID("00000000-0000-0000-0000-000000000002")
OUTPUT_ID = UUID("00000000-0000-0000-0000-000000000022")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
ITEM_A = UUID("00000000-0000-0000-0000-000000000010")
ITEM_B = UUID("00000000-0000-0000-0000-000000000011")
BUCKET = f"org-{ORG_ID}"
SOURCE_KEY = f"datasets/{SOURCE_ID}/source.tif"
SOURCE_URI = f"s3://{BUCKET}/{SOURCE_KEY}"

_TRANSFORM = from_origin(-10.0, 10.0, 1.0, 1.0)
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


def _write_geotiff(path, data, *, crs="EPSG:4326", tiled: bool = False):
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


def _item(*, item_id=ITEM_A, filename="source.tif", s3_uri=SOURCE_URI, rendering=None):
    item = MagicMock()
    item.id = item_id
    item.dataset_id = SOURCE_ID
    item.organization_id = ORG_ID
    item.filename = filename
    item.s3_uri = s3_uri
    item.is_active = True
    item.properties_cache = (
        {"rendering_config": rendering} if rendering is not None else {}
    )
    return item


def _mock_convert_session(*, source_org=ORG_ID, output_id=OUTPUT_ID, item_ids=None):
    session = MagicMock()
    job = MagicMock()
    job.id = JOB_ID
    job.organization_id = ORG_ID
    job.created_by_user_id = None
    job.config = {
        "source_dataset_id": str(SOURCE_ID),
        "output_dataset_id": str(output_id),
        "dataset_item_ids": [str(i) for i in (item_ids or [ITEM_A])],
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
    source.organization_id = source_org
    source.dataset_type = "imagery"
    source.name = "Source"
    source.description = None
    source.status = DatasetStatus.READY
    source.deleted_at = None
    source.metadata_ = {"rendering_config": {"rescale": ["0,255"]}}
    source.stac_collection_id = "col-src"

    output = MagicMock()
    output.id = output_id
    output.organization_id = ORG_ID
    output.dataset_type = "imagery"
    output.name = "Source · COG"
    output.status = DatasetStatus.INGESTING
    output.stac_collection_id = "col-out"
    output.metadata_ = None
    output.deleted_at = None

    def _get(model, pk):
        if model is Job:
            return job
        if model is Dataset:
            if pk == SOURCE_ID:
                return source
            if pk == output_id:
                return output
            return None
        return None

    session.get.side_effect = _get
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    session.execute.return_value = exec_result

    worker_cm = MagicMock()
    worker_cm.__enter__.return_value = session
    worker_cm.__exit__.return_value = False
    return worker_cm, session, job, source, output


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

    def _entity(self, stmt):
        try:
            return stmt.column_descriptions[0]["entity"]
        except Exception:
            return None

    async def scalar(self, stmt):
        ent = self._entity(stmt)
        if ent is Dataset:
            return self.dataset
        if ent is Project:
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


@contextmanager
def _convert_patches(
    *,
    items,
    already_done=None,
    materialize=None,
    prepare=None,
    extra=None,
):
    worker_cm, session, job, source, output = _mock_convert_session(
        item_ids=[it.id for it in items],
    )
    if materialize is None:
        materialize = MagicMock(
            return_value=(
                f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_source_cog.tif",
                False,
            )
        )
    if prepare is None:
        prepare = MagicMock(return_value=(True, [], "stac-out-1", dict(_STAC_ITEM)))
    stack = ExitStack()
    stack.enter_context(
        patch("app.workers.ingestion.tasks.WorkerSession", return_value=worker_cm)
    )
    stack.enter_context(
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET)
    )
    stack.enter_context(patch("app.workers.ingestion.tasks.storage_service.ensure_org_bucket"))
    stack.enter_context(patch("app.workers.ingestion.tasks._gdal_env_for_worker", return_value={}))
    stack.enter_context(
        patch("app.workers.ingestion.tasks._active_source_items", return_value=list(items))
    )
    stack.enter_context(
        patch(
            "app.workers.ingestion.tasks._output_items_by_source_id",
            return_value=already_done or {},
        )
    )
    stack.enter_context(
        patch("app.workers.ingestion.tasks._materialize_standalone_cog", materialize)
    )
    stack.enter_context(patch("app.workers.ingestion.tasks._prepare_single_cog", prepare))
    stack.enter_context(patch("app.workers.ingestion.tasks.batch_upsert_stac_items"))
    stack.enter_context(patch("app.workers.ingestion.tasks._upsert_dataset_item"))
    stack.enter_context(
        patch("app.workers.ingestion.tasks._compute_aggregated_metadata", return_value=_AGG)
    )
    stack.enter_context(patch("app.workers.ingestion.tasks._publish_job_event"))
    for extra_patch in extra or []:
        stack.enter_context(extra_patch)
    with stack:
        yield session, job, source, output, materialize


# ── helpers ──────────────────────────────────────────────────────────────────


def test_s3_key_from_uri_and_standalone_object_key():
    assert _s3_key_from_uri(SOURCE_URI) == SOURCE_KEY
    key = _standalone_cog_object_key(OUTPUT_ID, ITEM_A, "My File.tif")
    assert key == f"datasets/{OUTPUT_ID}/processed/{ITEM_A}_My_File_cog.tif"
    other = _standalone_cog_object_key(OUTPUT_ID, ITEM_B, "My File.tif")
    assert key != other


def test_s3_key_from_uri_rejects_non_s3():
    with pytest.raises(PermanentTaskError, match="Expected s3://"):
        _s3_key_from_uri("/tmp/local.tif")


def test_copy_object_noops_on_same_key_and_rejects_empty():
    with patch("app.services.storage_service._s3_client") as client_factory:
        storage_service.copy_object(ORG_ID, "datasets/a/x.tif", "datasets/a/x.tif")
        client_factory.assert_not_called()
    with pytest.raises(ValueError, match="source_key"):
        storage_service.copy_object(ORG_ID, "", "datasets/a/y.tif")


def test_copy_object_copies_within_org_bucket():
    client = MagicMock()
    with (
        patch("app.services.storage_service._s3_client", return_value=client),
        patch("app.services.storage_service.bucket_name", return_value=BUCKET),
    ):
        storage_service.copy_object(ORG_ID, SOURCE_KEY, "datasets/out/cog.tif")
    client.copy_object.assert_called_once_with(
        Bucket=BUCKET,
        Key="datasets/out/cog.tif",
        CopySource={"Bucket": BUCKET, "Key": SOURCE_KEY},
        ContentType="image/tiff",
        MetadataDirective="REPLACE",
    )


def test_materialize_already_cog_copies_and_skips_converter(tmp_path):
    copied = {}

    def _copy(org_id, source_key, dest_key, content_type="image/tiff"):
        copied["org_id"] = org_id
        copied["source_key"] = source_key
        copied["dest_key"] = dest_key
        copied["content_type"] = content_type

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, True, []),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
        patch("app.workers.ingestion.tasks.storage_service.copy_object", side_effect=_copy),
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        dest, was_cog = _materialize_standalone_cog(
            organization_id=ORG_ID,
            output_dataset_id=OUTPUT_ID,
            source_item_id=ITEM_A,
            s3_uri=SOURCE_URI,
            filename="source.tif",
            gdal_env={},
        )

    convert.assert_not_called()
    upload.assert_not_called()
    assert was_cog is True
    assert copied["org_id"] == ORG_ID
    assert copied["source_key"] == SOURCE_KEY
    assert copied["dest_key"] == f"datasets/{OUTPUT_ID}/processed/{ITEM_A}_source_cog.tif"
    assert dest == f"s3://{BUCKET}/{copied['dest_key']}"


def test_materialize_non_cog_converts_uploads_and_cleans_temp(tmp_path):
    src = tmp_path / "source.tif"
    _write_geotiff(src, np.arange(20, dtype=np.uint8).reshape(4, 5))
    uploaded = {}

    def _upload(org_id, key, path, content_type="image/tiff"):
        uploaded["org_id"] = org_id
        uploaded["key"] = key
        uploaded["exists"] = os.path.isfile(path)
        uploaded["path"] = path

    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(True, False, ["No compression"]),
        ),
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path", side_effect=_upload),
        patch("app.workers.ingestion.tasks.storage_service.copy_object") as copy_obj,
        patch("app.workers.ingestion.tasks.storage_service.bucket_name", return_value=BUCKET),
    ):
        dest, was_cog = _materialize_standalone_cog(
            organization_id=ORG_ID,
            output_dataset_id=OUTPUT_ID,
            source_item_id=ITEM_A,
            s3_uri=str(src),
            filename="My File.tif",
            gdal_env={},
        )

    assert was_cog is False
    copy_obj.assert_not_called()
    assert uploaded["org_id"] == ORG_ID
    assert uploaded["key"] == f"datasets/{OUTPUT_ID}/processed/{ITEM_A}_My_File_cog.tif"
    assert uploaded["exists"] is True
    assert dest.endswith(uploaded["key"])
    assert not os.path.exists(uploaded["path"])


def test_materialize_invalid_raster_is_permanent_and_does_not_write():
    with (
        patch(
            "app.workers.ingestion.tasks.is_cloud_optimized_geotiff",
            return_value=(False, False, ["File has no coordinate reference system"]),
        ),
        patch("app.workers.ingestion.tasks.convert_geotiff_to_cog") as convert,
        patch("app.workers.ingestion.tasks.storage_service.upload_from_path") as upload,
        patch("app.workers.ingestion.tasks.storage_service.copy_object") as copy_obj,
    ):
        with pytest.raises(PermanentTaskError, match="coordinate reference system"):
            _materialize_standalone_cog(
                organization_id=ORG_ID,
                output_dataset_id=OUTPUT_ID,
                source_item_id=ITEM_A,
                s3_uri=SOURCE_URI,
                filename="source.tif",
                gdal_env={},
            )
    convert.assert_not_called()
    upload.assert_not_called()
    copy_obj.assert_not_called()


# ── worker ───────────────────────────────────────────────────────────────────


def test_worker_converts_non_cog_and_does_not_mutate_source():
    item = _item()
    dest_uri = f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_source_cog.tif"
    materialize = MagicMock(return_value=(dest_uri, False))
    with _convert_patches(items=[item], materialize=materialize) as (session, job, source, output, _mat):
        convert_dataset_to_cog.run(str(JOB_ID))

    materialize.assert_called_once()
    assert materialize.call_args.kwargs["output_dataset_id"] == OUTPUT_ID
    assert materialize.call_args.kwargs["source_item_id"] == ITEM_A
    assert source.status == DatasetStatus.READY
    assert output.status == DatasetStatus.READY
    assert job.status == JobStatus.COMPLETED
    assert job.config["result"]["dataset_id"] == str(OUTPUT_ID)
    assert job.config["result"]["converted_count"] == 1
    assert job.config["result"]["skipped_already_cog"] == 0
    assert job.config["result"]["items"][0]["s3_uri"] == dest_uri
    assert output.metadata_["rendering_config"]["rescale"] == ["0,255"]
    assert any(
        isinstance(c.args[0], JobOutput) for c in session.add.call_args_list
    )


def test_worker_already_cog_skips_conversion_via_materialize_flag():
    item = _item()
    dest_uri = f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_source_cog.tif"
    materialize = MagicMock(return_value=(dest_uri, True))
    with _convert_patches(items=[item], materialize=materialize) as (_s, job, source, _o, _m):
        convert_dataset_to_cog.run(str(JOB_ID))

    assert job.status == JobStatus.COMPLETED
    assert job.config["result"]["skipped_already_cog"] == 1
    assert job.config["result"]["converted_count"] == 0
    assert job.config["result"]["items"][0]["was_already_cog"] is True
    assert source.status == DatasetStatus.READY


def test_worker_multi_item_registers_all_before_ready():
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]

    def _mat(**kwargs):
        sid = kwargs["source_item_id"]
        return (f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{sid}_cog.tif", False)

    prepare_calls = []

    def _prepare(s3_uri, filename, collection, s3_config, dataset_type="imagery"):
        prepare_calls.append(filename)
        return True, [], f"stac-{filename}", dict(_STAC_ITEM)

    with _convert_patches(items=items, materialize=_mat, prepare=_prepare) as (_s, job, _src, output, _m):
        convert_dataset_to_cog.run(str(JOB_ID))

    assert prepare_calls == ["a.tif", "b.tif"]
    assert job.status == JobStatus.COMPLETED
    assert job.processed_items == 2
    assert output.status == DatasetStatus.READY
    assert len(job.config["result"]["items"]) == 2


def test_worker_partial_failure_marks_output_failed_not_ready():
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]

    def _mat(**kwargs):
        if kwargs["source_item_id"] == ITEM_B:
            raise PermanentTaskError("GeoTIFF to COG conversion failed: boom")
        return (f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_a_cog.tif", False)

    batch = MagicMock()
    with _convert_patches(
        items=items,
        materialize=_mat,
        extra=[patch("app.workers.ingestion.tasks.batch_upsert_stac_items", batch)],
    ) as (_s, job, source, output, _m):
        convert_dataset_to_cog.run(str(JOB_ID))

    batch.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert output.status == DatasetStatus.FAILED
    assert source.status == DatasetStatus.READY
    assert job.failed_items == 1


def test_worker_retry_reuses_output_and_skips_registered_items():
    done_item = MagicMock()
    done_item.s3_uri = f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_A}_a_cog.tif"
    done_item.stac_item_id = "stac-a"
    done_item.geometry = None
    done_item.properties_cache = {
        "source_item_id": str(ITEM_A),
        "was_already_cog": False,
    }
    items = [_item(item_id=ITEM_A, filename="a.tif"), _item(item_id=ITEM_B, filename="b.tif")]
    materialize = MagicMock(
        return_value=(f"s3://{BUCKET}/datasets/{OUTPUT_ID}/processed/{ITEM_B}_b_cog.tif", False)
    )
    upsert = MagicMock()
    with _convert_patches(
        items=items,
        already_done={str(ITEM_A): done_item},
        materialize=materialize,
        extra=[patch("app.workers.ingestion.tasks._upsert_dataset_item", upsert)],
    ) as (session, job, _src, _out, _m):
        convert_dataset_to_cog.run(str(JOB_ID))

    materialize.assert_called_once()
    assert materialize.call_args.kwargs["source_item_id"] == ITEM_B
    upsert.assert_called_once()
    assert upsert.call_args.kwargs["dataset_id"] == OUTPUT_ID
    assert job.status == JobStatus.COMPLETED
    dataset_adds = [
        c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], Dataset)
    ]
    assert dataset_adds == []
    assert job.config["output_dataset_id"] == str(OUTPUT_ID)


def test_worker_org_mismatch_is_permanent_and_skips_io():
    item = _item()
    materialize = MagicMock()
    with _convert_patches(items=[item], materialize=materialize) as (_s, job, source, _o, _m):
        source.organization_id = OTHER_ORG_ID
        convert_dataset_to_cog.run(str(JOB_ID))

    materialize.assert_not_called()
    assert job.status == JobStatus.FAILED
    assert "organization mismatch" in job.logs
    assert source.status == DatasetStatus.READY


def test_worker_resume_after_job_when_automation_keys_present():
    item = _item()
    delay = MagicMock()
    with _convert_patches(
        items=[item],
        extra=[patch("app.workers.automation.tasks.resume_after_job.delay", delay)],
    ) as (_s, job, _src, _o, _m):
        job.config["automation_run_id"] = str(uuid4())
        job.config["automation_step_id"] = str(uuid4())
        convert_dataset_to_cog.run(str(JOB_ID))

    delay.assert_called_once()
    assert delay.call_args.args[0] == str(JOB_ID)
    assert delay.call_args.args[1]["dataset"]["id"] == str(OUTPUT_ID)


def test_worker_transient_error_retries_without_failing_job():
    item = _item()
    materialize = MagicMock(side_effect=RuntimeError("minio unavailable"))
    with _convert_patches(items=[item], materialize=materialize) as (_s, job, source, output, _m):
        with pytest.raises(Exception, match="minio unavailable"):
            convert_dataset_to_cog.run(str(JOB_ID))

    assert job.status != JobStatus.FAILED
    assert output.status == DatasetStatus.INGESTING
    assert source.status == DatasetStatus.READY


# ── API ──────────────────────────────────────────────────────────────────────


def test_convert_schema_requires_dataset_id():
    payload = ConvertToCogJobCreate(dataset_id=SOURCE_ID)
    assert payload.dataset_item_ids is None
    with pytest.raises(ValidationError):
        ConvertToCogJobCreate(dataset_id=SOURCE_ID, dataset_item_ids=[])


@pytest.mark.asyncio
async def test_api_create_convert_job_returns_queued_job_after_commit(monkeypatch):
    dataset = SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=DatasetStatus.READY,
    )
    item = SimpleNamespace(id=ITEM_A, organization_id=ORG_ID, dataset_id=SOURCE_ID, is_active=True)
    db = _FakeDB(dataset=dataset, items=[item])
    apply_calls = []

    def _apply_async(*args, **kwargs):
        assert db.committed is True
        apply_calls.append((args, kwargs))

    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        _apply_async,
    )
    job = await create_convert_to_cog_job(
        payload=ConvertToCogJobCreate(dataset_id=SOURCE_ID, dataset_name="Out COG"),
        org_id=ORG_ID,
        db=db,
        current_user=SimpleNamespace(id=uuid4()),
    )
    assert job.type == JobType.CONVERT_TO_COG
    assert job.status == JobStatus.QUEUED
    assert job.config["source_dataset_id"] == str(SOURCE_ID)
    assert job.config["dataset_item_ids"] == [str(ITEM_A)]
    assert job.config["dataset_name"] == "Out COG"
    assert job.input_refs == [{"type": "dataset", "id": str(SOURCE_ID)}]
    assert apply_calls
    assert apply_calls[0][1]["args"] == [str(job.id)]


@pytest.mark.asyncio
async def test_api_subset_missing_item_is_404(monkeypatch):
    dataset = SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=DatasetStatus.READY,
    )
    db = _FakeDB(dataset=dataset, items=[_item()])
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        MagicMock(),
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_convert_to_cog_job(
            payload=ConvertToCogJobCreate(
                dataset_id=SOURCE_ID,
                dataset_item_ids=[ITEM_A, ITEM_B],
            ),
            org_id=ORG_ID,
            db=db,
            current_user=SimpleNamespace(id=uuid4()),
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_api_missing_dataset_is_404(monkeypatch):
    db = _FakeDB(dataset=None, items=[])
    enqueue = MagicMock()
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        enqueue,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_convert_to_cog_job(
            payload=ConvertToCogJobCreate(dataset_id=SOURCE_ID),
            org_id=ORG_ID,
            db=db,
            current_user=SimpleNamespace(id=uuid4()),
        )
    assert exc.value.status_code == 404
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_api_empty_items_is_422(monkeypatch):
    dataset = SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=DatasetStatus.READY,
    )
    db = _FakeDB(dataset=dataset, items=[])
    enqueue = MagicMock()
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        enqueue,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_convert_to_cog_job(
            payload=ConvertToCogJobCreate(dataset_id=SOURCE_ID),
            org_id=ORG_ID,
            db=db,
            current_user=SimpleNamespace(id=uuid4()),
        )
    assert exc.value.status_code == 422
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_api_ingesting_dataset_is_409(monkeypatch):
    dataset = SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=DatasetStatus.INGESTING,
    )
    db = _FakeDB(dataset=dataset, items=[_item()])
    enqueue = MagicMock()
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        enqueue,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_convert_to_cog_job(
            payload=ConvertToCogJobCreate(dataset_id=SOURCE_ID),
            org_id=ORG_ID,
            db=db,
            current_user=SimpleNamespace(id=uuid4()),
        )
    assert exc.value.status_code == 409
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_api_unknown_project_is_404(monkeypatch):
    dataset = SimpleNamespace(
        id=SOURCE_ID,
        organization_id=ORG_ID,
        deleted_at=None,
        status=DatasetStatus.READY,
    )
    db = _FakeDB(dataset=dataset, items=[_item()], project=None)
    enqueue = MagicMock()
    monkeypatch.setattr(
        "app.workers.ingestion.tasks.convert_dataset_to_cog.apply_async",
        enqueue,
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_convert_to_cog_job(
            payload=ConvertToCogJobCreate(dataset_id=SOURCE_ID, project_id=uuid4()),
            org_id=ORG_ID,
            db=db,
            current_user=SimpleNamespace(id=uuid4()),
        )
    assert exc.value.status_code == 404
    enqueue.assert_not_called()
