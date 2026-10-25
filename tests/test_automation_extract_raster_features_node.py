"""Tests for the extract_raster_features automation node."""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from app.automation.nodes.conversion import execute_extract_raster_features
from app.automation.registry import DeferToJob, get_node_type
from app.core.enums import DatasetStatus, DatasetType
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.automation import AutomationPipeline, AutomationRun
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.job import Job

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
ITEM_ID = UUID("00000000-0000-0000-0000-000000000070")
ITEM_ID_2 = UUID("00000000-0000-0000-0000-000000000071")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000020")
OTHER_SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000021")
CLASS_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1")
RUN_ID = UUID("00000000-0000-0000-0000-000000000050")
PIPELINE_ID = UUID("00000000-0000-0000-0000-000000000051")
STEP_ID = "00000000-0000-0000-0000-000000000052"
USER_ID = UUID("00000000-0000-0000-0000-000000000004")
OWNER_ID = UUID("00000000-0000-0000-0000-000000000005")
JOB_ID = UUID("00000000-0000-0000-0000-000000000080")


def _item(**overrides):
    values = {
        "id": ITEM_ID,
        "dataset_id": DATASET_ID,
        "organization_id": ORG_ID,
        "is_active": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _dataset(**overrides):
    values = {
        "id": DATASET_ID,
        "organization_id": ORG_ID,
        "dataset_type": DatasetType.IMAGERY,
        "status": DatasetStatus.READY,
        "deleted_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _schema(**overrides):
    values = {"id": SCHEMA_ID, "organization_id": ORG_ID, "deleted_at": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def _class(**overrides):
    values = {"id": CLASS_ID, "schema_id": SCHEMA_ID}
    values.update(overrides)
    return SimpleNamespace(**values)


def _run(*, triggered_by=USER_ID):
    return SimpleNamespace(
        id=RUN_ID, pipeline_id=PIPELINE_ID, project_id=None, triggered_by=triggered_by
    )


class _FakeSession:
    def __init__(
        self,
        *,
        item=None,
        dataset=None,
        schema=None,
        ann_class=None,
        run=None,
        pipeline=None,
    ):
        item = _item() if item is None else item
        dataset = _dataset() if dataset is None else dataset
        schema = _schema() if schema is None else schema
        ann_class = _class() if ann_class is None else ann_class
        run = _run() if run is None else run
        self.objects = {
            (DatasetItem, item.id): item,
            (Dataset, dataset.id): dataset,
            (AnnotationSchema, schema.id): schema,
            (AnnotationClass, ann_class.id): ann_class,
            (AutomationRun, run.id): run,
        }
        if pipeline is not None:
            self.objects[(AutomationPipeline, pipeline.id)] = pipeline
        self.added = []

    def drop(self, model, key):
        self.objects.pop((model, key), None)

    def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if isinstance(obj, Job) and obj.id is None:
                obj.id = JOB_ID


def _config(**overrides):
    config = {
        "schema_id": str(SCHEMA_ID),
        "output_class_id": str(CLASS_ID),
        "band_index": 2,
        "threshold_min": 0.4,
        "threshold_max": 0.9,
        "min_area_px": 5,
        "simplify_tolerance": "",
        "connectivity": 8,
        "dissolve": True,
        "annotation_set_name": "  Vegetation  ",
    }
    config.update(overrides)
    return config


def _items(*ids):
    return [{"id": str(i), "dataset_id": str(DATASET_ID)} for i in (ids or (ITEM_ID,))]


def _execute(session, *, config=None, input_data=None):
    if input_data is None:
        input_data = {"items": _items()}
    with patch("app.workers.ingestion.tasks.extract_raster_features.delay") as delay:
        result = execute_extract_raster_features(
            session,
            _config() if config is None else config,
            input_data,
            organization_id=str(ORG_ID),
            run_id=str(RUN_ID),
            step_id=STEP_ID,
        )
    return result, delay


def _jobs(session):
    return [obj for obj in session.added if isinstance(obj, Job)]


def test_node_is_registered_with_item_input_and_set_output():
    node_type = get_node_type("extract_raster_features")

    assert node_type is not None
    assert node_type.executor is execute_extract_raster_features
    assert [(h.handle, h.type) for h in node_type.inputs] == [("items", "dataset_items")]
    assert [(h.handle, h.type) for h in node_type.outputs] == [
        ("annotation_set", "annotation_set")
    ]
    props = node_type.config_schema["properties"]
    assert props["schema_id"]["x-picker"] == "annotation_schema"
    assert props["output_class_id"]["x-picker"] == "annotation_class"
    assert props["output_class_id"]["type"] == "string"
    assert set(node_type.config_schema["required"]) == {
        "schema_id",
        "output_class_id",
        "threshold_min",
    }


def test_queues_extract_job_and_defers_step():
    session = _FakeSession()

    result, delay = _execute(session)

    jobs = _jobs(session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.organization_id == ORG_ID
    assert job.type == "extract_raster_features"
    assert job.status == "queued"
    assert job.created_by_user_id == USER_ID
    assert job.total_items == 1
    assert job.input_refs == [{"type": "dataset_item", "id": str(ITEM_ID)}]
    assert job.config == {
        "trigger": "automation",
        "dataset_item_id": str(ITEM_ID),
        "schema_id": str(SCHEMA_ID),
        "output_class_id": str(CLASS_ID),
        "band_index": 2,
        "threshold_min": 0.4,
        "threshold_max": 0.9,
        "min_area_px": 5.0,
        "simplify_tolerance": None,
        "connectivity": 8,
        "dissolve": True,
        "annotation_set_name": "Vegetation",
        "index": "band",
        "automation_run_id": str(RUN_ID),
        "automation_step_id": STEP_ID,
    }
    delay.assert_called_once_with(str(JOB_ID))
    assert isinstance(result, DeferToJob)
    assert result.job_id == str(JOB_ID)


def test_job_config_is_accepted_by_the_worker_schema():
    from app.schemas.job import ExtractRasterFeaturesJobCreate

    session = _FakeSession()
    _execute(session)

    payload = ExtractRasterFeaturesJobCreate.model_validate(_jobs(session)[0].config)
    assert payload.dataset_item_id == ITEM_ID
    assert payload.output_class_id == CLASS_ID


def test_blank_optional_fields_use_schema_defaults():
    session = _FakeSession()
    config = {
        "schema_id": str(SCHEMA_ID),
        "output_class_id": str(CLASS_ID),
        "threshold_min": 10,
        "threshold_max": "",
        "band_index": "",
        "connectivity": None,
        "annotation_set_name": "   ",
    }

    _execute(session, config=config)

    job_config = _jobs(session)[0].config
    assert job_config["band_index"] == 1
    assert job_config["threshold_max"] is None
    assert job_config["connectivity"] == 4
    assert job_config["dissolve"] is False
    assert job_config["annotation_set_name"] is None


def test_duplicate_item_payloads_count_as_one_item():
    session = _FakeSession()

    _execute(session, input_data={"items": _items(ITEM_ID, ITEM_ID)})

    assert len(_jobs(session)) == 1


def test_scheduled_run_falls_back_to_pipeline_owner():
    session = _FakeSession(
        run=_run(triggered_by=None),
        pipeline=SimpleNamespace(id=PIPELINE_ID, created_by=OWNER_ID),
    )

    _execute(session)

    assert _jobs(session)[0].created_by_user_id == OWNER_ID


def test_run_without_any_user_is_rejected():
    session = _FakeSession(run=_run(triggered_by=None))

    with pytest.raises(ValueError, match="Cannot determine the user"):
        _execute(session)

    assert _jobs(session) == []


@pytest.mark.parametrize("input_data", [{}, {"items": []}, {"items": [{"name": "no id"}]}])
def test_missing_input_item_raises(input_data):
    session = _FakeSession()

    with pytest.raises(ValueError, match="requires an input dataset item"):
        _execute(session, input_data=input_data)

    assert _jobs(session) == []


def test_more_than_one_item_is_rejected():
    session = _FakeSession()

    with pytest.raises(ValueError, match="one item at a time"):
        _execute(session, input_data={"items": _items(ITEM_ID, ITEM_ID_2)})

    assert _jobs(session) == []


@pytest.mark.parametrize(
    "config, message",
    [
        (_config(threshold_min=None), "threshold_min"),
        (_config(threshold_min=0.9, threshold_max=0.1), "threshold_max must be greater"),
        (_config(band_index=0), "band_index"),
        (_config(connectivity=6), "connectivity"),
        (_config(schema_id="not-a-uuid"), "schema_id"),
    ],
    ids=["missing-min", "inverted-range", "band-zero", "bad-connectivity", "bad-uuid"],
)
def test_invalid_config_is_rejected(config, message):
    session = _FakeSession()

    with pytest.raises(ValueError, match=message):
        _execute(session, config=config)

    assert _jobs(session) == []


@pytest.mark.parametrize(
    "item",
    [_item(organization_id=OTHER_ORG_ID), _item(is_active=False)],
    ids=["other-org", "inactive"],
)
def test_unavailable_item_is_not_found(item):
    session = _FakeSession(item=item)

    with pytest.raises(ValueError, match="Dataset item .* not found"):
        _execute(session)

    assert _jobs(session) == []


def test_missing_item_is_not_found():
    session = _FakeSession()
    session.drop(DatasetItem, ITEM_ID)

    with pytest.raises(ValueError, match="Dataset item .* not found"):
        _execute(session)


@pytest.mark.parametrize(
    "dataset, message",
    [
        (_dataset(organization_id=OTHER_ORG_ID), "Dataset not found"),
        (_dataset(deleted_at="2026-01-01T00:00:00Z"), "Dataset not found"),
        (_dataset(dataset_type=DatasetType.SEGMENTATION_MASK), "must be imagery"),
        (_dataset(status=DatasetStatus.INGESTING), "not ready"),
        (_dataset(status=DatasetStatus.FAILED), "not ready"),
    ],
    ids=["other-org", "soft-deleted", "not-imagery", "ingesting", "failed"],
)
def test_unusable_parent_dataset_is_rejected(dataset, message):
    session = _FakeSession(dataset=dataset)

    with pytest.raises(ValueError, match=message):
        _execute(session)

    assert _jobs(session) == []


@pytest.mark.parametrize(
    "schema",
    [_schema(organization_id=OTHER_ORG_ID), _schema(deleted_at="2026-01-01T00:00:00Z")],
    ids=["other-org", "soft-deleted"],
)
def test_unavailable_schema_is_not_found(schema):
    session = _FakeSession(schema=schema)

    with pytest.raises(ValueError, match="Annotation schema not found"):
        _execute(session)

    assert _jobs(session) == []


def test_class_from_another_schema_is_rejected():
    session = _FakeSession(ann_class=_class(schema_id=OTHER_SCHEMA_ID))

    with pytest.raises(ValueError, match="does not belong to the schema"):
        _execute(session)

    assert _jobs(session) == []
