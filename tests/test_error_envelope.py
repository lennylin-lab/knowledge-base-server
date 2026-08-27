"""Error-envelope contract on the AppError / 422 / 500 handler paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import NotFoundError


@asynccontextmanager
async def envelope_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI client that surfaces handler responses instead of re-raising.

    ServerErrorMiddleware always re-raises the exception after sending the
    500 response, so `raise_app_exceptions=False` is required to assert on it.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_app_error_returns_envelope_with_serialized_details(app: FastAPI) -> None:
    doc_id = UUID("123e4567-e89b-12d3-a456-426614174000")

    @app.get("/missing-doc")
    async def missing_doc() -> dict[str, str]:
        raise NotFoundError("Document not found", details={"document_id": doc_id})

    async with envelope_client(app) as client:
        resp = await client.get("/missing-doc")

    assert resp.status_code == 404
    body = resp.json()["error"]
    assert body["code"] == "not_found"
    assert body["message"] == "Document not found"
    # Non-JSON detail values (UUID here) must serialize, not crash the handler.
    assert body["details"] == {"document_id": str(doc_id)}
    assert resp.headers["X-Request-ID"]


async def test_unhandled_exception_returns_generic_500_envelope(app: FastAPI) -> None:
    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("secret internals")

    async with envelope_client(app) as client:
        resp = await client.get("/boom")

    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["code"] == "internal_error"
    # Generic message only — the exception text must not leak to the client.
    assert "secret" not in body["message"]
    assert body["details"] == {}
    # The 500 is emitted outside the request-id middleware, yet the header
    # must still be echoed for correlation.
    assert resp.headers["X-Request-ID"]


async def test_request_validation_error_returns_422_envelope(app: FastAPI) -> None:
    @app.get("/echo")
    async def echo(x: int) -> dict[str, int]:
        return {"x": x}

    async with envelope_client(app) as client:
        resp = await client.get("/echo", params={"x": "abc"})

    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "validation_failed"
    assert body["details"]["errors"]  # pydantic error list is preserved
