"""Health endpoint and error-envelope contract."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_returns_ok(client: AsyncClient) -> None:
    resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_healthz_response_carries_request_id_header(client: AsyncClient) -> None:
    resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"]


async def test_unknown_route_returns_not_found_envelope(client: AsyncClient) -> None:
    resp = await client.get("/nope")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"]
    assert body["error"]["details"] == {}
    assert resp.headers["X-Request-ID"]


async def test_method_not_allowed_returns_envelope(client: AsyncClient) -> None:
    resp = await client.post("/healthz")

    assert resp.status_code == 405
    assert resp.json()["error"]["code"] == "method_not_allowed"
