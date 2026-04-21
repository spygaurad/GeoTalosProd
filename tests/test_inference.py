"""Tests for SAM3 inference endpoint, client, and sam3_runner worker logic.

Uses the same TestClient + dep-override pattern as tests/test_core_crud_endpoints.py.
No real DB, no real S3, no real SAM3 endpoint.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.user import User


_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = UUID("00000000-0000-0000-0000-000000000004")
_FAKE_USER = User(id=_USER_ID, clerk_id="user_dev", email="dev@localhost", name="Dev User")


def _assert_status(resp, expected: int) -> None:
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


# ── SCHEMA VALIDATION ──────────────────────────────────────────────────────


def test_schema_pcs_empty_raises():
    from app.schemas.inference import SAM3PromptPCS

    with pytest.raises(ValueError, match="text_phrases or exemplar"):
        SAM3PromptPCS()


def test_schema_pvs_points_and_labels_length_mismatch():
    from app.schemas.inference import SAM3PromptPVS

    with pytest.raises(ValueError, match="same length"):
        SAM3PromptPVS(points=[[1.0, 2.0], [3.0, 4.0]], point_labels=[1])


def test_schema_pvs_empty_raises():
    from app.schemas.inference import SAM3PromptPVS

    with pytest.raises(ValueError, match="requires points or boxes"):
        SAM3PromptPVS()


def test_schema_request_rejects_pcs_without_prompt():
    from app.schemas.inference import SAM3InferenceRequest

    with pytest.raises(ValueError, match="requires prompt_pcs"):
        SAM3InferenceRequest(
            model_id=uuid4(),
            dataset_item_id=uuid4(),
            annotation_set_name="x",
            task_type="pcs",
        )


def test_schema_request_rejects_pvs_without_prompt():
    from app.schemas.inference import SAM3InferenceRequest

    with pytest.raises(ValueError, match="requires prompt_pvs"):
        SAM3InferenceRequest(
            model_id=uuid4(),
            dataset_item_id=uuid4(),
            annotation_set_name="x",
            task_type="pvs",
        )


def test_schema_request_rejects_invalid_confidence():
    from app.schemas.inference import SAM3InferenceRequest

    with pytest.raises(ValueError):
        SAM3InferenceRequest(
            model_id=uuid4(),
            dataset_item_id=uuid4(),
            annotation_set_name="x",
            task_type="pcs",
            prompt_pcs={"text_phrases": ["x"]},
            confidence_threshold=1.5,
        )


# ── SAM3 CLIENT ────────────────────────────────────────────────────────────


def test_sam3_client_builds_bearer_auth():
    from app.services.sam3_client import SAM3Client

    model = SimpleNamespace(
        id=uuid4(),
        endpoint_url="http://sam3.internal:8080/",
        auth_config={"type": "bearer", "token": "sk-abc"},
        request_config={"timeout_s": 60.0},
    )
    client = SAM3Client(model)
    assert client.base_url == "http://sam3.internal:8080"
    assert client.headers == {"Authorization": "Bearer sk-abc"}
    assert client.timeout == 60.0


def test_sam3_client_builds_api_key_auth():
    from app.services.sam3_client import SAM3Client

    model = SimpleNamespace(
        id=uuid4(),
        endpoint_url="http://sam3.internal:8080",
        auth_config={"type": "api_key", "header": "X-Custom-Key", "key": "xyz"},
        request_config=None,
    )
    client = SAM3Client(model)
    assert client.headers == {"X-Custom-Key": "xyz"}


def test_sam3_client_no_auth():
    from app.services.sam3_client import SAM3Client

    model = SimpleNamespace(
        id=uuid4(),
        endpoint_url="http://sam3.internal:8080",
        auth_config=None,
        request_config=None,
    )
    client = SAM3Client(model)
    assert client.headers == {}


def test_sam3_client_rejects_missing_endpoint():
    from app.services.sam3_client import SAM3Client

    model = SimpleNamespace(
        id=uuid4(), endpoint_url=None, auth_config=None, request_config=None
    )
    with pytest.raises(ValueError, match="no endpoint_url"):
        SAM3Client(model)


# ── SAM3 RUNNER HELPERS ────────────────────────────────────────────────────


class _FakeSession:
    """Minimal sync-session stand-in for testing sam3_runner internals."""

    def __init__(self, mappings: list):
        self._mappings = mappings
        self.added: list = []
        self.flushed = 0

    def execute(self, _stmt):
        mappings = self._mappings

        class _Result:
            def all(self_inner):
                return mappings

        return _Result()

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1


def test_runner_vector_persists_instances_via_class_map():
    from app.workers.inference.sam3_runner import _persist_vector_instances

    model_id = uuid4()
    class_a = uuid4()
    class_b = uuid4()

    session = _FakeSession(
        mappings=[
            SimpleNamespace(
                model_label="tree",
                annotation_class_id=class_a,
                confidence_threshold=None,
            ),
            SimpleNamespace(
                model_label="road",
                annotation_class_id=class_b,
                confidence_threshold=0.8,
            ),
        ]
    )

    job = SimpleNamespace(id=uuid4())
    aset = SimpleNamespace(id=uuid4())
    sam_response = {
        "instances": [
            {
                "label": "tree",
                "confidence": 0.9,
                "geometry": {"type": "Point", "coordinates": [10.0, 20.0]},
            },
            {
                "label": "road",
                "confidence": 0.7,  # filtered by per-class threshold 0.8
                "geometry": {"type": "Point", "coordinates": [11.0, 21.0]},
            },
            {
                "label": "road",
                "confidence": 0.95,
                "geometry": {"type": "Point", "coordinates": [12.0, 22.0]},
            },
            {
                "label": "mystery",  # unmapped
                "confidence": 0.9,
                "geometry": {"type": "Point", "coordinates": [13.0, 23.0]},
            },
        ]
    }
    warnings: list[str] = []

    count = _persist_vector_instances(
        session, job, aset, model_id, sam_response, warnings
    )
    assert count == 2
    assert len(session.added) == 2
    assert any("mystery" in w for w in warnings)


def test_runner_vector_skips_instances_missing_fields():
    from app.workers.inference.sam3_runner import _persist_vector_instances

    session = _FakeSession(
        mappings=[
            SimpleNamespace(
                model_label="tree",
                annotation_class_id=uuid4(),
                confidence_threshold=None,
            )
        ]
    )
    job = SimpleNamespace(id=uuid4())
    aset = SimpleNamespace(id=uuid4())
    sam_response = {
        "instances": [
            {"label": "tree"},  # missing geometry
            {
                "geometry": {"type": "Point", "coordinates": [10.0, 20.0]}
            },  # missing label
        ]
    }
    warnings: list[str] = []
    count = _persist_vector_instances(
        session, job, aset, uuid4(), sam_response, warnings
    )
    assert count == 0
    assert len(warnings) == 2


def test_runner_resolve_s3_uri_prefers_column():
    from app.workers.inference.sam3_runner import _resolve_s3_uri

    item = SimpleNamespace(
        id=uuid4(),
        s3_uri="s3://bucket/item.tif",
        properties_cache={"assets": {"data": {"href": "s3://other/a.tif"}}},
    )
    assert _resolve_s3_uri(item) == "s3://bucket/item.tif"


def test_runner_resolve_s3_uri_falls_back_to_properties_cache():
    from app.workers.inference.sam3_runner import _resolve_s3_uri

    item = SimpleNamespace(
        id=uuid4(),
        s3_uri=None,
        properties_cache={"assets": {"visual": {"href": "s3://bucket/v.tif"}}},
    )
    assert _resolve_s3_uri(item) == "s3://bucket/v.tif"


def test_runner_resolve_s3_uri_raises_when_missing():
    from app.workers.inference.sam3_runner import _resolve_s3_uri

    item = SimpleNamespace(id=uuid4(), s3_uri=None, properties_cache=None)
    with pytest.raises(ValueError, match="no resolvable S3 URI"):
        _resolve_s3_uri(item)


# ── ENDPOINT INTEGRATION ───────────────────────────────────────────────────


class _StubSession:
    """Async session stand-in that returns pre-seeded objects from execute()."""

    def __init__(self, execute_results: list):
        self._results = list(execute_results)
        self.added: list = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, _stmt):
        row = self._results.pop(0) if self._results else None
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        return result

    def add(self, obj):
        # Populate id on flush-style objects so downstream code can reference it
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        pass

    async def refresh(self, _obj):
        # In real ORM, refresh reloads server defaults. Here we just ensure id is set.
        if getattr(_obj, "id", None) is None:
            _obj.id = uuid4()


@pytest.fixture()
def inference_client(monkeypatch):
    """TestClient with DB-session stub that returns a model + item, and mocked Celery."""
    model = SimpleNamespace(
        id=uuid4(),
        organization_id=_ORG_ID,
        endpoint_url="http://sam3.internal:8080",
        annotation_schema_id=uuid4(),
        deleted_at=None,
        config={},
    )
    item = SimpleNamespace(
        id=uuid4(),
        organization_id=_ORG_ID,
        dataset_id=uuid4(),
    )
    session = _StubSession(execute_results=[model, item])

    async def _session_override() -> AsyncGenerator:
        yield session

    async def _user_override():
        return _FAKE_USER

    async def _org_override():
        return _ORG_ID

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_current_org_id] = _org_override

    apply_async_mock = MagicMock()
    monkeypatch.setattr(
        "app.workers.inference.tasks.run_inference_job.apply_async",
        apply_async_mock,
    )

    with patch("app.middleware.clerk_auth.settings") as mock_settings, patch.object(
        ClerkAuthMiddleware, "_is_dev_bypass_allowed", return_value=True
    ):
        mock_settings.ENVIRONMENT = "development"
        with TestClient(app) as test_client:
            yield SimpleNamespace(
                client=test_client,
                model=model,
                item=item,
                session=session,
                apply_async=apply_async_mock,
            )

    app.dependency_overrides.clear()


def test_sam3_inference_pcs_vector_accepted(inference_client):
    ctx = inference_client
    body = {
        "model_id": str(ctx.model.id),
        "dataset_item_id": str(ctx.item.id),
        "annotation_set_name": "test-set",
        "task_type": "pcs",
        "prompt_pcs": {"text_phrases": ["tree"]},
        "confidence_threshold": 0.5,
        "output_format": "vector",
    }
    resp = ctx.client.post("/api/v1/inference/sam3", json=body)
    _assert_status(resp, 202)
    data = resp.json()
    assert "job_id" in data
    assert "annotation_set_id" in data
    assert data["status"] == "pending"
    assert ctx.apply_async.called
    call_args = ctx.apply_async.call_args
    assert call_args.kwargs.get("args") or call_args.args
    assert ctx.session.commit_count >= 1


def test_sam3_inference_schema_rejects_pcs_without_prompt(inference_client):
    body = {
        "model_id": str(inference_client.model.id),
        "dataset_item_id": str(inference_client.item.id),
        "annotation_set_name": "test-set",
        "task_type": "pcs",
        "output_format": "vector",
    }
    resp = inference_client.client.post("/api/v1/inference/sam3", json=body)
    _assert_status(resp, 422)


def test_sam3_inference_schema_accepts_pvs_raster_cog(inference_client):
    body = {
        "model_id": str(inference_client.model.id),
        "dataset_item_id": str(inference_client.item.id),
        "annotation_set_name": "test-set",
        "task_type": "pvs",
        "prompt_pvs": {"points": [[10.0, 20.0]], "point_labels": [1]},
        "output_format": "raster_cog",
    }
    resp = inference_client.client.post("/api/v1/inference/sam3", json=body)
    _assert_status(resp, 202)


def test_sam3_inference_rejects_invalid_aoi_geometry(inference_client):
    body = {
        "model_id": str(inference_client.model.id),
        "dataset_item_id": str(inference_client.item.id),
        "annotation_set_name": "test-set",
        "task_type": "pcs",
        "prompt_pcs": {"text_phrases": ["tree"]},
        "aoi_geometry": {"type": "Point", "coordinates": [0, 0]},  # Not Polygon
        "output_format": "vector",
    }
    resp = inference_client.client.post("/api/v1/inference/sam3", json=body)
    _assert_status(resp, 400)
