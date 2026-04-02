"""Debug script: check titiler rendering metadata for datasets.

Run inside the API container:
  docker compose exec -T api python3 /app/check_titiler.py
"""
import asyncio
import httpx
import json


async def check():
    stac_base = "http://stac-api:8080"
    titiler_base = "http://titiler:8000"

    # Get all collections
    async with httpx.AsyncClient(base_url=stac_base, timeout=30) as stac:
        r = await stac.get("/collections")
        collections = r.json().get("collections", [])
        print(f"Found {len(collections)} collections\n")

        for coll in collections[:5]:
            cid = coll["id"]
            print(f"=== {cid} ===")

            # Get first item
            r = await stac.get(f"/collections/{cid}/items", params={"limit": 1})
            features = r.json().get("features", [])
            if not features:
                print("  No items\n")
                continue

            iid = features[0]["id"]
            props = features[0].get("properties", {})
            rc = props.get("rendering_config")
            if rc:
                print(f"  Item {iid} has rendering_config:")
                print(f"    category: {rc.get('data_category')}")
                print(f"    default:  {rc.get('default_preset')}")
                print(f"    presets:  {list(rc.get('presets', {}).keys())}")
                dp = rc.get("presets", {}).get(rc.get("default_preset"), {}).get("params", {})
                print(f"    params:   {dp}")
            else:
                print(f"  Item {iid}: no rendering_config (pre-existing item)")

            # Check titiler info
            async with httpx.AsyncClient(base_url=titiler_base, timeout=30) as tc:
                r2 = await tc.get(f"/collections/{cid}/items/{iid}/info", params={"assets": "data"})
                if r2.status_code == 200:
                    data = r2.json().get("data", {})
                    print(f"    titiler: dtype={data.get('dtype')} bands={data.get('count')} colorinterp={data.get('colorinterp')}")
                else:
                    print(f"    titiler info: {r2.status_code}")
            print()


asyncio.run(check())
