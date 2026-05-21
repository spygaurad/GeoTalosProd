from uuid import uuid4

from app.api.v1.endpoints.automation import _rewrite_graph_ids


def test_rewrite_graph_ids_updates_project_map_and_aoi_config_values():
    target_project_id = uuid4()
    target_map_id = uuid4()
    target_aoi_id = uuid4()

    graph = {
        "nodes": [
            {
                "id": "node-1",
                "type": "search_map_aoi_resources",
                "data": {
                    "config": {
                        "map_id": "old-map",
                        "aoi_id": "old-aoi",
                        "project_id": "old-project",
                    }
                },
            },
            {
                "id": "node-2",
                "type": "overlay_on_map",
                "data": {
                    "config": {
                        "map_id": "old-map",
                    }
                },
            },
        ],
        "edges": [],
    }

    rewritten = _rewrite_graph_ids(
        graph,
        target_project_id=target_project_id,
        target_map_id=target_map_id,
        target_aoi_id=target_aoi_id,
    )

    node_1 = rewritten["nodes"][0]["data"]["config"]
    node_2 = rewritten["nodes"][1]["data"]["config"]
    assert node_1["project_id"] == str(target_project_id)
    assert node_1["map_id"] == str(target_map_id)
    assert node_1["aoi_id"] == str(target_aoi_id)
    assert node_2["map_id"] == str(target_map_id)
