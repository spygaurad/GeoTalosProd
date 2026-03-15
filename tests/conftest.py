from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


async def _fake_db() -> AsyncGenerator[None, None]:
    yield None


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_db] = _fake_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
