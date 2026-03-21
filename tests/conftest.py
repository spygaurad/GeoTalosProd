"""
Shared pytest fixtures.

Provides a minimal `client` fixture that:
  - patches ClerkAuthMiddleware dev bypass (TestClient uses "testclient" host,
    which fails the private-IP check)
  - overrides get_session / get_current_user / get_current_org_id so tests
    never touch a real database or Clerk
"""
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.user import User

_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = UUID("00000000-0000-0000-0000-000000000004")
_FAKE_USER = User(id=_USER_ID, clerk_id="user_dev", email="dev@localhost", name="Dev User")


async def _noop_session() -> AsyncGenerator[None, None]:
    """Yield a mock session where async methods are AsyncMock.

    Endpoints call await session.commit() / rollback() (e.g. via log_audit_event).
    Bare MagicMock is not awaitable, so those attributes need to be AsyncMock.
    """
    mock = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    mock.execute = AsyncMock()
    mock.refresh = AsyncMock()
    mock.flush = AsyncMock()
    yield mock


async def _fake_user():
    return _FAKE_USER


async def _fake_org_id():
    return _ORG_ID


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_session] = _noop_session
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_org_id] = _fake_org_id

    with patch("app.middleware.clerk_auth.settings") as mock_settings, \
         patch.object(ClerkAuthMiddleware, "_is_dev_bypass_allowed", return_value=True):
        mock_settings.ENVIRONMENT = "development"
        with TestClient(app) as test_client:
            yield test_client

    app.dependency_overrides.clear()
