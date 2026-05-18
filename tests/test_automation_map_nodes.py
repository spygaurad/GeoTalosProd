from types import SimpleNamespace
from uuid import uuid4

from app.automation.nodes.data_source import (
    execute_search_map_aoi_resources,
    execute_select_map_dataset_items_in_aoi,
    execute_select_map_datasets,
)
from app.automation.nodes.map_overlay import execute_overlay_inference_outputs_on_map
from app.automation.nodes.ml_annotation import execute_run_inference


class _ExecuteResult:
    def __init__(self, scalars=None, scalar_one=None):
        self._scalars = scalars
        self._scalar_one = scalar_one

    def scalars(self):
        return self

    def all(self):
        return self._scalars or []

    def scalar_one_or_none(self):
        return self._scalar_one


class _FakeSession:
    def __init__(self, objects=None, execute_results=None):
        self.objects = objects or {}
        self.execute_results = list(execute_results or [])
        self.added = []

    def get(self, model, key):
        return self.objects.get((model, key))

    def execute(self, _stmt):
        if not self.execute_results:
            raise AssertionError("Unexpected execute() call")
        return self.execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        return None


def test_select_map_datasets_returns_selection_payload():
    from app.models.dataset import Dataset
    from app.models.map import Map

    map_id = uuid4()
    dataset_id = uuid4()
    session = _FakeSession(
        objects={(Map, map_id): SimpleNamespace(id=map_id)},
        execute_results=[
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(
                        id=dataset_id,
                        name="Orthomosaic",
                        stac_collection_id="org-demo-collection",
                    )
                ]
            )
        ],
    )

    result = execute_select_map_datasets(
        session,
        {"map_id": str(map_id)},
        {},
    )

    assert result["selection"]["map_id"] == str(map_id)
    assert result["selection"]["dataset_ids"] == [str(dataset_id)]
    assert result["selection"]["datasets"][0]["name"] == "Orthomosaic"


def test_select_map_dataset_items_in_aoi_returns_items_and_aoi():
    from app.models.dataset import Dataset
    from app.models.dataset_item import DatasetItem
    from app.models.map import Map

    map_id = uuid4()
    dataset_id = uuid4()
    item_id = uuid4()
    session = _FakeSession(
        objects={
            (Map, map_id): SimpleNamespace(id=map_id),
            (Dataset, dataset_id): SimpleNamespace(
                id=dataset_id,
                name="Trees",
                stac_collection_id="org-demo-trees",
                deleted_at=None,
            ),
        },
        execute_results=[
            _ExecuteResult(scalar_one=object()),
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(
                        id=item_id,
                        dataset_id=dataset_id,
                        stac_item_id="item-001",
                        geometry={
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                        },
                    )
                ]
            ),
        ],
    )

    result = execute_select_map_dataset_items_in_aoi(
        session,
        {
            "map_id": str(map_id),
            "dataset_id": str(dataset_id),
            "aoi_bbox": [0, 0, 1, 1],
        },
        {},
    )

    assert result["selection"]["aoi_bbox"] == [0.0, 0.0, 1.0, 1.0]
    assert result["items"][0]["id"] == str(item_id)
    assert result["selection"]["dataset_ids"] == [str(dataset_id)]


def test_search_map_aoi_resources_returns_selection_and_items():
    from app.models.map import Map

    map_id = uuid4()
    org_id = uuid4()
    dataset_id = uuid4()
    item_id = uuid4()
    vector_set_id = uuid4()
    raster_set_id = uuid4()
    session = _FakeSession(
        objects={
            (Map, map_id): SimpleNamespace(
                id=map_id,
                project=SimpleNamespace(organization_id=org_id),
            )
        },
        execute_results=[
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(
                        id=item_id,
                        dataset_id=dataset_id,
                        stac_item_id="item-a",
                        organization_id=org_id,
                        geometry={
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                        },
                    )
                ]
            ),
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(id=dataset_id, name="Dataset A", stac_collection_id="org-a", created_at=1)
                ]
            ),
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(id=raster_set_id, name="Mask 1", raster_config={"bounds_4326": [0, 0, 1, 1]})
                ]
            ),
            _ExecuteResult(scalars=[vector_set_id]),
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(
                        id=vector_set_id,
                        name="Vectors 1",
                    )
                ]
            ),
        ],
    )

    result = execute_search_map_aoi_resources(
        session,
        {"map_id": str(map_id), "aoi_bbox": [0, 0, 1, 1]},
        {},
    )

    assert result["selection"]["map_id"] == str(map_id)
    assert result["selection"]["dataset_ids"] == [str(dataset_id)]
    assert result["selection"]["vector_annotation_sets"][0]["id"] == str(vector_set_id)
    assert result["selection"]["raster_mask_annotation_sets"][0]["id"] == str(raster_set_id)
    assert result["items"][0]["id"] == str(item_id)


def test_search_map_aoi_resources_is_not_limited_to_map_layers():
    from app.models.map import Map

    map_id = uuid4()
    org_id = uuid4()
    dataset_id = uuid4()
    session = _FakeSession(
        objects={
            (Map, map_id): SimpleNamespace(
                id=map_id,
                project=SimpleNamespace(organization_id=org_id),
            )
        },
        execute_results=[
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(
                        id=uuid4(),
                        dataset_id=dataset_id,
                        stac_item_id="item-stac-wide",
                        organization_id=org_id,
                        geometry={
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                        },
                    )
                ]
            ),
            _ExecuteResult(
                scalars=[
                    SimpleNamespace(id=dataset_id, name="Global Dataset", stac_collection_id="org-global", created_at=1)
                ]
            ),
            _ExecuteResult(scalars=[]),
            _ExecuteResult(scalars=[]),
            _ExecuteResult(scalars=[]),
        ],
    )

    result = execute_search_map_aoi_resources(
        session,
        {"map_id": str(map_id), "aoi_bbox": [0, 0, 1, 1]},
        {},
    )

    assert result["selection"]["dataset_ids"] == [str(dataset_id)]
    assert result["selection"]["datasets"][0]["name"] == "Global Dataset"


def test_run_inference_uses_aoi_from_map_selection(monkeypatch):
    delay_calls = []

    def _fake_delay(job_id):
        delay_calls.append(job_id)

    monkeypatch.setattr(
        "app.workers.inference.tasks.run_inference_batch.delay",
        _fake_delay,
    )

    session = _FakeSession()
    result = execute_run_inference(
        session,
        {"confidence_threshold": 0.7, "prompt_payload": {"boxes": [[0, 0, 10, 10]]}},
        {
            "items": [{"id": str(uuid4())}],
            "model": {"id": str(uuid4())},
            "selection": {"aoi_bbox": [1, 2, 3, 4]},
        },
        organization_id=str(uuid4()),
        run_id=str(uuid4()),
        step_id=str(uuid4()),
    )

    assert result.job_id
    assert delay_calls
    assert session.added[0].config["run_output_config"]["aoi_bbox"] == [1, 2, 3, 4]
    assert session.added[0].config["run_output_config"]["prompt_payload"] == {"boxes": [[0, 0, 10, 10]]}


def test_overlay_inference_outputs_on_map_mounts_all_sets():
    map_id = uuid4()
    set_a = uuid4()
    set_b = uuid4()
    session = _FakeSession(
        execute_results=[
            _ExecuteResult(scalar_one=None),
            _ExecuteResult(scalar_one=None),
        ]
    )

    result = execute_overlay_inference_outputs_on_map(
        session,
        {"map_id": str(map_id), "opacity": 0.6, "z_index": 4},
        {"predictions": {"annotation_set_ids": [str(set_a), str(set_b)]}},
    )

    assert result["selection"]["map_id"] == str(map_id)
    assert result["selection"]["mounted_annotation_set_ids"] == [str(set_a), str(set_b)]
    assert len(session.added) == 2
