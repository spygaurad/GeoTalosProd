from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.jobs import create_inference_job
from app.schemas.job import InferenceJobCreate
from app.services.patch_service import PatchService


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeDB:
    def __init__(self, model, items):
        self.model = model
        self.items = items
        self.added = []

    async def scalar(self, _stmt):
        return self.model

    async def scalars(self, _stmt):
        return _ScalarListResult(self.items)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


def test_inference_job_schema_accepts_aoi_bbox():
    payload = InferenceJobCreate(
        model_id=uuid4(),
        dataset_item_ids=[uuid4()],
        aoi_bbox=[-10.0, -10.0, 10.0, 10.0],
    )
    assert payload.aoi_bbox == [-10.0, -10.0, 10.0, 10.0]


def test_inference_job_schema_rejects_invalid_aoi_bbox():
    with pytest.raises(ValidationError):
        InferenceJobCreate(
            model_id=uuid4(),
            dataset_item_ids=[uuid4()],
            aoi_bbox=[10.0, 10.0, -10.0, -10.0],
        )


def test_patch_service_generates_clipped_windows_for_aoi():
    windows, capped = PatchService.generate(
        item_id="item-1",
        item_bbox=[0.0, 0.0, 100.0, 100.0],
        item_width=1000,
        item_height=1000,
        patch_size_px=256,
        max_patches=32,
        clip_bbox=[25.0, 25.0, 75.0, 75.0],
    )
    assert capped is False
    assert windows
    assert all(25.0 <= w.bbox[0] <= 75.0 for w in windows)
    assert all(25.0 <= w.bbox[1] <= 75.0 for w in windows)
    assert all(25.0 <= w.bbox[2] <= 75.0 for w in windows)
    assert all(25.0 <= w.bbox[3] <= 75.0 for w in windows)
    assert windows[0].x >= 250
    assert windows[0].y >= 250


def test_patch_service_returns_no_windows_when_aoi_has_no_overlap():
    windows, capped = PatchService.generate(
        item_id="item-1",
        item_bbox=[0.0, 0.0, 100.0, 100.0],
        item_width=1000,
        item_height=1000,
        patch_size_px=256,
        max_patches=32,
        clip_bbox=[200.0, 200.0, 250.0, 250.0],
    )
    assert windows == []
    assert capped is False


@pytest.mark.asyncio
async def test_create_inference_job_includes_aoi_bbox_in_run_output_config(monkeypatch):
    org_id = uuid4()
    model = SimpleNamespace(
        id=uuid4(),
        organization_id=org_id,
        deleted_at=None,
        output_config={"patch_size_px": 1024},
    )
    item = SimpleNamespace(
        id=uuid4(),
        organization_id=org_id,
        dataset_id=uuid4(),
        is_active=True,
    )
    db = _FakeDB(model=model, items=[item])
    current_user = SimpleNamespace(id=uuid4())

    apply_async_mock = []

    def _fake_apply_async(*args, **kwargs):
        apply_async_mock.append((args, kwargs))

    monkeypatch.setattr(
        "app.workers.inference.tasks.run_inference_batch.apply_async",
        _fake_apply_async,
    )

    payload = InferenceJobCreate(
        model_id=model.id,
        dataset_item_ids=[item.id],
        aoi_bbox=[-1.0, -1.0, 1.0, 1.0],
        patch_size_px=512,
    )
    job = await create_inference_job(
        payload=payload,
        org_id=org_id,
        db=db,
        current_user=current_user,
    )

    assert job.config["run_output_config"]["aoi_bbox"] == [-1.0, -1.0, 1.0, 1.0]
    assert job.config["run_output_config"]["patch_size_px"] == 512
    assert apply_async_mock
