from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.maps import _bbox_intersects, _geometry_intersects_bbox, _parse_bbox
from app.schemas.map import MapAOIResourcesRead, MapInferenceCreate


def test_parse_bbox_accepts_valid_epsg4326_bbox():
    assert _parse_bbox("-10,-5,10,5") == [-10.0, -5.0, 10.0, 5.0]


def test_parse_bbox_rejects_invalid_bbox_order():
    with pytest.raises(Exception):
        _parse_bbox("10,5,-10,-5")


def test_bbox_intersects():
    assert _bbox_intersects([0, 0, 10, 10], [5, 5, 15, 15]) is True
    assert _bbox_intersects([0, 0, 10, 10], [11, 11, 15, 15]) is False


def test_geometry_intersects_bbox():
    geom = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    }
    assert _geometry_intersects_bbox(geom, [5, 5, 15, 15]) is True
    assert _geometry_intersects_bbox(geom, [20, 20, 30, 30]) is False


def test_map_inference_create_to_inference_job():
    model_id = uuid4()
    item_id = uuid4()
    map_id = uuid4()
    payload = MapInferenceCreate(
        model_id=model_id,
        dataset_item_ids=[item_id],
        aoi_bbox=[-1.0, -1.0, 1.0, 1.0],
        prompt_payload={"boxes": [[0, 0, 10, 10]]},
        mount_on_map=True,
    )
    job_payload = payload.to_inference_job(map_id=map_id)
    assert job_payload.model_id == model_id
    assert job_payload.dataset_item_ids == [item_id]
    assert job_payload.map_id == map_id
    assert job_payload.aoi_bbox == [-1.0, -1.0, 1.0, 1.0]
    assert job_payload.prompt_payload == {"boxes": [[0, 0, 10, 10]]}
    assert job_payload.mount_on_map is True


def test_map_inference_create_invalid_bbox():
    with pytest.raises(ValidationError):
        MapInferenceCreate(
            model_id=uuid4(),
            dataset_item_ids=[uuid4()],
            aoi_bbox=[10.0, 10.0, -10.0, -10.0],
        )


def test_map_aoi_resources_schema_accepts_raw_stac_results():
    payload = MapAOIResourcesRead(
        bbox=[0.0, 0.0, 1.0, 1.0],
        stac_collection_ids=["org-demo-collection"],
        stac_items=[
            {
                "id": "item-1",
                "collection_id": "org-demo-collection",
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
                "properties": {"datetime": "2025-01-01T00:00:00Z"},
                "dataset_item_id": None,
                "dataset_id": None,
                "s3_uri": None,
                "filename": None,
            }
        ],
    )
    assert payload.stac_collection_ids == ["org-demo-collection"]
    assert payload.stac_items[0]["id"] == "item-1"
