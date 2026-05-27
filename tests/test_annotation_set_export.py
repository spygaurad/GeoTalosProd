from types import SimpleNamespace
from uuid import uuid4

import app.api.v1.endpoints.annotation_sets as annotation_sets_ep


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _assert_status(resp, expected: int) -> None:
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


def test_annotation_set_export_endpoint(client, monkeypatch):
    set_id = str(uuid4())
    annotation_set = _ns(
        id=set_id,
        name="Export Me",
    )
    annotations = [
        _ns(
            id=uuid4(),
            class_id=uuid4(),
            confidence=0.9,
            properties={"label": "tree"},
            geometry={"type": "Polygon", "coordinates": []},
        )
    ]

    async def _get_set(*_a, **_kw):
        return annotation_set

    async def _list_annotations(*_a, **_kw):
        return annotations, len(annotations)

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(annotation_sets_ep.AnnotationSetService, "get_set", _get_set)
    monkeypatch.setattr(annotation_sets_ep.AnnotationService, "list_annotations", _list_annotations)
    monkeypatch.setattr(annotation_sets_ep, "serialize_geometry", lambda geom: geom)
    monkeypatch.setattr(annotation_sets_ep.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(annotation_sets_ep.storage_service, "ensure_org_bucket", lambda *_a, **_kw: None)
    monkeypatch.setattr(annotation_sets_ep.storage_service, "upload_bytes", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        annotation_sets_ep.storage_service,
        "generate_download_url",
        lambda *_a, **_kw: "https://minio.example/download",
    )

    response = client.post(
        f"/api/v1/annotation-sets/{set_id}/export",
        json={"format": "geojson", "ttl_seconds": 3600},
    )
    _assert_status(response, 200)
    body = response.json()
    assert body["annotation_set_id"] == set_id
    assert body["format"] == "geojson"
    assert body["download_url"] == "https://minio.example/download"
    assert body["filename"].endswith(".geojson")
