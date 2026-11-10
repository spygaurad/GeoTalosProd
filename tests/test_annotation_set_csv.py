"""API and automation tests for annotation-set CSV export / import."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import app.api.v1.endpoints.annotation_sets as annotation_sets_ep

CLASS_ID = str(uuid4())
SCHEMA_ID = uuid4()


def _assert_status(resp, expected: int) -> None:
    assert resp.status_code == expected, (
        f"Expected HTTP {expected}, got {resp.status_code}\n"
        f"Response body: {resp.text}"
    )


@pytest.fixture()
def annotation_set(monkeypatch):
    aset = SimpleNamespace(id=str(uuid4()), name="Survey", schema_id=SCHEMA_ID)

    async def _get_set(*_a, **_kw):
        return aset

    async def _class_names(_db, schema_id):
        assert schema_id == SCHEMA_ID
        return {CLASS_ID: "Palm"}

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _no_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(annotation_sets_ep.AnnotationSetService, "get_set", _get_set)
    monkeypatch.setattr(annotation_sets_ep, "_schema_class_names", _class_names)
    monkeypatch.setattr(annotation_sets_ep.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(annotation_sets_ep, "log_audit_event", _no_audit)
    monkeypatch.setattr(annotation_sets_ep.storage_service, "ensure_org_bucket", lambda *_a, **_kw: None)
    return aset


@pytest.fixture()
def uploads(monkeypatch):
    calls = []

    def _upload_bytes(org_id, key, body, *, content_type):
        calls.append({"key": key, "body": body, "content_type": content_type})

    monkeypatch.setattr(annotation_sets_ep.storage_service, "upload_bytes", _upload_bytes)
    return calls


@pytest.fixture()
def enqueue(monkeypatch):
    apply_async = MagicMock()
    monkeypatch.setattr("app.workers.bulk.tasks.bulk_import_annotations.apply_async", apply_async)
    return apply_async


# ── Export ────────────────────────────────────────────────────────────────────

def test_export_csv_writes_rows_with_class_names(client, monkeypatch, annotation_set, uploads):
    annotations = [
        SimpleNamespace(
            id=uuid4(),
            class_id=CLASS_ID,
            confidence=0.75,
            properties={"label": "tree"},
            geometry={"type": "Point", "coordinates": [10.0, 20.0]},
        )
    ]

    async def _list_annotations(*_a, **_kw):
        return annotations, len(annotations)

    monkeypatch.setattr(annotation_sets_ep.AnnotationService, "list_annotations", _list_annotations)
    monkeypatch.setattr(annotation_sets_ep, "serialize_geometry", lambda geom: geom)
    monkeypatch.setattr(
        annotation_sets_ep.storage_service,
        "generate_download_url",
        lambda *_a, **_kw: "https://minio.example/download",
    )

    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "csv"},
    )

    _assert_status(resp, 200)
    body = resp.json()
    assert body["format"] == "csv"
    assert body["filename"].endswith(".csv")
    assert body["download_url"] == "https://minio.example/download"
    (upload,) = uploads
    assert upload["key"].endswith(".csv")
    assert upload["content_type"] == "text/csv"
    lines = upload["body"].decode("utf-8").splitlines()
    assert lines[0] == "id,class_id,class_name,confidence,geometry_wkt,longitude,latitude,label"
    assert lines[1] == f"{annotations[0].id},{CLASS_ID},Palm,0.75,POINT (10 20),10.0,20.0,tree"


def test_export_rejects_unknown_format(client, annotation_set):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "shapefile"},
    )

    _assert_status(resp, 422)


# ── Import ────────────────────────────────────────────────────────────────────

def _staged_geojson(uploads) -> dict:
    (upload,) = uploads
    assert upload["content_type"] == "application/geo+json"
    return json.loads(upload["body"])


def test_import_csv_stages_geojson_and_queues_job(client, annotation_set, uploads, enqueue):
    csv_text = (
        "class_name,confidence,longitude,latitude,note\n"
        "palm,0.8,10,20,first\n"
        "Unknown,,11,21,second\n"
    )

    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/import-csv",
        json={"csv_text": csv_text, "filename": "survey.csv"},
    )

    _assert_status(resp, 202)
    assert resp.json()["status"] == "queued"
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["queue"] == "bulk"

    fc = _staged_geojson(uploads)
    first, second = fc["features"]
    assert first["geometry"] == {"type": "Point", "coordinates": [10.0, 20.0]}
    assert first["properties"] == {"class_id": CLASS_ID, "confidence": "0.8", "note": "first"}
    # Unmatched names are left for the worker's default_class_id / failure path.
    assert second["properties"] == {"note": "second"}
    assert uploads[0]["key"].endswith("/survey.geojson")


def test_import_csv_keeps_existing_class_ids(client, annotation_set, uploads, enqueue):
    other = str(uuid4())
    csv_text = f"class_id,class_name,geometry_wkt\n{other},palm,POINT (1 2)\n"

    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/import-csv",
        json={"csv_text": csv_text},
    )

    _assert_status(resp, 202)
    (feature,) = _staged_geojson(uploads)["features"]
    assert feature["properties"] == {"class_id": other}


@pytest.mark.parametrize(
    "csv_text, message",
    [
        ("name,value\nA,1\n", "WKT geometry column"),
        ("longitude,latitude\n", "no data rows"),
    ],
    ids=["no-geometry-columns", "header-only"],
)
def test_import_csv_rejects_unusable_files(client, annotation_set, uploads, enqueue, csv_text, message):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/import-csv",
        json={"csv_text": csv_text},
    )

    _assert_status(resp, 422)
    assert message in resp.json()["detail"]
    assert uploads == []
    enqueue.assert_not_called()


def test_import_csv_requires_schema(client, annotation_set, uploads, enqueue):
    annotation_set.schema_id = None

    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/import-csv",
        json={"csv_text": "longitude,latitude\n1,2\n"},
    )

    _assert_status(resp, 400)
    enqueue.assert_not_called()


def test_geojson_import_still_uses_shared_staging(client, annotation_set, uploads, enqueue):
    geojson = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {}}],
    }

    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/import",
        json={"geojson": geojson, "filename": "points.geojson"},
    )

    _assert_status(resp, 202)
    assert _staged_geojson(uploads) == geojson
    enqueue.assert_called_once()


# ── Automation node ───────────────────────────────────────────────────────────

def test_export_annotations_node_writes_csv(monkeypatch):
    from app.automation.nodes.map_overlay import execute_export_annotations
    from app.automation.registry import get_node_type

    assert get_node_type("export_annotations").config_schema["properties"]["format"]["enum"] == [
        "geojson",
        "csv",
    ]

    row = SimpleNamespace(
        id=uuid4(),
        class_id=CLASS_ID,
        geojson='{"type":"Point","coordinates":[5,6]}',
        confidence=0.5,
        properties={"label": "x"},
        class_name="Palm",
    )
    session = MagicMock()
    session.execute.return_value.all.return_value = [row]
    written = {}

    def _upload_from_path(org_id, key, path, *, content_type):
        with open(path, encoding="utf-8") as f:
            written.update(key=key, body=f.read(), content_type=content_type)

    monkeypatch.setattr("app.services.storage_service.upload_from_path", _upload_from_path)
    monkeypatch.setattr("app.services.storage_service.bucket_name", lambda _org: "org-bucket")
    run_id = str(uuid4())

    result = execute_export_annotations(
        session,
        {"format": "csv"},
        {"annotation_set": {"id": str(uuid4())}},
        organization_id=str(uuid4()),
        run_id=run_id,
    )

    assert written["key"] == f"exports/automation/{run_id}/annotations.csv"
    assert written["content_type"] == "text/csv"
    lines = written["body"].splitlines()
    assert lines[1] == f"{row.id},{CLASS_ID},Palm,0.5,POINT (5 6),5,6,x"
    assert result["export_url"] == f"s3://org-bucket/exports/automation/{run_id}/annotations.csv"


# ── Export in another CRS ─────────────────────────────────────────────────────

@pytest.fixture()
def point_annotations(monkeypatch):
    annotations = [
        SimpleNamespace(
            id=uuid4(),
            class_id=CLASS_ID,
            confidence=None,
            properties={},
            geometry={"type": "Point", "coordinates": [10.0, 0.0]},
        )
    ]

    async def _list_annotations(*_a, **_kw):
        return annotations, len(annotations)

    monkeypatch.setattr(annotation_sets_ep.AnnotationService, "list_annotations", _list_annotations)
    monkeypatch.setattr(annotation_sets_ep, "serialize_geometry", lambda geom: geom)
    monkeypatch.setattr(
        annotation_sets_ep.storage_service,
        "generate_download_url",
        lambda *_a, **_kw: "https://minio.example/download",
    )
    return annotations


def test_export_geojson_in_web_mercator(client, annotation_set, uploads, point_annotations):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "geojson", "crs": "3857"},
    )

    _assert_status(resp, 200)
    assert resp.json()["crs"] == "EPSG:3857"
    body = json.loads(uploads[0]["body"])
    assert body["crs"] == {"type": "name", "properties": {"name": "EPSG:3857"}}
    x, y = body["features"][0]["geometry"]["coordinates"]
    assert x == pytest.approx(1113194.9, abs=1)
    assert y == pytest.approx(0.0, abs=1e-6)


def test_export_csv_in_projected_crs_uses_x_y_columns(client, annotation_set, uploads, point_annotations):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "csv", "crs": "EPSG:3857"},
    )

    _assert_status(resp, 200)
    header, row = uploads[0]["body"].decode("utf-8").splitlines()
    assert header == "id,class_id,class_name,confidence,geometry_wkt,x,y"
    assert ",POINT (1113194.9" in row


def test_default_export_stays_in_wgs84(client, annotation_set, uploads, point_annotations):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "geojson"},
    )

    _assert_status(resp, 200)
    assert resp.json()["crs"] == "EPSG:4326"
    body = json.loads(uploads[0]["body"])
    assert "crs" not in body
    assert body["features"][0]["geometry"]["coordinates"] == [10.0, 0.0]


def test_export_with_unknown_crs_is_422(client, annotation_set, uploads):
    resp = client.post(
        f"/api/v1/annotation-sets/{annotation_set.id}/export",
        json={"format": "geojson", "crs": "EPSG:999999"},
    )

    _assert_status(resp, 422)
    assert uploads == []


def test_export_annotations_node_reprojects(monkeypatch):
    from app.automation.nodes.map_overlay import execute_export_annotations

    row = SimpleNamespace(
        id=uuid4(),
        class_id=CLASS_ID,
        geojson='{"type":"Point","coordinates":[10,0]}',
        confidence=None,
        properties={},
        class_name="Palm",
    )
    session = MagicMock()
    session.execute.return_value.all.return_value = [row]
    written = {}

    def _upload_from_path(org_id, key, path, *, content_type):
        with open(path, encoding="utf-8") as f:
            written.update(body=f.read())

    monkeypatch.setattr("app.services.storage_service.upload_from_path", _upload_from_path)
    monkeypatch.setattr("app.services.storage_service.bucket_name", lambda _org: "org-bucket")

    execute_export_annotations(
        session,
        {"format": "geojson", "crs": "EPSG:3857"},
        {"annotation_set": {"id": str(uuid4())}},
        organization_id=str(uuid4()),
        run_id=str(uuid4()),
    )

    body = json.loads(written["body"])
    assert body["crs"]["properties"]["name"] == "EPSG:3857"
    assert body["features"][0]["geometry"]["coordinates"][0] == pytest.approx(1113194.9, abs=1)
