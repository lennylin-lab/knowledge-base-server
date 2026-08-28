"""Shared pytest fixtures.

Two worlds live here:

- `client` — plain ASGI client against a fresh app, no database involved
  (health/error-envelope contract tests must pass offline).
- `db`-marked tests — use `db_session` / `db_client`, backed by a disposable
  `kb_test` database. A 1s reachability probe auto-skips those tests (with a
  visible reason) when PostgreSQL is not running, so the offline gate
  `uv run pytest` stays green.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.database import Base, get_db
from app.main import create_app

TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL", "postgresql+asyncpg://kb:kb@localhost:5432/kb_test"
)


def _as_asyncpg_dsn(sqlalchemy_url: str, *, database: str | None = None) -> str:
    """Render a raw-asyncpg DSN (drop `+asyncpg`, keep the password).

    `str(URL)` would mask the password as `***`, so render explicitly.
    """
    url = make_url(sqlalchemy_url).set(drivername="postgresql")
    if database is not None:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


async def _probe() -> None:
    # Probe the maintenance DB: `kb_test` may not exist yet, but the `postgres`
    # database always does — reachability is about the server, not the schema.
    conn = await asyncpg.connect(
        _as_asyncpg_dsn(TEST_DATABASE_URL, database="postgres"), timeout=1.0
    )
    await conn.close()


def _pg_reachable() -> bool:
    """1s reachability probe against the PG host; memoized per session."""
    global _reachable
    if _reachable is None:
        try:
            asyncio.run(_probe())
            _reachable = True
        except (OSError, asyncpg.PostgresError):
            # OSError: refused/unreachable; PostgresError: PG-side rejection.
            _reachable = False
    return _reachable


_reachable: bool | None = None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Auto-skip `db`-marked tests when PG is unreachable (visible reason)."""
    if item.get_closest_marker("db") is not None and not _pg_reachable():
        pytest.skip("PG not reachable")


async def _create_test_database() -> None:
    """Create `kb_test` via the `postgres` maintenance DB (idempotent)."""
    url = make_url(TEST_DATABASE_URL)
    maintenance_url = url.set(database="postgres").render_as_string(hide_password=False)
    # AUTOCOMMIT from the start: CREATE DATABASE cannot run inside a
    # transaction block, and switching isolation after a read fails.
    maintenance = create_async_engine(
        maintenance_url, pool_pre_ping=True, isolation_level="AUTOCOMMIT"
    )
    try:
        async with maintenance.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": url.database},
            )
            if exists.scalar_one_or_none() is None:
                # An identifier cannot be a bound parameter, so f-string is the
                # only form; the name comes from trusted env/test config.
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await maintenance.dispose()


async def _create_schema() -> None:
    """Create every table on the test DB (tests never run migrations)."""
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Ensure the disposable `kb_test` database exists with a fresh schema."""
    if not _pg_reachable():
        pytest.skip("PG not reachable")
    asyncio.run(_create_test_database())
    asyncio.run(_create_schema())
    yield TEST_DATABASE_URL


async def _truncate(engine: AsyncEngine) -> None:
    """Empty every table between tests; CASCADE covers future FK relations."""
    table_names = ", ".join(Base.metadata.tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))


@pytest.fixture
async def db_engine(test_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Per-test engine (fresh event loop per test — connections must not
    outlive it); truncates so tests start from an empty table."""
    engine = create_async_engine(test_database_url)
    await _truncate(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session for service/repository-level tests."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """A fresh app instance per test (factory pattern keeps tests isolated)."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI-transport HTTP client — no live server, no network, no database."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
async def db_client(app: FastAPI, db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """ASGI client with `get_db` overridden onto the test database."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()
