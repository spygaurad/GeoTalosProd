from types import SimpleNamespace
from uuid import uuid4

import app.api.v1.endpoints.annotation_set_collections as collections_ep
import app.api.v1.endpoints.annotations as annotations_ep


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _assert_status(resp, expected: int) -> None:
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


def test_annotation_set_collection_crud_endpoints(client, monkeypatch):
    collection_id = str(uuid4())
    schema_id = str(uuid4())
    collection = _ns(
        id=collection_id,
        organization_id="00000000-0000-0000-0000-000000000001",
        schema_id=schema_id,
        name="Tree Results",
        description="Grouped model outputs",
        created_by="00000000-0000-0000-0000-000000000004",
        created_at="2026-05-22T00:00:00Z",
        updated_at="2026-05-22T00:00:00Z",
        deleted_at=None,
    )

    async def _list_collections(*_a, **_kw):
        return [collection], 1

    async def _get_collection(*_a, **_kw):
        return collection

    async def _create_collection(*_a, **_kw):
        return collection

    async def _update_collection(*_a, **_kw):
        return collection

    async def _delete_collection(*_a, **_kw):
        return None

    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "list_collections", _list_collections)
    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "get_collection", _get_collection)
    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "create_collection", _create_collection)
    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "update_collection", _update_collection)
    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "delete_collection", _delete_collection)

    _assert_status(client.get("/api/v1/annotation-set-collections?limit=10&offset=0"), 200)
    _assert_status(client.get(f"/api/v1/annotation-set-collections/{collection_id}"), 200)
    _assert_status(
        client.post(
            "/api/v1/annotation-set-collections",
            json={"schema_id": schema_id, "name": "Tree Results", "description": "Grouped model outputs"},
        ),
        201,
    )
    _assert_status(
        client.patch(
            f"/api/v1/annotation-set-collections/{collection_id}",
            json={"name": "Updated Tree Results"},
        ),
        200,
    )
    _assert_status(client.delete(f"/api/v1/annotation-set-collections/{collection_id}"), 204)


def test_annotation_set_collection_link_endpoints(client, monkeypatch):
    collection_id = str(uuid4())
    annotation_set_id = str(uuid4())
    set_obj = _ns(
        id=annotation_set_id,
        organization_id="00000000-0000-0000-0000-000000000001",
        schema_id=str(uuid4()),
        dataset_id=None,
        dataset_item_id=None,
        source_type="model",
        model_id=None,
        job_id=None,
        name="Run 1",
        description=None,
        raster_config=None,
        created_by_user_id=None,
        created_at="2026-05-22T00:00:00Z",
        updated_at="2026-05-22T00:00:00Z",
        deleted_at=None,
    )
    link = _ns(
        collection_id=collection_id,
        annotation_set_id=annotation_set_id,
        linked_at="2026-05-22T00:00:00Z",
        linked_by="00000000-0000-0000-0000-000000000004",
    )

    async def _list_collection_sets(*_a, **_kw):
        return [set_obj], 1

    async def _add_set_to_collection(*_a, **_kw):
        return link

    async def _remove_set_from_collection(*_a, **_kw):
        return None

    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "list_collection_sets", _list_collection_sets)
    monkeypatch.setattr(collections_ep.AnnotationSetCollectionService, "add_set_to_collection", _add_set_to_collection)
    monkeypatch.setattr(
        collections_ep.AnnotationSetCollectionService,
        "remove_set_from_collection",
        _remove_set_from_collection,
    )

    _assert_status(
        client.get(f"/api/v1/annotation-set-collections/{collection_id}/annotation-sets?limit=10&offset=0"),
        200,
    )
    _assert_status(
        client.post(
            f"/api/v1/annotation-set-collections/{collection_id}/annotation-sets",
            json={"annotation_set_id": annotation_set_id},
        ),
        201,
    )
    _assert_status(
        client.delete(
            f"/api/v1/annotation-set-collections/{collection_id}/annotation-sets/{annotation_set_id}"
        ),
        204,
    )


def test_annotation_delete_endpoint_allows_member_workflow(client, monkeypatch):
    async def _delete_annotation(*_a, **_kw):
        return None

    monkeypatch.setattr(annotations_ep.AnnotationService, "delete_annotation", _delete_annotation)

    _assert_status(
        client.delete(
            f"/api/v1/annotation-sets/{uuid4()}/annotations/{uuid4()}"
        ),
        204,
    )
