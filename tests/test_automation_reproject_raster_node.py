"""Tests for the reproject_raster automation node."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from app.automation.nodes.conversion import execute_reproject_raster
from app.automation.registry import DeferToJob, get_node_type
from app.core.enums import DatasetStatus, DatasetType
from app.models.automation import AutomationRun
from app.models.dataset import Dataset
from app.models.job import Job

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
RUN_ID = UUID("00000000-0000-0000-0000-000000000050")
PIPELINE_ID = UUID("00000000-0000-0000-0000-000000000051")
STEP_ID = "00000000-0000-0000-0000-000000000052"
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000060")
USER_ID = UUID("00000000-0000-0000-0000-000000000004")
ITEM_IDS = [UUID("00000000-0000-0000-0000-000000000070")]
JOB_ID = UUID("00000000-0000-0000-0000-000000000080")


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *, dataset, item_ids=ITEM_IDS):
        self.objects = {
            (AutomationRun, RUN_ID): SimpleNamespace(
                id=RUN_ID, pipeline_id=PIPELINE_ID, project_id=PROJECT_ID, triggered_by=USER_ID
            )
        }
        if dataset is not None:
            self.objects[(Dataset, dataset.id)] = dataset
        self.item_ids = list(item_ids)
        self.added = []

    def get(self, model, key):
        return self.objects.get((model, key))

    def execute(self, _stmt):
        return _ExecuteResult(self.item_ids)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if isinstance(obj, Job) and obj.id is None:
                obj.id = JOB_ID


def _dataset(**overrides):
    values = {
        "id": DATASET_ID,
        "organization_id": ORG_ID,
        "status": DatasetStatus.READY,
        "dataset_type": DatasetType.IMAGERY,
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _execute(session, config):
    with patch("app.workers.ingestion.tasks.reproject_raster.delay") as delay:
        result = execute_reproject_raster(
            session,
            config,
            {"dataset": {"id": str(DATASET_ID)}},
            organization_id=str(ORG_ID),
            run_id=str(RUN_ID),
            step_id=STEP_ID,
        )
    return result, delay


def _jobs(session):
    return [obj for obj in session.added if isinstance(obj, Job)]


def test_node_is_registered_with_dataset_handles():
    node_type = get_node_type("reproject_raster")

    assert node_type is not None
    assert node_type.executor is execute_reproject_raster
    assert [(h.handle, h.type) for h in node_type.inputs] == [("dataset", "dataset")]
    assert [(h.handle, h.type) for h in node_type.outputs] == [("dataset", "dataset")]
    assert node_type.config_schema["required"] == ["dst_crs"]
    assert node_type.config_schema["properties"]["resampling"]["enum"] == [
        "nearest",
        "bilinear",
        "cubic",
    ]


def test_queues_reproject_job_and_defers_step():
    session = _FakeSession(dataset=_dataset())

    result, delay = _execute(session, {"dst_crs": "3857", "dataset_name": " Mercator "})

    (job,) = _jobs(session)
    assert job.type == "reproject_raster"
    assert job.status == "queued"
    assert job.created_by_user_id == USER_ID
    assert job.config == {
        "trigger": "automation",
        "source_dataset_id": str(DATASET_ID),
        "dataset_item_ids": [str(i) for i in ITEM_IDS],
        "dst_crs": "EPSG:3857",
        "resampling": "bilinear",
        "dataset_name": "Mercator",
        "project_id": str(PROJECT_ID),
        "automation_run_id": str(RUN_ID),
        "automation_step_id": STEP_ID,
    }
    delay.assert_called_once_with(str(JOB_ID))
    assert isinstance(result, DeferToJob)
    assert result.job_id == str(JOB_ID)


def test_mask_defaults_to_nearest():
    session = _FakeSession(dataset=_dataset(dataset_type=DatasetType.SEGMENTATION_MASK))

    _execute(session, {"dst_crs": "EPSG:32618"})

    assert _jobs(session)[0].config["resampling"] == "nearest"


@pytest.mark.parametrize(
    "dataset, config, message",
    [
        (_dataset(), {}, "CRS is required"),
        (_dataset(), {"dst_crs": "EPSG:999999"}, "Unsupported CRS"),
        (_dataset(), {"dst_crs": "EPSG:3857", "resampling": "lanczos"}, "Unknown resampling"),
        (
            _dataset(dataset_type=DatasetType.SEGMENTATION_MASK),
            {"dst_crs": "EPSG:3857", "resampling": "cubic"},
            "'nearest' resampling",
        ),
        (_dataset(organization_id=OTHER_ORG_ID), {"dst_crs": "EPSG:3857"}, "not found"),
        (_dataset(status=DatasetStatus.INGESTING), {"dst_crs": "EPSG:3857"}, "currently ingesting"),
    ],
    ids=["no-crs", "bad-crs", "bad-resampling", "mask-cubic", "other-org", "ingesting"],
)
def test_invalid_input_is_rejected(dataset, config, message):
    session = _FakeSession(dataset=dataset)

    with pytest.raises(ValueError, match=message):
        _execute(session, config)

    assert _jobs(session) == []


def test_dataset_without_items_is_rejected():
    session = _FakeSession(dataset=_dataset(), item_ids=[])

    with pytest.raises(ValueError, match="no active raster items"):
        _execute(session, {"dst_crs": "EPSG:3857"})
