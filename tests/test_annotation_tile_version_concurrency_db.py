"""Opt-in concurrency checks for the annotation_sets.tile_version triggers (migration 071).

Run with TILE_DB_TESTS=1 in the local API container. Unlike the other tile_version
tests these COMMIT, because two real transactions have to overlap, so each test
creates its own organization and deletes everything it made in a finally block.

Each writer holds one transaction open across several statements (like a bulk
import that commits once), so the second writer really waits on the row lock of
annotation_sets. The expected end state is exact: every statement advances the
version once, so no increment may be lost, and neither writer may deadlock.

Not covered: one statement writing to two sets from two writers in opposite order
deadlocks in the row-level annotations_extent_aiud trigger (migration 047), with or
without tile_version. Real imports write to a single set.
"""

import asyncio
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
STATEMENTS = 6  # per writer
ROWS = 100  # per insert statement
TIMEOUT = 60  # a deadlock or a lost wakeup fails the test instead of hanging it


async def _ctx(conn, ctx):
    await conn.execute(text(
        "SELECT set_config('app.current_org_id', :org, true),"
        " set_config('app.current_user_id', :user, true),"
        " set_config('app.current_role', 'org:admin', true)"
    ), {"org": str(ctx["org"]), "user": str(ctx["user"])})


async def _create(engine, n_sets=2):
    ctx = {k: uuid4() for k in ("org", "user", "schema", "cls")}
    ctx["sets"] = [uuid4() for _ in range(n_sets)]
    async with engine.connect() as conn:
        async with conn.begin():
            await _ctx(conn, ctx)
            await conn.execute(text("INSERT INTO organizations (id, name, slug) VALUES (:o, 'tvc-test', :slug)"),
                               {"o": ctx["org"], "slug": f"tvc-{ctx['org']}"})
            await conn.execute(text("INSERT INTO users (id, clerk_id, email) VALUES (:u, :c, 'tvc@test')"),
                               {"u": ctx["user"], "c": f"user_{ctx['user']}"})
            await conn.execute(text(
                "INSERT INTO annotation_schemas (id, organization_id, name, geometry_types)"
                " VALUES (:s, :o, 'tvc-schema', ARRAY['Polygon'])"
            ), {"s": ctx["schema"], "o": ctx["org"]})
            await conn.execute(text("INSERT INTO annotation_classes (id, schema_id, name) VALUES (:c, :s, 'tvc-class')"),
                               {"c": ctx["cls"], "s": ctx["schema"]})
            for s in ctx["sets"]:
                await conn.execute(text(
                    "INSERT INTO annotation_sets (id, organization_id, schema_id, name, created_by_user_id)"
                    " VALUES (:id, :o, :s, 'tvc-set', :u)"
                ), {"id": s, "o": ctx["org"], "s": ctx["schema"], "u": ctx["user"]})
    return ctx


async def _cleanup(engine, ctx):
    async with engine.connect() as conn:
        async with conn.begin():
            await _ctx(conn, ctx)
            sets = {"s": ctx["sets"]}
            await conn.execute(text("DELETE FROM annotations WHERE annotation_set_id = ANY(:s)"), sets)
            await conn.execute(text("DELETE FROM annotation_sets WHERE id = ANY(:s)"), sets)
            await conn.execute(text("DELETE FROM annotation_classes WHERE id = :c"), {"c": ctx["cls"]})
            await conn.execute(text("DELETE FROM annotation_schemas WHERE id = :s"), {"s": ctx["schema"]})
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": ctx["user"]})
            await conn.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": ctx["org"]})


async def _insert(conn, ctx, set_ids, rows=ROWS):
    """One multi-row INSERT, like one BATCH_SIZE chunk of a bulk import."""
    await conn.execute(text(
        f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id)"
        f" SELECT s, :c, {SQUARE}, :u FROM unnest(CAST(:sets AS uuid[])) AS s, generate_series(1, :n)"
    ), {"sets": [str(s) for s in set_ids], "c": ctx["cls"], "u": ctx["user"], "n": rows})


async def _import(engine, ctx, set_ids_per_statement):
    async with engine.connect() as conn:
        async with conn.begin():
            await _ctx(conn, ctx)
            for set_ids in set_ids_per_statement:
                await _insert(conn, ctx, set_ids)
                await asyncio.sleep(0.02)  # let the other writer interleave


async def _edit(engine, ctx, set_id, statements):
    async with engine.connect() as conn:
        async with conn.begin():
            await _ctx(conn, ctx)
            for i in range(statements):
                await conn.execute(text(
                    "UPDATE annotations SET confidence = :v WHERE annotation_set_id = :s"
                ), {"v": 0.1 + i / 100, "s": set_id})
                await asyncio.sleep(0.02)


async def _state(engine, ctx, set_id):
    async with engine.connect() as conn:
        async with conn.begin():
            await _ctx(conn, ctx)
            version = (await conn.execute(text("SELECT tile_version FROM annotation_sets WHERE id = :s"),
                                          {"s": set_id})).scalar_one()
            count = (await conn.execute(text("SELECT count(*) FROM annotations WHERE annotation_set_id = :s"),
                                        {"s": set_id})).scalar_one()
    return version, count


async def _with_sets(check):
    engine = create_async_engine(settings.APP_DATABASE_URL, poolclass=NullPool)
    ctx = None
    try:
        ctx = await _create(engine)
        await asyncio.wait_for(check(engine, ctx), TIMEOUT)
    finally:
        if ctx:
            await _cleanup(engine, ctx)
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_imports_into_the_same_set():
    async def check(engine, ctx):
        a = ctx["sets"][0]
        plan = [[a]] * STATEMENTS
        await asyncio.gather(_import(engine, ctx, plan), _import(engine, ctx, plan))
        version, count = await _state(engine, ctx, a)
        assert count == 2 * STATEMENTS * ROWS  # no rows lost
        assert version == 1 + 2 * STATEMENTS  # one bump per statement, no lost increment
    await _with_sets(check)


@pytest.mark.asyncio
async def test_import_while_editing_the_set():
    async def check(engine, ctx):
        a = ctx["sets"][0]
        await _import(engine, ctx, [[a]])  # existing annotations to edit; version is now 2
        await asyncio.gather(
            _import(engine, ctx, [[a]] * STATEMENTS),
            _edit(engine, ctx, a, STATEMENTS),
        )
        version, count = await _state(engine, ctx, a)
        assert count == (1 + STATEMENTS) * ROWS
        assert version == 2 + 2 * STATEMENTS
    await _with_sets(check)
