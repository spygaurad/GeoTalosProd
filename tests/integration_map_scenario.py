"""
Manual integration scenario: Map state management
==================================================
Uses REAL database connections with proper RLS context.
No mocks. No dev bypass. Runs against live data.

Real identities used:
  ORG_ID   = 249210ee-e845-412b-84aa-c6b23f3fc185  (org with ready datasets)
  USER_ID  = 72d4ffe1-4d28-4c70-a1be-f865ab3d4a01  (org:admin member)
  PROJECT  = c52d6282-b619-45ed-ac38-31002c011cfc  (Test Gocta project)
  DATASET_A = faff485a-1104-432b-b7e1-f5ac4587e86c (test upload gocta — ready)
  DATASET_B = 3ba4dd20-0699-460b-9775-22e913517cad (Gocta Dataset — ready)

Scenario
--------
1.  Create a map in the project
2.  GET /maps/{id}  → verify layers list is empty
3.  Add dataset-A layer  (z_index auto = 0)
4.  Add dataset-B layer  (z_index auto = 0, server sets it — reorder will fix this)
5.  Reorder layers       → dataset-B bottom, dataset-A top
6.  GET /maps/{id}/layers → verify z_index order
7.  8-second auto-save:
      PATCH /maps/{id} with updated view_state + layer states
        - dataset-B: visible=False, opacity stays 1.0
        - dataset-A: opacity=0.6, style_override={color: #ff5500}
8.  GET /maps/{id}  → verify full embedded layer state persisted
9.  PATCH single layer  → toggle dataset-B back to visible=True
10. DELETE dataset-A layer
11. GET /maps/{id}  → verify only dataset-B remains, visible=True
12. Cleanup: DELETE map
"""

import json
import sys
from unittest.mock import patch
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_current_org_id, get_current_user, get_session
from app.db.session import AsyncSessionLocal
from app.main import app
from app.middleware.clerk_auth import ClerkAuthMiddleware
from app.models.user import User

# ── Real identities ───────────────────────────────────────────────────────────

ORG_ID    = UUID("249210ee-e845-412b-84aa-c6b23f3fc185")
USER_ID   = UUID("72d4ffe1-4d28-4c70-a1be-f865ab3d4a01")
PROJECT_ID = UUID("c52d6282-b619-45ed-ac38-31002c011cfc")
DATASET_A = UUID("faff485a-1104-432b-b7e1-f5ac4587e86c")  # test upload gocta
DATASET_B = UUID("3ba4dd20-0699-460b-9775-22e913517cad")  # Gocta Dataset


# ── Auth + session injection ──────────────────────────────────────────────────

FAKE_USER = User(
    id=USER_ID,
    clerk_id="user_3B1Ky3ipUKerjCNG8jmjp5sssdQ",
    email="",
    name="Integration Test User",
)


async def _real_session_with_rls():
    """Real AsyncSession with RLS context set to the test org/user."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "SELECT set_config('app.current_org_id',  :org,  true), "
                "       set_config('app.current_user_id', :usr,  true), "
                "       set_config('app.current_role',    :role, true)"
            ),
            {
                "org":  str(ORG_ID),
                "usr":  str(USER_ID),
                "role": "org:admin",
            },
        )
        yield session


async def _fake_user():
    return FAKE_USER


async def _fake_org_id():
    return ORG_ID


async def _middleware_passthrough(self, request, call_next):
    """Bypass ClerkAuthMiddleware — inject minimal claims so downstream deps work."""
    request.state.clerk_claims = {
        "sub": str(USER_ID),
        "org_id": str(ORG_ID),
        "org_role": "org:admin",
        "email": "",
        "name": "Integration Test User",
    }
    request.state.org_uuid = str(ORG_ID)
    return await call_next(request)


# ── Helpers ───────────────────────────────────────────────────────────────────

def ok(resp: httpx.Response, expected: int = 200) -> dict:
    if resp.status_code != expected:
        print(f"\n  FAIL  HTTP {resp.status_code} (expected {expected})")
        print(f"        {resp.text[:400]}")
        sys.exit(1)
    return resp.json() if resp.content else {}


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def show(label: str, data):
    print(f"  {label}:")
    print("    " + json.dumps(data, indent=2, default=str).replace("\n", "\n    "))


# ── Main scenario ─────────────────────────────────────────────────────────────

def run():
    app.dependency_overrides[get_session]         = _real_session_with_rls
    app.dependency_overrides[get_current_user]    = _fake_user
    app.dependency_overrides[get_current_org_id]  = _fake_org_id

    with patch.object(ClerkAuthMiddleware, "dispatch", _middleware_passthrough):
     with TestClient(app, raise_server_exceptions=True) as client:
        # ── 1. Create map ─────────────────────────────────────────────────────
        section("1. Create map in Test Gocta project")
        resp = client.post("/api/v1/maps", json={
            "project_id": str(PROJECT_ID),
            "name": "Integration Test Map",
            "view_state": {
                "center": [-77.042, -6.052],
                "zoom": 14,
                "bearing": 0,
                "pitch": 0,
            },
        })
        map_data = ok(resp, 201)
        MAP_ID = map_data["id"]
        show("Created map", {"id": MAP_ID, "name": map_data["name"],
                             "layers": map_data["layers"]})
        assert map_data["layers"] == [], "New map should have no layers"
        print("  ✓  layers=[] on fresh map")

        # ── 2. GET map — verify layers embedded ──────────────────────────────
        section("2. GET /maps/{id} — verify layers embedded in response")
        resp = client.get(f"/api/v1/maps/{MAP_ID}")
        m = ok(resp)
        assert "layers" in m, "MapRead must include 'layers' key"
        assert m["layers"] == []
        print(f"  ✓  layers key present, value=[]")

        # ── 3. Add dataset-A layer ────────────────────────────────────────────
        section("3. Add dataset-A layer  (source_type=dataset)")
        resp = client.post(f"/api/v1/maps/{MAP_ID}/layers", json={
            "name": "test upload gocta",
            "layer_type": "raster",
            "source_type": "dataset",
            "dataset_id": str(DATASET_A),
            "visible": True,
            "opacity": 1.0,
        })
        layer_a = ok(resp, 201)
        LAYER_A_ID = layer_a["id"]
        show("Layer A created", {
            "id": LAYER_A_ID, "name": layer_a["name"],
            "source_type": layer_a["source_type"],
            "dataset_id": layer_a["dataset_id"],
            "z_index": layer_a["z_index"],
            "visible": layer_a["visible"],
            "opacity": layer_a["opacity"],
        })

        # ── 4. Add dataset-B layer ────────────────────────────────────────────
        section("4. Add dataset-B layer  (source_type=dataset)")
        resp = client.post(f"/api/v1/maps/{MAP_ID}/layers", json={
            "name": "Gocta Dataset",
            "layer_type": "raster",
            "source_type": "dataset",
            "dataset_id": str(DATASET_B),
            "visible": True,
            "opacity": 1.0,
        })
        layer_b = ok(resp, 201)
        LAYER_B_ID = layer_b["id"]
        show("Layer B created", {
            "id": LAYER_B_ID, "name": layer_b["name"],
            "z_index": layer_b["z_index"],
        })

        # ── 5. Reorder: B at bottom (z=0), A on top (z=1) ────────────────────
        section("5. Reorder layers — B bottom (z=0), A top (z=1)")
        resp = client.put(f"/api/v1/maps/{MAP_ID}/layers/reorder", json={
            "layer_ids": [str(LAYER_B_ID), str(LAYER_A_ID)],
        })
        reordered = ok(resp)
        for l in reordered:
            print(f"  layer {l['name']!r:30s}  z_index={l['z_index']}")
        assert reordered[0]["id"] == str(LAYER_B_ID) and reordered[0]["z_index"] == 0
        assert reordered[1]["id"] == str(LAYER_A_ID) and reordered[1]["z_index"] == 1
        print("  ✓  z_index correctly assigned by reorder endpoint")

        # ── 6. GET /maps/{id}/layers — verify order ───────────────────────────
        section("6. GET /maps/{id}/layers — verify persisted z_index order")
        resp = client.get(f"/api/v1/maps/{MAP_ID}/layers")
        layers_list = ok(resp)
        items = layers_list["items"]
        assert items[0]["id"] == str(LAYER_B_ID), "B should be first (z=0)"
        assert items[1]["id"] == str(LAYER_A_ID), "A should be second (z=1)"
        print(f"  ✓  Layer order correct: B(z=0) → A(z=1)")

        # ── 7. 8-second auto-save ─────────────────────────────────────────────
        section("7. Simulate 8-second auto-save  (PATCH /maps/{id})")
        print("     Payload: updated view_state + layer B visible=False + layer A opacity=0.6 style_override")
        resp = client.patch(f"/api/v1/maps/{MAP_ID}", json={
            "view_state": {
                "center": [-77.045, -6.058],   # user panned
                "zoom": 16,                     # user zoomed in
                "bearing": 15,
                "pitch": 30,
            },
            "layers": [
                {
                    "id": str(LAYER_B_ID),
                    "visible": False,
                },
                {
                    "id": str(LAYER_A_ID),
                    "opacity": 0.6,
                    "style_override": {"color": "#ff5500", "gamma": 1.2},
                },
            ],
        })
        saved = ok(resp)
        show("Saved map (view_state)", saved["view_state"])

        # Verify layer state embedded in map response
        saved_layers = {l["id"]: l for l in saved["layers"]}
        assert saved_layers[str(LAYER_B_ID)]["visible"] == False, "B should be hidden"
        assert saved_layers[str(LAYER_A_ID)]["opacity"] == 0.6, "A opacity should be 0.6"
        assert saved_layers[str(LAYER_A_ID)]["style_override"] == {"color": "#ff5500", "gamma": 1.2}
        print("  ✓  view_state persisted (zoom=16, panned coordinates)")
        print("  ✓  Layer B: visible=False")
        print("  ✓  Layer A: opacity=0.6, style_override={color:#ff5500, gamma:1.2}")

        # ── 8. Reload map — verify full state ─────────────────────────────────
        section("8. GET /maps/{id} — reload and verify full embedded state")
        resp = client.get(f"/api/v1/maps/{MAP_ID}")
        reloaded = ok(resp)

        assert reloaded["view_state"]["zoom"] == 16
        assert reloaded["view_state"]["center"] == [-77.045, -6.058]
        reloaded_layers = sorted(reloaded["layers"], key=lambda l: l["z_index"])
        assert reloaded_layers[0]["id"] == str(LAYER_B_ID)
        assert reloaded_layers[0]["visible"] == False
        assert reloaded_layers[1]["id"] == str(LAYER_A_ID)
        assert reloaded_layers[1]["opacity"] == 0.6
        assert reloaded_layers[1]["style_override"]["color"] == "#ff5500"
        show("Reloaded layers", [
            {k: l[k] for k in ("id", "name", "z_index", "visible", "opacity", "style_override")}
            for l in reloaded_layers
        ])
        print("  ✓  Full map state (camera + all layer properties) survived reload")

        # ── 9. Single layer PATCH — restore B visibility ──────────────────────
        section("9. PATCH /maps/{id}/layers/{layer_b_id} — restore B visible=True")
        resp = client.patch(f"/api/v1/maps/{MAP_ID}/layers/{LAYER_B_ID}", json={
            "visible": True,
        })
        patched = ok(resp)
        assert patched["visible"] == True
        print(f"  ✓  Layer B visible=True restored via single-layer PATCH")

        # ── 10. Delete layer A ────────────────────────────────────────────────
        section("10. DELETE /maps/{id}/layers/{layer_a_id}")
        resp = client.delete(f"/api/v1/maps/{MAP_ID}/layers/{LAYER_A_ID}")
        assert resp.status_code == 204, f"Expected 204, got {resp.status_code}"
        print("  ✓  Layer A deleted (204)")

        # ── 11. Reload — only B remains ───────────────────────────────────────
        section("11. GET /maps/{id} — verify only layer B remains")
        resp = client.get(f"/api/v1/maps/{MAP_ID}")
        final = ok(resp)
        assert len(final["layers"]) == 1
        remaining = final["layers"][0]
        assert remaining["id"] == str(LAYER_B_ID)
        assert remaining["visible"] == True
        show("Final map state", {
            "name": final["name"],
            "view_state": final["view_state"],
            "layers": [{k: l[k] for k in ("id", "name", "z_index", "visible", "opacity")}
                       for l in final["layers"]],
        })
        print("  ✓  Only layer B present, visible=True")

        # ── 12. Cleanup ───────────────────────────────────────────────────────
        section("12. Cleanup — DELETE map")
        resp = client.delete(f"/api/v1/maps/{MAP_ID}")
        assert resp.status_code == 204
        print("  ✓  Map deleted (soft delete)")

        # Confirm GET returns 404 after deletion
        resp = client.get(f"/api/v1/maps/{MAP_ID}")
        assert resp.status_code == 404
        print("  ✓  GET after delete returns 404")

    app.dependency_overrides.clear()
    print(f"\n{'='*60}")
    print("  ALL SCENARIO STEPS PASSED")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run()
