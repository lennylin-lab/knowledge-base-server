"""Shared pytest fixtures.

Three fixture worlds live here:

- `client` — plain ASGI client against a fresh app, no database involved
  (health/error-envelope contract tests must pass offline).
- `db`-marked tests — use `db_session` / `db_client`, backed by a disposable
  `kb_test` database. A 1s reachability probe auto-skips those tests (with a
  visible reason) when PostgreSQL is not running, so the offline gate
  `uv run pytest` stays green.
- `es`-marked tests — use `es_client` / `es_index_name`, backed by a unique
  disposable index per test on the configured Elasticsearch node, with the
  same probe-skip contract as `db`.
- `live_redis`-marked tests — connect to a real Redis for the ARQ queue round
  trip; deselected by default AND probe-skipped when Redis is unreachable,
  so the default suite opens zero Redis connections.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from uuid import UUID

import asyncpg
import httpx
import pytest
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Side-effect import: every table (documents, chunks, chat) registers on
# Base.metadata, so schema creation and truncation always see the full set.
import app.models
from app.api.deps import SessionDep, get_agent_operation_service, get_document_service
from app.core.database import Base, get_db
from app.llm.embeddings import EmbeddingProvider
from app.main import create_app
from app.models.document import IndexStatus
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.indexer import IndexingPipeline
from app.schemas.document import DocumentCreate
from app.services.document import DocumentService
from app.services.operation import AgentOperationService
from fakes import FakeEmbeddingProvider

# Seeding callback: (provider, markdown content) -> created document id.
type Seeder = Callable[[EmbeddingProvider, str], Awaitable[UUID]]

TEST_DATABASE_URL = os.environ.get(
    "KB_TEST_DATABASE_URL", "postgresql+asyncpg://kb:kb@localhost:5432/kb_test"
)
TEST_ELASTICSEARCH_URL = os.environ.get("KB_TEST_ELASTICSEARCH_URL", "http://localhost:9200")
TEST_REDIS_URL = os.environ.get("KB_TEST_REDIS_URL", "redis://localhost:6379")


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


async def _es_probe(url: str) -> None:
    async with httpx.AsyncClient(timeout=1.0) as client:
        response = await client.get(url)
        response.raise_for_status()


def _es_reachable() -> bool:
    """1s reachability probe against the ES node; memoized per session."""
    global _es_reachable_flag
    if _es_reachable_flag is None:
        try:
            asyncio.run(_es_probe(TEST_ELASTICSEARCH_URL))
            _es_reachable_flag = True
        except (httpx.HTTPError, OSError):
            _es_reachable_flag = False
    return _es_reachable_flag


_es_reachable_flag: bool | None = None


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Auto-skip `db`/`es`-marked tests when the backend is unreachable."""
    if item.get_closest_marker("db") is not None and not _pg_reachable():
        pytest.skip("PG not reachable")
    if item.get_closest_marker("es") is not None and not _es_reachable():
        pytest.skip("Elasticsearch not reachable")
    # Probe runs only for live_redis-marked items (default run deselects them),
    # so the offline suite never opens a Redis connection.
    if item.get_closest_marker("live_redis") is not None and not _redis_reachable():
        pytest.skip("Redis not reachable")


async def _redis_probe() -> None:
    client = Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1.0)
    try:
        await client.ping()
    finally:
        await client.aclose()


def _redis_reachable() -> bool:
    """1s reachability probe against the Redis node; memoized per session."""
    global _redis_reachable_flag
    if _redis_reachable_flag is None:
        try:
            asyncio.run(_redis_probe())
            _redis_reachable_flag = True
        except Exception:
            # Any failure class (connection refused, timeout, auth, ...) means
            # unreachable for probe purposes.
            _redis_reachable_flag = False
    return _redis_reachable_flag


_redis_reachable_flag: bool | None = None


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
    """Create every table on the test DB (tests never run migrations).

    The pgvector extension is enabled here too — migration 0001 only ran
    against the dev database, and `document_chunks.embedding` needs the type.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
    """Empty every table between tests; CASCADE covers FK relations.

    The deterministic default tenant is re-inserted afterwards: Stage 5
    services require a tenant scope (FK-enforced), and the test database is
    schema-created (never migrated), so the 0010 backfill row must be seeded
    here — the same fixed id the migration uses.
    """
    table_names = ", ".join(Base.metadata.tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, 'default', 'Default tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": DEFAULT_TENANT_ID},
        )


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
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the per-test engine (pipeline/CLI tests)."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
def fake_embedding_provider() -> FakeEmbeddingProvider:
    """Shared deterministic provider (1536-dim, matching the DB column)."""
    return FakeEmbeddingProvider()


@pytest.fixture
def tenant_id() -> UUID:
    """The deterministic default tenant every seeded row belongs to.

    The same constant migration 0010 backfills; the `db_engine` fixture
    seeds the matching `tenants` row, so FK-enforced writes succeed.
    """
    return DEFAULT_TENANT_ID


@pytest.fixture
async def seed_indexed(
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index_name: str,
) -> AsyncIterator[Seeder]:
    """Seed one document exactly as production does: service write -> pipeline.

    The provider is a parameter (retrieval tests script its vector map before
    seeding so the vector leg has known neighbors). No manual ES writes.
    """

    async def seed(provider: EmbeddingProvider, content: str) -> UUID:
        async with session_factory() as session:
            created = await DocumentService(session).create_document(
                DocumentCreate(content=content), tenant_id=DEFAULT_TENANT_ID
            )
        pipeline = IndexingPipeline(
            session_factory=session_factory,
            embedding_provider=provider,
            es_client=es_client,
            es_index=es_index_name,
        )
        assert await pipeline.process_document(created.id, DEFAULT_TENANT_ID) is IndexStatus.DONE
        return created.id

    yield seed


@pytest.fixture
async def es_client() -> AsyncIterator[AsyncElasticsearch]:
    """ES client for tests; paired with `es_index_name` for disposability."""
    if not _es_reachable():
        pytest.skip("Elasticsearch not reachable")
    client = AsyncElasticsearch(hosts=[TEST_ELASTICSEARCH_URL], request_timeout=30)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def es_index_name(es_client: AsyncElasticsearch) -> AsyncIterator[str]:
    """A unique index name per test; whatever was created is deleted after."""
    name = f"kb_documents_test-{uuid.uuid4().hex[:12]}"
    yield name
    exists = await es_client.indices.exists(index=name)
    if bool(exists):
        await es_client.indices.delete(index=name)


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
    """ASGI client with `get_db` overridden onto the test database.

    The document service is re-created WITHOUT an enqueuer: background
    indexing against dev settings (real ES/OpenAI) must not fire from contract
    tests. The write-path wiring has its own end-to-end test.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def override_get_document_service(session: SessionDep) -> DocumentService:
        return DocumentService(session)

    async def override_get_operation_service(session: SessionDep) -> AgentOperationService:
        # No enqueuer and no LLM wiring: contract tests must not fire
        # background indexing or construct a model (same policy as the
        # document-service override above).
        return AgentOperationService(session)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_document_service] = override_get_document_service
    app.dependency_overrides[get_agent_operation_service] = override_get_operation_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()
