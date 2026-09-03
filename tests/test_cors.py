"""CORS middleware is added only when `KB_CORS_ORIGINS` is configured."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def cors_origins(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    """Set `KB_CORS_ORIGINS` (None = unset) and clear the settings cache."""
    if request.param is None:
        monkeypatch.delenv("KB_CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("KB_CORS_ORIGINS", request.param)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("cors_origins", ["[]"], indirect=True)
async def test_no_cors_headers_when_unconfigured(
    cors_origins: None,
) -> None:
    """No origins configured = middleware not added = no CORS headers."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/healthz", headers={"Origin": "http://localhost:5173"})

    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize("cors_origins", ['["*"]'], indirect=True)
async def test_cors_allows_wildcard_origin(cors_origins: None) -> None:
    """Configured origins get CORS headers on cross-origin requests."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.get("/healthz", headers={"Origin": "http://localhost:5173"})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("cors_origins", ['["http://localhost:5173"]'], indirect=True)
async def test_cors_echoes_matching_origin_and_rejects_others(
    cors_origins: None,
) -> None:
    """Explicit origins: matching origin is echoed, others get no header."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        allowed = await ac.get("/healthz", headers={"Origin": "http://localhost:5173"})
        denied = await ac.get("/healthz", headers={"Origin": "http://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers
