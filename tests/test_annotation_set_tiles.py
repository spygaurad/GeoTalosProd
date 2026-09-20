from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

from app.api.deps import get_current_org_id, get_session
from app.api.v1.endpoints import annotation_sets
from app.middleware import clerk_auth


def test_tile_without_token_is_rejected(client, monkeypatch):
    # Change only the mocked settings used by this test.
    monkeypatch.setattr(clerk_auth.settings, "ENVIRONMENT", "production")

    forward = AsyncMock()
    monkeypatch.setattr(annotation_sets, "forward_tile", forward)

    response = client.get(
        f"/api/v1/annotation-sets/{uuid4()}/tiles/14/12306/7077.pbf"
    )

    assert response.status_code == 401, response.text
    assert response.json() == {
        "detail": "Missing or malformed Authorization header"
    }
    forward.assert_not_called()


def test_tile_without_organization_is_rejected(client, monkeypatch):
    async def no_organization():
        return None

    monkeypatch.setitem(
        client.app.dependency_overrides,
        get_current_org_id,
        no_organization,
    )

    forward = AsyncMock()
    monkeypatch.setattr(annotation_sets, "forward_tile", forward)

    response = client.get(
        f"/api/v1/annotation-sets/{uuid4()}/tiles/14/12306/7077.pbf"
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Organization context required"}
    forward.assert_not_called()


def test_tile_excludes_deleted_sets(client, monkeypatch):
    result = MagicMock()
    result.scalar_one_or_none.return_value = None

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    async def fake_session():
        yield db

    monkeypatch.setitem(
        client.app.dependency_overrides,
        get_session,
        fake_session,
    )

    forward = AsyncMock()
    monkeypatch.setattr(annotation_sets, "forward_tile", forward)

    response = client.get(
        f"/api/v1/annotation-sets/{uuid4()}/tiles/14/12306/7077.pbf"
    )

    db.execute.assert_awaited_once()
    query = db.execute.await_args.args[0]
    assert "annotation_sets.deleted_at IS NULL" in str(query)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "AnnotationSet not found"}
    forward.assert_not_called()


@pytest.mark.parametrize("x,y", [(16384, 7077), (12306, 16384)])
def test_tile_out_of_bounds_never_forwards(client, monkeypatch, x, y):
    set_id = uuid4()

    get_set = AsyncMock(return_value=SimpleNamespace(id=set_id))
    monkeypatch.setattr(
        annotation_sets.AnnotationSetService, "get_set", get_set
    )

    forward = AsyncMock()
    monkeypatch.setattr(annotation_sets, "forward_tile", forward)

    response = client.get(
        f"/api/v1/annotation-sets/{set_id}/tiles/14/{x}/{y}.pbf"
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": "Tile coordinates are outside the zoom level bounds"
    }
    forward.assert_not_called()


@pytest.mark.parametrize(
    "case, expected_status, expected_detail",
    [
        ("not_found", 404, "Tile not found"),
        ("server_error", 502, "Tile service error"),
        ("timeout", 502, "Tile service unavailable"),
        ("connection_error", 502, "Tile service unavailable"),
    ],
)
def test_tile_upstream_failure(
    client, monkeypatch, case, expected_status, expected_detail
):
    set_id = uuid4()
    monkeypatch.setattr(
        annotation_sets.AnnotationSetService,
        "get_set",
        AsyncMock(return_value=SimpleNamespace(id=set_id)),
    )

    upstream_get = AsyncMock()

    if case == "timeout":
        upstream_get.side_effect = httpx.ReadTimeout("Simulated timeout")
    elif case == "connection_error":
        upstream_get.side_effect = httpx.ConnectError("Simulated connection failure")
    else:
        upstream_get.return_value = httpx.Response(
            404 if case == "not_found" else 500,
            text="Internal upstream diagnostic",
        )

    monkeypatch.setattr(
        client.app.state.martin_client, "get", upstream_get
    )

    response = client.get(
        f"/api/v1/annotation-sets/{set_id}/tiles/14/12306/7077.pbf"
    )

    assert response.status_code == expected_status, response.text
    assert response.json() == {"detail": expected_detail}
    upstream_get.assert_awaited_once_with(
        f"/annotation_set_mvt/14/12306/7077?set_id={set_id}"
    )


@pytest.mark.parametrize("upstream_status", [200, 204])
def test_tile_success_response(client, monkeypatch, upstream_status):
    set_id = uuid4()
    monkeypatch.setattr(
        annotation_sets.AnnotationSetService,
        "get_set",
        AsyncMock(return_value=SimpleNamespace(id=set_id)),
    )

    body = b"test-tile-bytes" if upstream_status == 200 else b""
    upstream_get = AsyncMock(
        return_value=httpx.Response(
            upstream_status,
            content=body,
            headers={
                "content-type": "application/x-protobuf",
                "cache-control": "public, max-age=3600",
            },
        )
    )
    monkeypatch.setattr(
        client.app.state.martin_client, "get", upstream_get
    )

    response = client.get(
        f"/api/v1/annotation-sets/{set_id}/tiles/14/12306/7077.pbf"
    )

    assert response.status_code == upstream_status, response.text
    assert response.content == body
    assert response.headers["cache-control"] == "private, no-store"

    if upstream_status == 200:
        assert response.headers["content-type"] == "application/x-protobuf"

    upstream_get.assert_awaited_once_with(
        f"/annotation_set_mvt/14/12306/7077?set_id={set_id}"
    )
