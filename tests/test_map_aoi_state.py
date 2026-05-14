from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.map_aoi import MapAOICreate, MapAOIInferenceCreate, MapAOISelectionConfig


def test_map_aoi_create_accepts_valid_bbox():
    payload = MapAOICreate(
        name="AOI 1",
        bbox_4326=[-10.0, -5.0, 10.0, 5.0],
        selection_config=MapAOISelectionConfig(dataset_ids=[uuid4()]),
    )
    assert payload.bbox_4326 == [-10.0, -5.0, 10.0, 5.0]


def test_map_aoi_create_rejects_invalid_bbox():
    with pytest.raises(ValidationError):
        MapAOICreate(
            name="Bad AOI",
            bbox_4326=[10.0, 5.0, -10.0, -5.0],
        )


def test_map_aoi_inference_to_job_uses_scope_bbox():
    payload = MapAOIInferenceCreate(model_id=uuid4(), scope="aoi", mount_on_map=True)
    item_id = uuid4()
    map_id = uuid4()
    project_id = uuid4()
    job_payload = payload.to_inference_job(
        dataset_item_ids=[item_id],
        map_id=map_id,
        project_id=project_id,
        aoi_bbox=[0.0, 0.0, 1.0, 1.0],
    )
    assert job_payload.dataset_item_ids == [item_id]
    assert job_payload.map_id == map_id
    assert job_payload.project_id == project_id
    assert job_payload.aoi_bbox == [0.0, 0.0, 1.0, 1.0]
    assert job_payload.mount_on_map is True
