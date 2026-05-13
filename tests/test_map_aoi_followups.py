from types import SimpleNamespace
from uuid import uuid4

from app.automation.nodes.data_source import (
    execute_load_saved_map_aoi_timeline,
    execute_select_saved_map_aoi,
)
from app.services.aoi_timeline_service import AOITimelineService


class _ExecuteResult:
    def __init__(self, scalars=None):
        self._scalars = scalars or []

    def scalars(self):
        return self

    def all(self):
        return self._scalars


class _FakeSession:
    def __init__(self, objects=None, execute_results=None):
        self.objects = objects or {}
        self.execute_results = list(execute_results or [])

    def get(self, model, key):
        return self.objects.get((model, key))

    def execute(self, _stmt):
        if not self.execute_results:
            raise AssertionError("Unexpected execute() call")
        return self.execute_results.pop(0)


def test_aoi_timeline_manifest_payload_contains_frames():
    aoi_id = uuid4()
    item_id = uuid4()
    dataset_id = uuid4()
    payload = AOITimelineService.build_manifest_payload(
        aoi_id=aoi_id,
        bbox_4326=[0.0, 0.0, 1.0, 1.0],
        render_config={"rescale": "0,255"},
        dataset_items=[
            SimpleNamespace(
                id=item_id,
                dataset_id=dataset_id,
                stac_item_id="item-1",
                stac_collection_id="collection-1",
                item_datetime=None,
                geometry={"type": "Point", "coordinates": [0.5, 0.5]},
            )
        ],
    )
    assert payload["aoi_id"] == str(aoi_id)
    assert payload["frame_count"] == 1
    assert payload["frames"][0]["dataset_item_id"] == str(item_id)


def test_select_saved_map_aoi_returns_saved_selection():
    from app.models.map_aoi import MapAOI

    map_id = uuid4()
    aoi_id = uuid4()
    session = _FakeSession(
        objects={
            (MapAOI, aoi_id): SimpleNamespace(
                id=aoi_id,
                map_id=map_id,
                deleted_at=None,
                bbox_4326=[0.0, 0.0, 1.0, 1.0],
                geometry=None,
                selection_config={"dataset_ids": [str(uuid4())], "dataset_item_ids": []},
                render_config={"rescale": "0,255"},
                temporal_config={"speed": 2},
                analysis_config={"mode": "temporal"},
            )
        }
    )
    result = execute_select_saved_map_aoi(
        session,
        {"map_id": str(map_id), "aoi_id": str(aoi_id)},
        {},
    )
    assert result["selection"]["aoi_id"] == str(aoi_id)
    assert result["selection"]["render_config"]["rescale"] == "0,255"


def test_load_saved_map_aoi_timeline_returns_sorted_items():
    from app.models.map_aoi import MapAOI

    map_id = uuid4()
    aoi_id = uuid4()
    dataset_id = uuid4()
    item_a = SimpleNamespace(
        id=uuid4(),
        dataset_id=dataset_id,
        stac_item_id="item-a",
        geometry={"type": "Point", "coordinates": [0.2, 0.2]},
        item_datetime=None,
        created_at=2,
    )
    item_b = SimpleNamespace(
        id=uuid4(),
        dataset_id=dataset_id,
        stac_item_id="item-b",
        geometry={"type": "Point", "coordinates": [0.1, 0.1]},
        item_datetime=None,
        created_at=1,
    )
    session = _FakeSession(
        objects={
            (MapAOI, aoi_id): SimpleNamespace(
                id=aoi_id,
                map_id=map_id,
                deleted_at=None,
                bbox_4326=[0.0, 0.0, 1.0, 1.0],
                selection_config={"dataset_ids": [str(dataset_id)]},
                render_config={},
                temporal_config={},
                analysis_config={},
            )
        },
        execute_results=[_ExecuteResult(scalars=[item_a, item_b])],
    )
    result = execute_load_saved_map_aoi_timeline(
        session,
        {"map_id": str(map_id), "aoi_id": str(aoi_id)},
        {},
    )
    assert result["selection"]["aoi_id"] == str(aoi_id)
    assert [item["stac_item_id"] for item in result["items"]] == ["item-b", "item-a"]
