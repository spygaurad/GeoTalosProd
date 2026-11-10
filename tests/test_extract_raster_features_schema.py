"""Validation tests for ExtractRasterFeaturesJobCreate."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.job import ExtractRasterFeaturesJobCreate


def _payload(**overrides):
    data = {
        "dataset_item_id": uuid4(),
        "schema_id": uuid4(),
        "output_class_id": uuid4(),
        "threshold_min": 0.2,
    }
    data.update(overrides)
    return data


def test_band_payload_accepts_required_fields_and_defaults():
    payload = ExtractRasterFeaturesJobCreate(**_payload())

    assert payload.index == "band"
    assert payload.band_index == 1
    assert payload.threshold_max is None
    assert payload.min_area_px == 0.0
    assert payload.simplify_tolerance is None
    assert payload.connectivity == 4
    assert payload.dissolve is False
    assert payload.annotation_set_name is None


def test_band_payload_accepts_inclusive_threshold_max():
    payload = ExtractRasterFeaturesJobCreate(
        **_payload(
            band_index=2,
            threshold_min=1.5,
            threshold_max=1.5,
            min_area_px=4,
            simplify_tolerance=0.25,
            connectivity=8,
            dissolve=True,
            annotation_set_name="Canopy",
        )
    )

    assert payload.band_index == 2
    assert payload.threshold_max == 1.5
    assert payload.connectivity == 8
    assert payload.dissolve is True
    assert payload.annotation_set_name == "Canopy"


@pytest.mark.parametrize(
    "overrides",
    [
        {"threshold_min": None},
        {"band_index": 0},
        {"threshold_max": 0.1},
        {"min_area_px": -1},
        {"connectivity": 3},
        {"index": "ndvi"},
        {"index": "ndwi"},
    ],
)
def test_band_payload_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        ExtractRasterFeaturesJobCreate(**_payload(**overrides))


def test_missing_threshold_min_is_rejected():
    data = _payload()
    del data["threshold_min"]
    with pytest.raises(ValidationError):
        ExtractRasterFeaturesJobCreate(**data)
