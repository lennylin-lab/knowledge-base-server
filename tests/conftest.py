"""Shared pytest fixtures.

No database in this scaffold's tests yet — the documents slice will extend
this conftest with a disposable test DB and truncation fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """A fresh app instance per test (factory pattern keeps tests isolated)."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI-transport HTTP client — no live server, no network."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
