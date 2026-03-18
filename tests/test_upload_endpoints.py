"""
Tests for the Step 5 upload endpoints:
  POST /api/v1/datasets/{id}/uploads/initiate
  POST /api/v1/datasets/{id}/uploads/{upload_id}/part-urls
  POST /api/v1/datasets/{id}/uploads/{upload_id}/complete
"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.core.exceptions import not_found
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.job import Job
from app.models.user import User
from app.services.dataset_service import DatasetService

# ── Fixed IDs ────────────────────────────────────────────────────────────────

ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
DATASET_ID = UUID("00000000-0000-0000-0000-000000000002")
JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
USER_ID = UUID("00000000-0000-0000-0000-000000000004")
UPLOAD_ID = "test-upload-id-abc123"
S3_KEY = f"datasets/{DATASET_ID}/test.tif"
PRESIGNED_URL = "https://minio.example.com/org-1/test.tif?X-Amz-Signature=abc"

FAKE_USER = User(id=USER_ID, clerk_id="user_dev", email="dev@localhost", name="Dev User")


def _assert_status(resp, expected: int) -> None:
    """Assert HTTP status and include response body in the failure message."""
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


def _make_fake_job(status: str = "pending") -> Job:
    job = Job(
        organization_id=ORG_ID,
        type="ingest",
        status=status,
        config={"s3_key": S3_KEY, "filename": "test.tif", "upload_id": UPLOAD_ID},
        input_refs=[{"type": "dataset", "id": str(DATASET_ID)}],
        created_by_user_id=USER_ID,
    )
    job.id = JOB_ID
    return job


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_db():
    """Lightweight AsyncSession stand-in with sensible defaults."""
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = MagicMock()

    async def _refresh(obj):
        # Simulate DB assigning the server-default UUID after flush/refresh.
        if isinstance(obj, Job):
            obj.id = JOB_ID

    session.refresh = AsyncMock(side_effect=_refresh)
    return session


@pytest.fixture()
def client(mock_db):
    """TestClient with auth + DB dependencies overridden.

    * get_session → yields mock_db (skips real DB + RLS setup)
    * get_current_user → returns FAKE_USER (skips clerk upsert)
    * get_current_org_id → returns ORG_ID (skips state lookup)

    The ClerkAuthMiddleware dev bypass still fires because TestClient
    connects from 127.0.0.1 with no Authorization header (ENVIRONMENT=development).
    This sets request.state.clerk_claims with org_role="org:admin", satisfying
    the require_org_role("org:member") guards.
    """

    async def _get_session():
        yield mock_db

    async def _get_user():
        return FAKE_USER

    async def _get_org_id():
        return ORG_ID

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = _get_user
    app.dependency_overrides[get_current_org_id] = _get_org_id

    # TestClient's synthetic client address ("testclient") doesn't pass the
    # private-IP check in _is_dev_bypass_allowed, so we patch it to always
    # return True.  The dev bypass then injects _DEV_CLAIMS (org_role=org:admin)
    # which satisfies require_org_role guards without hitting a real Clerk endpoint.
    with patch.object(ClerkAuthMiddleware, "_is_dev_bypass_allowed", return_value=True):
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client

    app.dependency_overrides.clear()


# ── POST /{dataset_id}/uploads/initiate ──────────────────────────────────────


class TestInitiateUpload:
    _url = f"/api/v1/datasets/{DATASET_ID}/uploads/initiate"

    def test_success_single_part(self, client, mock_db, monkeypatch):
        """50 MB file → 1 part → 1 presigned URL returned."""
        monkeypatch.setattr(DatasetService, "get_dataset", AsyncMock(return_value=MagicMock()))

        with (
            patch("app.services.storage_service.ensure_org_bucket"),
            patch(
                "app.services.storage_service.initiate_upload",
                return_value=(S3_KEY, UPLOAD_ID),
            ),
            patch(
                "app.services.storage_service.generate_part_url",
                return_value=PRESIGNED_URL,
            ),
        ):
            resp = client.post(
                self._url,
                json={
                    "filename": "test.tif",
                    "file_size_bytes": 50 * 1024 * 1024,  # 50 MB → 1 part
                    "content_type": "image/tiff",
                },
            )

        _assert_status(resp, 200)
        body = resp.json()
        assert body["upload_id"] == UPLOAD_ID, f"upload_id mismatch: {body}"
        assert body["job_id"] == str(JOB_ID), f"job_id mismatch: {body}"
        assert body["s3_key"] == S3_KEY, f"s3_key mismatch: {body}"
        assert body["part_size_bytes"] == 100 * 1024 * 1024, f"part_size_bytes mismatch: {body}"
        assert len(body["part_urls"]) == 1, f"expected 1 part_url, got: {body['part_urls']}"
        assert body["part_urls"][0]["part_number"] == 1
        assert body["part_urls"][0]["url"] == PRESIGNED_URL

    def test_success_multi_part_capped_at_initial_batch(self, client, mock_db, monkeypatch):
        """1.5 GB file → 15 parts, but only first 10 returned in initiate response."""
        monkeypatch.setattr(DatasetService, "get_dataset", AsyncMock(return_value=MagicMock()))

        with (
            patch("app.services.storage_service.ensure_org_bucket"),
            patch(
                "app.services.storage_service.initiate_upload",
                return_value=(S3_KEY, UPLOAD_ID),
            ),
            patch(
                "app.services.storage_service.generate_part_url",
                return_value=PRESIGNED_URL,
            ),
        ):
            resp = client.post(
                self._url,
                json={
                    "filename": "large.tif",
                    "file_size_bytes": 1500 * 1024 * 1024,  # 1.5 GB → 15 parts
                },
            )

        _assert_status(resp, 200)
        body = resp.json()
        assert len(body["part_urls"]) == 10, (
            f"expected 10 part_urls (capped at _INITIAL_PART_BATCH), got {len(body['part_urls'])}"
        )
        part_numbers = [p["part_number"] for p in body["part_urls"]]
        assert part_numbers == list(range(1, 11)), f"unexpected part numbers: {part_numbers}"

    def test_dataset_not_found_returns_404(self, client, mock_db, monkeypatch):
        monkeypatch.setattr(
            DatasetService, "get_dataset", AsyncMock(side_effect=not_found("Dataset"))
        )

        resp = client.post(
            self._url,
            json={"filename": "test.tif", "file_size_bytes": 1024},
        )

        _assert_status(resp, 404)
        assert "Dataset" in resp.json()["detail"], f"unexpected detail: {resp.json()}"

    def test_job_created_and_committed(self, client, mock_db, monkeypatch):
        """Verify db.add and db.commit are called to persist the Job."""
        monkeypatch.setattr(DatasetService, "get_dataset", AsyncMock(return_value=MagicMock()))

        with (
            patch("app.services.storage_service.ensure_org_bucket"),
            patch(
                "app.services.storage_service.initiate_upload",
                return_value=(S3_KEY, UPLOAD_ID),
            ),
            patch("app.services.storage_service.generate_part_url", return_value=PRESIGNED_URL),
        ):
            resp = client.post(
                self._url,
                json={"filename": "test.tif", "file_size_bytes": 1024},
            )

        _assert_status(resp, 200)
        mock_db.add.assert_called_once()
        added_job = mock_db.add.call_args[0][0]
        assert isinstance(added_job, Job), f"expected Job, got {type(added_job)}"
        assert added_job.config["upload_id"] == UPLOAD_ID, f"config mismatch: {added_job.config}"
        assert added_job.config["s3_key"] == S3_KEY, f"config mismatch: {added_job.config}"
        mock_db.commit.assert_awaited()


# ── POST /{dataset_id}/uploads/{upload_id}/part-urls ─────────────────────────


class TestGetPartUrls:
    _url = f"/api/v1/datasets/{DATASET_ID}/uploads/{UPLOAD_ID}/part-urls"

    def _setup_execute(self, mock_db, job=None):
        """Make db.execute return a result whose scalar_one_or_none() gives *job*."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_db.execute = AsyncMock(return_value=mock_result)

    def test_success(self, client, mock_db):
        self._setup_execute(mock_db, _make_fake_job())

        with patch(
            "app.services.storage_service.generate_part_url",
            return_value=PRESIGNED_URL,
        ):
            resp = client.post(self._url, json={"part_numbers": [11, 12, 13]})

        _assert_status(resp, 200)
        body = resp.json()
        assert len(body["part_urls"]) == 3, f"expected 3 urls, got: {body}"
        assert body["part_urls"][0]["part_number"] == 11
        assert body["part_urls"][0]["url"] == PRESIGNED_URL

    def test_job_not_found_returns_404(self, client, mock_db):
        self._setup_execute(mock_db, job=None)

        resp = client.post(self._url, json={"part_numbers": [11]})

        _assert_status(resp, 404)
        assert "Upload job" in resp.json()["detail"], f"unexpected detail: {resp.json()}"

    def test_job_dataset_mismatch_returns_404(self, client, mock_db):
        """Job exists but is associated with a different dataset → 404."""
        wrong_job = _make_fake_job()
        wrong_job.input_refs = [{"type": "dataset", "id": "00000000-0000-0000-0000-000000009999"}]
        self._setup_execute(mock_db, wrong_job)

        resp = client.post(self._url, json={"part_numbers": [11]})

        _assert_status(resp, 404)

    def test_empty_part_numbers_rejected(self, client, mock_db):
        """Pydantic enforces min_length=1 on part_numbers."""
        resp = client.post(self._url, json={"part_numbers": []})

        _assert_status(resp, 422)


# ── POST /{dataset_id}/uploads/{upload_id}/complete ──────────────────────────


class TestCompleteUpload:
    _url = f"/api/v1/datasets/{DATASET_ID}/uploads/{UPLOAD_ID}/complete"
    _parts_payload = {"parts": [{"part_number": 1, "etag": '"etag-abc"'}]}

    def _setup_execute(self, mock_db, job=None):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = job
        mock_db.execute = AsyncMock(return_value=mock_result)

    def test_success_returns_202_and_enqueues_task(self, client, mock_db):
        self._setup_execute(mock_db, _make_fake_job())

        with (
            patch("app.services.storage_service.complete_upload"),
            patch("app.workers.ingestion.tasks.ingest_dataset") as mock_task,
        ):
            mock_task.apply_async = MagicMock()
            resp = client.post(self._url, json=self._parts_payload)

        _assert_status(resp, 202)
        assert resp.json()["job_id"] == str(JOB_ID), f"job_id mismatch: {resp.json()}"
        mock_task.apply_async.assert_called_once_with(
            args=[str(JOB_ID), str(DATASET_ID), S3_KEY, "test.tif"]
        )

    def test_success_sets_job_status_to_queued(self, client, mock_db):
        fake_job = _make_fake_job()
        self._setup_execute(mock_db, fake_job)

        with (
            patch("app.services.storage_service.complete_upload"),
            patch("app.workers.ingestion.tasks.ingest_dataset") as mock_task,
        ):
            mock_task.apply_async = MagicMock()
            client.post(self._url, json=self._parts_payload)

        assert fake_job.status == "queued", f"expected 'queued', got '{fake_job.status}'"

    def test_minio_failure_returns_500(self, client, mock_db):
        self._setup_execute(mock_db, _make_fake_job())

        with (
            patch(
                "app.services.storage_service.complete_upload",
                side_effect=Exception("MinIO connection refused"),
            ),
            patch("app.services.storage_service.abort_upload"),
        ):
            resp = client.post(self._url, json=self._parts_payload)

        _assert_status(resp, 500)
        assert "Upload completion failed" in resp.json()["detail"], (
            f"unexpected detail: {resp.json()}"
        )

    def test_minio_failure_calls_abort(self, client, mock_db):
        """abort_upload must be called to clean up the dangling multipart upload."""
        self._setup_execute(mock_db, _make_fake_job())

        with (
            patch(
                "app.services.storage_service.complete_upload",
                side_effect=Exception("timeout"),
            ),
            patch("app.services.storage_service.abort_upload") as mock_abort,
        ):
            client.post(self._url, json=self._parts_payload)

        mock_abort.assert_called_once()
        call_args = mock_abort.call_args.args
        assert call_args[0] == ORG_ID, f"abort org_id mismatch: {call_args[0]}"
        assert call_args[1] == S3_KEY, f"abort s3_key mismatch: {call_args[1]}"
        assert call_args[2] == UPLOAD_ID, f"abort upload_id mismatch: {call_args[2]}"

    def test_minio_failure_marks_job_failed(self, client, mock_db):
        fake_job = _make_fake_job()
        self._setup_execute(mock_db, fake_job)

        with (
            patch(
                "app.services.storage_service.complete_upload",
                side_effect=Exception("network error"),
            ),
            patch("app.services.storage_service.abort_upload"),
        ):
            client.post(self._url, json=self._parts_payload)

        assert fake_job.status == "failed", f"expected 'failed', got '{fake_job.status}'"
        assert fake_job.logs is not None, "expected job.logs to contain error details"

    def test_job_not_found_returns_404(self, client, mock_db):
        self._setup_execute(mock_db, job=None)

        resp = client.post(self._url, json=self._parts_payload)

        _assert_status(resp, 404)
        assert "Upload job" in resp.json()["detail"], f"unexpected detail: {resp.json()}"

    def test_empty_parts_rejected(self, client, mock_db):
        """Pydantic enforces min_length=1 on parts list."""
        resp = client.post(self._url, json={"parts": []})

        _assert_status(resp, 422)
