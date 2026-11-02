import gzip

import httpx
import pytest

from app.api.v1.endpoints import tiles


@pytest.mark.parametrize("query, expected", [
    ("", "assets=data"),
    ("rescale=0,1000", "rescale=0,1000&assets=data"),
    ("assets=red&assets=green", "assets=red&assets=green"),
    ("expression=red/green", "expression=red/green"),
])
async def test_raster_options_and_headers(monkeypatch, query, expected):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, content=b"raster-bytes", headers={
            "cache-control": "public, max-age=60", "etag": '"raster-v1"',
            "last-modified": "Fri, 18 Sep 2026 12:00:00 GMT",
        })

    async with httpx.AsyncClient(
        base_url="http://titiler", transport=httpx.MockTransport(upstream)
    ) as upstream_client:
        monkeypatch.setattr(tiles, "_get_proxy_client", lambda: upstream_client)
        response = await tiles._proxy_tile("/stac/0/0/0.png", query)

    assert len(requests) == 1
    assert requests[0].url.path == "/stac/0/0/0.png"
    assert requests[0].url.query.decode() == expected
    assert response.status_code == 200
    assert response.body == b"raster-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=60"
    assert response.headers["etag"] == '"raster-v1"'
    assert response.headers["last-modified"] == "Fri, 18 Sep 2026 12:00:00 GMT"


async def test_raster_gzip_body_and_length(monkeypatch):
    body = b"raster-pixels" * 100

    def upstream(request):
        return httpx.Response(200, content=gzip.compress(body), headers={
            "content-type": "image/webp", "content-encoding": "gzip",
        })

    async with httpx.AsyncClient(
        base_url="http://titiler", transport=httpx.MockTransport(upstream)
    ) as upstream_client:
        monkeypatch.setattr(tiles, "_get_proxy_client", lambda: upstream_client)
        response = await tiles._proxy_tile("/stac/0/0/0.webp", "")

    assert response.body == body
    assert response.headers["content-type"] == "image/webp"
    assert int(response.headers["content-length"]) == len(body)
    assert "content-encoding" not in response.headers
