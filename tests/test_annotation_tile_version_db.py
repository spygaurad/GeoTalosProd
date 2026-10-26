"""Opt-in PostgreSQL checks for the annotation_sets.tile_version triggers (migration 071).

Run with TILE_DB_TESTS=1 in the local API container. Each test creates its own
organization, schema, class and sets as app_user under RLS, inside one
transaction that is always rolled back. Nothing is committed.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

pytestmark = pytest.mark.skipif(
    os.environ.get("TILE_DB_TESTS") != "1",
    reason="Requires explicit opt-in to the local PostgreSQL database",
)

SQUARE = "ST_GeomFromText('POLYGON((90.4 23.78,90.41 23.78,90.41 23.79,90.4 23.79,90.4 23.78))',4326)"
MOVED = "ST_GeomFromText('POLYGON((90.5 23.78,90.51 23.78,90.51 23.79,90.5 23.79,90.5 23.78))',4326)"


async def _setup(conn, n_sets=2):
    org, user, schema, cls = uuid4(), uuid4(), uuid4(), uuid4()
    sets = [uuid4() for _ in range(n_sets)]
    await conn.execute(text(
        "SELECT set_config('app.current_org_id', :org, true),"
        " set_config('app.current_user_id', :user, true),"
        " set_config('app.current_role', 'org:admin', true)"
    ), {"org": str(org), "user": str(user)})
    await conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:o, 'tv-test', :slug)"),
                       {"o": org, "slug": f"tv-{org}"})
    await conn.execute(text("INSERT INTO users (id, clerk_id, email) VALUES (:u, :c, 'tv@test')"),
                       {"u": user, "c": f"user_{user}"})
    await conn.execute(text(
        "INSERT INTO annotation_schemas (id, organization_id, name, geometry_types)"
        " VALUES (:s, :o, 'tv-schema', ARRAY['Polygon'])"
    ), {"s": schema, "o": org})
    await conn.execute(text("INSERT INTO annotation_classes (id, schema_id, name) VALUES (:c, :s, 'tv-class')"),
                       {"c": cls, "s": schema})
    for s in sets:
        await conn.execute(text(
            "INSERT INTO annotation_sets (id, organization_id, schema_id, name, created_by_user_id)"
            " VALUES (:id, :o, :s, 'tv-set', :u)"
        ), {"id": s, "o": org, "s": schema, "u": user})
    return {"org": org, "user": user, "cls": cls, "sets": sets}


async def _insert(conn, ctx, set_id, n=1):
    for _ in range(n):  # n statements → n bumps (one per statement, not per row)
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id)"
            f" VALUES (:s, :c, {SQUARE}, :u)"
        ), {"s": set_id, "c": ctx["cls"], "u": ctx["user"]})


async def _version(conn, set_id):
    return (await conn.execute(text("SELECT tile_version FROM annotation_sets WHERE id = :s"), {"s": set_id})).scalar_one()


async def _run(check):
    engine = create_async_engine(settings.APP_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                await check(conn, await _setup(conn))
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_set_starts_at_one():
    async def check(conn, ctx):
        assert await _version(conn, ctx["sets"][0]) == 1
    await _run(check)


@pytest.mark.asyncio
async def test_multi_row_insert_bumps_once():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id)"
            f" SELECT :s, :c, {SQUARE}, :u FROM generate_series(1, 50)"
        ), {"s": a, "c": ctx["cls"], "u": ctx["user"]})
        assert await _version(conn, a) == 2
    await _run(check)


@pytest.mark.asyncio
async def test_bulk_insert_into_two_sets_bumps_each_once():
    async def check(conn, ctx):
        a, b = ctx["sets"]
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id)"
            f" SELECT unnest(ARRAY[:a, :a, :b]::uuid[]), :c, {SQUARE}, :u"
        ), {"a": a, "b": b, "c": ctx["cls"], "u": ctx["user"]})
        assert (await _version(conn, a), await _version(conn, b)) == (2, 2)
    await _run(check)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    f"geometry = {MOVED}",
    "confidence = 0.5",
    "deleted_at = now()",
    "properties = '{\"note\": \"x\"}'::jsonb",
])
async def test_update_bumps(change):
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await _insert(conn, ctx, a)
        await conn.execute(text(f"UPDATE annotations SET {change} WHERE annotation_set_id = :s"), {"s": a})
        assert await _version(conn, a) == 3
    await _run(check)


@pytest.mark.asyncio
async def test_soft_delete_then_restore_bumps_twice():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await _insert(conn, ctx, a)
        await conn.execute(text("UPDATE annotations SET deleted_at = now() WHERE annotation_set_id = :s"), {"s": a})
        await conn.execute(text("UPDATE annotations SET deleted_at = NULL WHERE annotation_set_id = :s"), {"s": a})
        assert await _version(conn, a) == 4
    await _run(check)


@pytest.mark.asyncio
async def test_move_between_sets_bumps_both():
    async def check(conn, ctx):
        a, b = ctx["sets"]
        await _insert(conn, ctx, a)
        await conn.execute(text("UPDATE annotations SET annotation_set_id = :b WHERE annotation_set_id = :a"),
                           {"a": a, "b": b})
        assert (await _version(conn, a), await _version(conn, b)) == (3, 2)
    await _run(check)


@pytest.mark.asyncio
async def test_hard_delete_bumps():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await _insert(conn, ctx, a)
        await conn.execute(text("DELETE FROM annotations WHERE annotation_set_id = :s"), {"s": a})
        assert await _version(conn, a) == 3
    await _run(check)


@pytest.mark.asyncio
async def test_statement_touching_no_rows_does_not_bump():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await conn.execute(text("UPDATE annotations SET confidence = 1 WHERE annotation_set_id = :s"), {"s": a})
        assert await _version(conn, a) == 1
    await _run(check)


@pytest.mark.asyncio
async def test_rolled_back_write_does_not_bump():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        savepoint = await conn.begin_nested()
        await _insert(conn, ctx, a)
        assert await _version(conn, a) == 2
        await savepoint.rollback()
        assert await _version(conn, a) == 1
    await _run(check)


@pytest.mark.asyncio
async def test_other_set_untouched():
    async def check(conn, ctx):
        a, b = ctx["sets"]
        await _insert(conn, ctx, a, n=3)
        assert (await _version(conn, a), await _version(conn, b)) == (4, 1)
    await _run(check)
