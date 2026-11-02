"""Opt-in PostgreSQL check that annotation_set_mvt() tiles carry the flat attributes (migration 073).

Run with TILE_DB_TESTS=1 in the local API container. Uses the same rolled-back
transaction setup as test_annotation_tile_version_db.py; nothing is committed.
MVT keys and string values are stored as plain bytes in the tile, so presence is
checked on the raw bytea without a protobuf decoder. Keys are layer-wide, so
an unset attribute shows up as a key without a value, never as a missing feature.
"""

import os

import pytest
from sqlalchemy import text

from test_annotation_tile_version_db import SQUARE, _run

pytestmark = pytest.mark.skipif(
    os.environ.get("TILE_DB_TESTS") != "1",
    reason="Requires explicit opt-in to the local PostgreSQL database",
)

KEYS = [
    "id", "class_id", "confidence", "annotation_set_id", "dataset_item_id",
    "created_by_user_id", "created_by_job_id", "created_at", "updated_at", "review_status",
    "aoi_id", "aoi_name", "source", "source_type", "verified", "model_id", "model_name", "aoi_bbox",
]
PROPS = (
    '{"aoi_id": "aoi-1", "aoi_name": "Dhaka AOI", "source": "model", "source_type": "model",'
    ' "verified": true, "model_id": "m-1", "model_name": "DINOv2 head", "review_status": "verified",'
    ' "aoi_bbox": [90.3, 23.7, 90.5, 23.8]}'
)


async def _tile(conn, set_id):
    row = await conn.execute(
        text("SELECT annotation_set_mvt(14, 12306, 7077, json_build_object('set_id', CAST(:s AS text)))"),
        {"s": str(set_id)},
    )
    return bytes(row.scalar_one())


@pytest.mark.asyncio
async def test_tile_carries_all_flat_attributes():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, confidence,"
            f" created_by_user_id, properties) VALUES (:s, :c, {SQUARE}, 0.9, :u, CAST(:p AS jsonb))"
        ), {"s": a, "c": ctx["cls"], "u": ctx["user"], "p": PROPS})
        tile = await _tile(conn, a)
        for key in ("id", "class_id", "confidence", "annotation_set_id", "created_by_user_id",
                    "created_at", "updated_at", "review_status", "aoi_id", "aoi_name", "source",
                    "source_type", "verified", "model_id", "model_name", "aoi_bbox"):
            assert key.encode() in tile, key
        # aoi_bbox is a JSON string, not an array
        assert b"[90.3, 23.7, 90.5, 23.8]" in tile
        assert b"Dhaka AOI" in tile and b"DINOv2 head" in tile
    await _run(check)


@pytest.mark.asyncio
async def test_null_values_are_left_out_not_errors():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id)"
            f" VALUES (:s, :c, {SQUARE}, :u)"
        ), {"s": a, "c": ctx["cls"], "u": ctx["user"]})
        tile = await _tile(conn, a)
        # Keys are layer-wide; a NULL value is simply not written for the feature.
        assert b"created_at" in tile and len(tile) > 0
        assert b"[90.3" not in tile and b"DINOv2" not in tile
    await _run(check)


@pytest.mark.asyncio
async def test_non_boolean_verified_does_not_break_the_tile():
    async def check(conn, ctx):
        a = ctx["sets"][0]
        await conn.execute(text(
            f"INSERT INTO annotations (annotation_set_id, class_id, geometry, created_by_user_id, properties)"
            f" VALUES (:s, :c, {SQUARE}, :u, CAST('{{\"verified\": \"yes\"}}' AS jsonb))"
        ), {"s": a, "c": ctx["cls"], "u": ctx["user"]})
        tile = await _tile(conn, a)
        assert len(tile) > 0 and b"yes" not in tile
    await _run(check)
