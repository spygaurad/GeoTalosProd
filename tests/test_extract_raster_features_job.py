"""API tests for POST /api/v1/jobs/extract-raster-features."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.core.enums import DatasetStatus, DatasetType
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.annotation_class import AnnotationClass
from app.models.annotation_schema import AnnotationSchema
from app.models.dataset import Dataset
from app.models.dataset_item import DatasetItem
from app.models.job import Job
from app.models.user import User

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
ITEM_ID = UUID("00000000-0000-0000-0000-000000000010")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
SCHEMA_ID = UUID("00000000-0000-0000-0000-000000000020")
CLASS_ID = UUID("00000000-0000-0000-0000-000000000021")
USER_ID = UUID("00000000-0000-0000-0000-000000000004")
URL = "/api/v1/jobs/extract-raster-features"

FAKE_USER = User(id=USER_ID, clerk_id="user_dev", email="dev@localhost", name="Dev User")


def _body(**overrides):
    payload = {
        "dataset_item_id": str(ITEM_ID),
        "schema_id": str(SCHEMA_ID),
        "output_class_id": str(CLASS_ID),
        "band_index": 1,
        "threshold_min": 0.2,
        "index": "band",
    }
    payload.update(overrides)
    return payload


def _item(*, organization_id=ORG_ID, is_active=True):
    return SimpleNamespace(
        id=ITEM_ID,
        dataset_id=DATASET_ID,
        organization_id=organization_id,
        is_active=is_active,
    )


def _dataset(*, dataset_type=DatasetType.IMAGERY, status=DatasetStatus.READY, deleted_at=None):
    return SimpleNamespace(
        id=DATASET_ID,
        organization_id=ORG_ID,
        dataset_type=dataset_type,
        status=status,
        deleted_at=deleted_at,
    )


def _schema():
    return SimpleNamespace(id=SCHEMA_ID, organization_id=ORG_ID, deleted_at=None)


def _ann_class(*, schema_id=SCHEMA_ID):
    return SimpleNamespace(id=CLASS_ID, schema_id=schema_id)


class _FakeDB:
    def __init__(self, item, dataset, schema, ann_class):
        self.item = item
        self.dataset = dataset
        self.schema = schema
        self.ann_class = ann_class
        self.added = []
        self.committed = False

    def _entity(self, stmt):
        try:
            return stmt.column_descriptions[0]["entity"]
        except Exception:
            return None

    async def scalar(self, stmt):
        ent = self._entity(stmt)
        if ent is DatasetItem:
            return self.item
        if ent is Dataset:
            return self.dataset
        if ent is AnnotationSchema:
            return self.schema
        if ent is AnnotationClass:
            return self.ann_class
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if isinstance(obj, Job) and obj.id is None:
            obj.id = uuid4()
        now = datetime.now(timezone.utc)
        if getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if getattr(obj, "updated_at", None) is None:
            obj.updated_at = now


@pytest.fixture()
def db():
    return _FakeDB(item=_item(), dataset=_dataset(), schema=_schema(), ann_class=_ann_class())


@pytest.fixture()
def client(db):
    async def _get_session():
        yield db

    async def _get_user():
        return FAKE_USER

    async def _get_org_id():
        return ORG_ID

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_current_org_id] = _get_org_id
    with (
        patch("app.middleware.clerk_auth.settings") as mock_settings,
        patch.object(ClerkAuthMiddleware, "_is_dev_bypass_allowed", return_value=True),
    ):
        mock_settings.ENVIRONMENT = "development"
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    app.dependency_overrides.clear()


def _enqueue(monkeypatch, db):
    calls = []

    def _apply_async(*args, **kwargs):
        assert db.committed is True
        calls.append((args, kwargs))

    monkeypatch.setattr(
        "app.workers.ingestion.tasks.extract_raster_features.apply_async",
        _apply_async,
    )
    raster = MagicMock()
    monkeypatch.setattr(
        "app.services.conversion.threshold_extract.threshold_raster_to_features",
        raster,
    )
    return calls, raster


def test_valid_request_returns_202_after_commit(client, db, monkeypatch):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["type"] == "extract_raster_features"
    assert body["status"] == "queued"
    assert body["organization_id"] == str(ORG_ID)
    assert body["config"]["dataset_item_id"] == str(ITEM_ID)
    assert body["config"]["schema_id"] == str(SCHEMA_ID)
    assert body["config"]["output_class_id"] == str(CLASS_ID)
    assert body["config"]["threshold_min"] == 0.2
    assert body["config"]["band_index"] == 1
    assert body["config"]["index"] == "band"
    assert body["config"]["trigger"] == "api"
    assert "result" not in body["config"]
    assert body["input_refs"] == [{"type": "dataset_item", "id": str(ITEM_ID)}]
    assert calls
    assert calls[0][1]["args"] == [body["id"]]
    raster.assert_not_called()
    assert db.added
    assert db.added[0].type == "extract_raster_features"


def test_cross_org_item_is_404(client, db, monkeypatch):
    db.item = _item(organization_id=OTHER_ORG_ID)
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_missing_schema_is_404(client, db, monkeypatch):
    db.schema = None
    calls, _raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert "schema" in resp.json()["detail"].lower()
    assert calls == []


def test_class_from_another_schema_is_422(client, db, monkeypatch):
    db.ann_class = _ann_class(schema_id=uuid4())
    calls, _raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 422, resp.text
    assert "output_class_id" in resp.json()["detail"]
    assert calls == []


def test_threshold_max_below_min_is_422(client, db, monkeypatch):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body(threshold_min=0.8, threshold_max=0.1))

    assert resp.status_code == 422, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_band_index_below_one_is_422(client, db, monkeypatch):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body(band_index=0))

    assert resp.status_code == 422, resp.text
    assert calls == []
    raster.assert_not_called()


def test_non_band_index_is_422(client, db, monkeypatch):
    calls, _raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body(index="ndvi"))

    assert resp.status_code == 422, resp.text
    assert calls == []
