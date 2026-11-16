"""Tests for the convert_to_cog automation node."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from app.automation.nodes.conversion import execute_convert_to_cog
from app.automation.registry import DeferToJob, get_node_type
from app.core.enums import DatasetStatus
from app.models.automation import AutomationPipeline, AutomationRun
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
OWNER_ID = UUID("00000000-0000-0000-0000-000000000005")
ITEM_IDS = [
    UUID("00000000-0000-0000-0000-000000000070"),
    UUID("00000000-0000-0000-0000-000000000071"),
]
JOB_ID = UUID("00000000-0000-0000-0000-000000000080")


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *, dataset, item_ids=ITEM_IDS, run=None, pipeline=None):
        self.objects = {}
        if dataset is not None:
            self.objects[(Dataset, dataset.id)] = dataset
        if run is not None:
            self.objects[(AutomationRun, run.id)] = run
        if pipeline is not None:
            self.objects[(AutomationPipeline, pipeline.id)] = pipeline
        self.item_ids = list(item_ids)
        self.statements = []
        self.added = []
        self.flushed = False

    def get(self, model, key):
        return self.objects.get((model, key))

    def execute(self, stmt):
        self.statements.append(stmt)
        return _ExecuteResult(self.item_ids)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed = True
        for obj in self.added:
            if isinstance(obj, Job) and obj.id is None:
                obj.id = JOB_ID


def _dataset(**overrides):
    values = {
        "id": DATASET_ID,
        "organization_id": ORG_ID,
        "name": "Orthomosaic",
        "status": DatasetStatus.READY,
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _run(*, triggered_by=USER_ID, project_id=PROJECT_ID):
    return SimpleNamespace(
        id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        project_id=project_id,
        triggered_by=triggered_by,
    )


def _execute(session, *, config=None, input_data=None):
    if input_data is None:
        input_data = {"dataset": {"id": str(DATASET_ID), "name": "Orthomosaic"}}
    with patch("app.workers.ingestion.tasks.convert_dataset_to_cog.delay") as delay:
        result = execute_convert_to_cog(
            session,
            config or {},
            input_data,
            organization_id=str(ORG_ID),
            run_id=str(RUN_ID),
            step_id=STEP_ID,
        )
    return result, delay


def _jobs(session):
    return [obj for obj in session.added if isinstance(obj, Job)]


def test_node_is_registered_with_dataset_handles():
    node_type = get_node_type("convert_to_cog")

    assert node_type is not None
    assert node_type.executor is execute_convert_to_cog
    assert [(h.handle, h.type) for h in node_type.inputs] == [("dataset", "dataset")]
    assert [(h.handle, h.type) for h in node_type.outputs] == [("dataset", "dataset")]
    assert "dataset_name" in node_type.config_schema["properties"]


def test_queues_convert_job_and_defers_step():
    session = _FakeSession(dataset=_dataset(), run=_run())

    result, delay = _execute(session, config={"dataset_name": "  COG copy  "})

    jobs = _jobs(session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.organization_id == ORG_ID
    assert job.type == "convert_to_cog"
    assert job.status == "queued"
    assert job.created_by_user_id == USER_ID
    assert job.total_items == len(ITEM_IDS)
    assert job.input_refs == [{"type": "dataset", "id": str(DATASET_ID)}]
    assert job.config == {
        "trigger": "automation",
        "source_dataset_id": str(DATASET_ID),
        "dataset_item_ids": [str(i) for i in ITEM_IDS],
        "dataset_name": "COG copy",
        "project_id": str(PROJECT_ID),
        "automation_run_id": str(RUN_ID),
        "automation_step_id": STEP_ID,
    }
    assert session.flushed is True
    delay.assert_called_once_with(str(JOB_ID))
    assert isinstance(result, DeferToJob)
    assert result.job_id == str(JOB_ID)


def test_item_query_is_scoped_to_dataset_org_and_active_items():
    session = _FakeSession(dataset=_dataset(), run=_run())

    _execute(session)

    sql = str(session.statements[0])
    assert "dataset_items.dataset_id" in sql
    assert "dataset_items.organization_id" in sql
    assert "dataset_items.is_active" in sql


def test_blank_name_and_missing_project_are_null():
    session = _FakeSession(dataset=_dataset(), run=_run(project_id=None))

    _execute(session, config={"dataset_name": "   "})

    config = _jobs(session)[0].config
    assert config["dataset_name"] is None
    assert config["project_id"] is None


def test_scheduled_run_falls_back_to_pipeline_owner():
    session = _FakeSession(
        dataset=_dataset(),
        run=_run(triggered_by=None),
        pipeline=SimpleNamespace(id=PIPELINE_ID, created_by=OWNER_ID),
    )

    _execute(session)

    assert _jobs(session)[0].created_by_user_id == OWNER_ID


@pytest.mark.parametrize(
    "input_data",
    [{}, {"dataset": {}}, {"dataset": {"name": "no id"}}, {"dataset": []}],
)
def test_missing_input_dataset_raises(input_data):
    session = _FakeSession(dataset=_dataset(), run=_run())

    with pytest.raises(ValueError, match="requires an input dataset"):
        _execute(session, input_data=input_data)

    assert _jobs(session) == []


@pytest.mark.parametrize(
    "dataset",
    [
        None,
        _dataset(organization_id=OTHER_ORG_ID),
        _dataset(deleted_at="2026-01-01T00:00:00Z"),
    ],
    ids=["missing", "other-org", "soft-deleted"],
)
def test_unavailable_dataset_is_not_found(dataset):
    session = _FakeSession(dataset=dataset, run=_run())

    with pytest.raises(ValueError, match="not found"):
        _execute(session)

    assert _jobs(session) == []


def test_ingesting_dataset_is_rejected():
    session = _FakeSession(dataset=_dataset(status=DatasetStatus.INGESTING), run=_run())

    with pytest.raises(ValueError, match="currently ingesting"):
        _execute(session)

    assert _jobs(session) == []


def test_dataset_without_active_items_is_rejected():
    session = _FakeSession(dataset=_dataset(), item_ids=[], run=_run())

    with pytest.raises(ValueError, match="no active raster items"):
        _execute(session)

    assert _jobs(session) == []
