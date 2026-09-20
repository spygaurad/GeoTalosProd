"""Opt-in PostgreSQL lookup checks using connection-local temporary data.

Run with TILE_DB_TESTS=1 in the local API container. The temporary table
uses the application table's columns, but not its RLS, foreign keys or triggers.
No rows are written to public.annotation_sets.
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_current_org_id, get_session
from app.api.v1.endpoints import annotation_sets
from app.config import settings
from app.middleware import clerk_auth


@pytest.mark.skipif(
    os.environ.get("TILE_DB_TESTS") != "1",
    reason="Requires explicit opt-in to the local PostgreSQL database",
)
@pytest.mark.parametrize("case", ["viewer", "deleted", "other_org"])
def test_tile_access_with_postgres_rows(client, monkeypatch, case):
    set_id, owner_org, caller_org = uuid4(), uuid4(), uuid4()
    if case != "other_org":
        caller_org = owner_org

    async def current_org():
        return caller_org

    async def temporary_session():
        engine = create_async_engine(settings.APP_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    await connection.execute(text(
                        "CREATE TEMP TABLE annotation_sets "
                        "(LIKE public.annotation_sets INCLUDING DEFAULTS) ON COMMIT DROP"
                    ))
                    await connection.execute(text("SET LOCAL search_path TO pg_temp, public"))
                    await connection.execute(text(
                        "INSERT INTO pg_temp.annotation_sets "
                        "(id, organization_id, name, created_by_user_id, deleted_at) "
                        "VALUES (:id, :org, :name, :creator, :deleted)"
                    ), {
                        "id": set_id, "org": owner_org, "name": "isolated-tile-test",
                        "creator": uuid4(),
                        "deleted": datetime.now(timezone.utc) if case == "deleted" else None,
                    })
                    # Prove the row exists even when the service must exclude it.
                    assert (await connection.execute(text(
                        "SELECT count(*) FROM pg_temp.annotation_sets WHERE id = :id"
                    ), {"id": set_id})).scalar_one() == 1
                    async with AsyncSession(bind=connection) as session:
                        yield session
                finally:
                    await transaction.rollback()
        finally:
            await engine.dispose()

    monkeypatch.setitem(client.app.dependency_overrides, get_current_org_id, current_org)
    monkeypatch.setitem(client.app.dependency_overrides, get_session, temporary_session)
    monkeypatch.setattr(clerk_auth, "_DEV_CLAIMS", {
        **clerk_auth._DEV_CLAIMS, "org_role": "org:viewer",
    })

    upstream_get = AsyncMock(return_value=httpx.Response(
        200, content=b"tile", headers={"content-type": "application/x-protobuf"}
    ))
    monkeypatch.setattr(client.app.state.martin_client, "get", upstream_get)
    # Spy on the real helper; do not replace the service or its SQL lookup.
    forward = AsyncMock(wraps=annotation_sets.forward_tile)
    monkeypatch.setattr(annotation_sets, "forward_tile", forward)

    response = client.get(
        f"/api/v1/annotation-sets/{set_id}/tiles/14/12306/7077.pbf"
    )
    if case == "viewer":
        assert response.status_code == 200, response.text
        assert response.content == b"tile"
        assert response.headers["cache-control"] == "private, no-store"
        upstream_get.assert_awaited_once_with(
            f"/annotation_set_mvt/14/12306/7077?set_id={set_id}"
        )
    else:
        assert response.status_code == 404, response.text
        assert response.json() == {"detail": "AnnotationSet not found"}
        forward.assert_not_called()
        upstream_get.assert_not_called()
