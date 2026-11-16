"""API tests for POST /api/v1/jobs/rasterize-annotation-set."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.core.enums import DatasetStatus
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.annotation_set import AnnotationSet
from app.models.dataset import Dataset
from app.models.job import Job
from app.models.user import User

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ORG_ID = UUID("00000000-0000-0000-0000-000000000099")
SET_ID = UUID("00000000-0000-0000-0000-000000000040")
SET_ID_2 = UUID("00000000-0000-0000-0000-000000000041")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
USER_ID = UUID("00000000-0000-0000-0000-000000000004")
URL = "/api/v1/jobs/rasterize-annotation-set"

FAKE_USER = User(id=USER_ID, clerk_id="user_dev", email="dev@localhost", name="Dev User")


def _body(**overrides):
    payload = {
        "annotation_set_ids": [str(SET_ID), str(SET_ID_2)],
        "reference_dataset_id": str(DATASET_ID),
        "resolution_m": 2.5,
        "dataset_name": "  GT mask  ",
    }
    payload.update(overrides)
    return payload


def _annotation_set(*, set_id=SET_ID, organization_id=ORG_ID, deleted_at=None):
    return SimpleNamespace(
        id=set_id,
        organization_id=organization_id,
        deleted_at=deleted_at,
    )


def _dataset(*, organization_id=ORG_ID, status=DatasetStatus.READY, deleted_at=None):
    return SimpleNamespace(
        id=DATASET_ID,
        organization_id=organization_id,
        status=status,
        deleted_at=deleted_at,
    )


def _column_name(column):
    name = getattr(column, "key", None) or getattr(column, "name", None)
    if name:
        return name
    inner = getattr(column, "element", None)
    if inner is not None and inner is not column:
        return _column_name(inner)
    return None


def _bound_value(value):
    if isinstance(value, BindParameter):
        if value.effective_value is not None:
            return value.effective_value
        return value.value
    clauses = getattr(value, "clauses", None)
    if clauses is not None and not isinstance(value, BooleanClauseList):
        return [_bound_value(part) for part in clauses]
    inner = getattr(value, "element", None)
    if inner is not None and type(value).__name__ == "Grouping":
        return _bound_value(inner)
    return value


def _matches(obj, clause) -> bool:
    if clause is None:
        return True
    op_name = getattr(getattr(clause, "operator", None), "__name__", "")
    clauses = getattr(clause, "clauses", None)
    if clauses is not None and op_name in ("and_", "or_"):
        parts = [_matches(obj, part) for part in clauses]
        return any(parts) if op_name == "or_" else all(parts)
    attr = _column_name(clause.left)
    actual = getattr(obj, attr)
    if op_name == "in_op":
        return actual in list(_bound_value(clause.right))
    if op_name == "eq":
        return actual == _bound_value(clause.right)
    if op_name == "is_":
        right = clause.right
        if type(right).__name__ == "Null" or _bound_value(right) is None:
            return actual is None
        return actual is _bound_value(right)
    raise AssertionError(f"unhandled SQL operator {op_name}")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, sets, dataset):
        self.sets = list(sets)
        self.dataset = dataset
        self.added = []
        self.committed = False

    def _entity(self, stmt):
        try:
            return stmt.column_descriptions[0]["entity"]
        except Exception:
            return None

    def _filter(self, rows, stmt):
        return [row for row in rows if _matches(row, stmt.whereclause)]

    async def scalars(self, stmt):
        if self._entity(stmt) is AnnotationSet:
            return _Rows(self._filter(self.sets, stmt))
        return _Rows([])

    async def scalar(self, stmt):
        if self._entity(stmt) is Dataset and self.dataset is not None:
            matched = self._filter([self.dataset], stmt)
            return matched[0] if matched else None
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
    return _FakeDB(
        sets=[
            _annotation_set(set_id=SET_ID),
            _annotation_set(set_id=SET_ID_2),
        ],
        dataset=_dataset(),
    )


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
        "app.workers.ingestion.tasks.rasterize_annotation_set.apply_async",
        _apply_async,
    )
    raster = MagicMock()
    monkeypatch.setattr(
        "app.services.conversion.rasterize_annotation_sets_to_cog",
        raster,
    )
    return calls, raster


def test_valid_request_returns_202_after_commit(client, db, monkeypatch):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["type"] == "rasterize_annotation_set"
    assert body["status"] == "queued"
    assert body["organization_id"] == str(ORG_ID)
    assert body["total_items"] == 1
    assert body["config"]["trigger"] == "api"
    assert body["config"]["annotation_set_ids"] == [str(SET_ID), str(SET_ID_2)]
    assert body["config"]["reference_dataset_id"] == str(DATASET_ID)
    assert body["config"]["resolution_m"] == 2.5
    assert body["config"]["dataset_name"] == "GT mask"
    assert "result" not in body["config"]
    assert body["input_refs"] == [
        {"type": "annotation_set", "id": str(SET_ID)},
        {"type": "annotation_set", "id": str(SET_ID_2)},
    ]
    assert calls
    assert calls[0][1]["args"] == [body["id"]]
    raster.assert_not_called()
    assert db.added
    assert db.added[0].type == "rasterize_annotation_set"
    assert db.added[0].status == "queued"
    assert db.added[0].created_by_user_id == USER_ID
    assert db.added[0].total_items == 1


def test_empty_annotation_set_ids_is_422(client, db, monkeypatch):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body(annotation_set_ids=[]))

    assert resp.status_code == 422, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


@pytest.mark.parametrize("resolution_m", [0, -1])
def test_non_positive_resolution_is_422(client, db, monkeypatch, resolution_m):
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body(resolution_m=resolution_m))

    assert resp.status_code == 422, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_missing_annotation_set_is_404(client, db, monkeypatch):
    db.sets = [_annotation_set(set_id=SET_ID)]
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert "annotation set" in resp.json()["detail"].lower()
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_annotation_set_from_another_org_is_404(client, db, monkeypatch):
    db.sets = [
        _annotation_set(set_id=SET_ID, organization_id=OTHER_ORG_ID),
        _annotation_set(set_id=SET_ID_2),
    ]
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_soft_deleted_annotation_set_is_404(client, db, monkeypatch):
    db.sets = [
        _annotation_set(set_id=SET_ID, deleted_at=datetime.now(timezone.utc)),
        _annotation_set(set_id=SET_ID_2),
    ]
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_missing_reference_dataset_is_404(client, db, monkeypatch):
    db.dataset = None
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Dataset not found"
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_reference_dataset_from_another_org_is_404(client, db, monkeypatch):
    db.dataset = _dataset(organization_id=OTHER_ORG_ID)
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Dataset not found"
    assert calls == []
    raster.assert_not_called()


def test_soft_deleted_reference_dataset_is_404(client, db, monkeypatch):
    db.dataset = _dataset(deleted_at=datetime.now(timezone.utc))
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Dataset not found"
    assert calls == []
    raster.assert_not_called()


def test_ingesting_reference_dataset_is_409(client, db, monkeypatch):
    db.dataset = _dataset(status=DatasetStatus.INGESTING)
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 409, resp.text
    assert "ingesting" in resp.json()["detail"].lower()
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False


def test_non_ready_reference_dataset_is_422(client, db, monkeypatch):
    db.dataset = _dataset(status=DatasetStatus.FAILED)
    calls, raster = _enqueue(monkeypatch, db)
    resp = client.post(URL, json=_body())

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Dataset is not ready"
    assert calls == []
    raster.assert_not_called()
    assert db.committed is False
