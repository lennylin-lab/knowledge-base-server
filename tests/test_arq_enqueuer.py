"""Indexing enqueue transport: mode selection, shared pool, degradation.

Everything here is offline: the ARQ pool is a recording stub (the module
global pre-set), so the default suite opens ZERO Redis connections. The real
enqueue -> worker round trip lives under the `live_redis` marker
(test_arq_live.py).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import BackgroundTasks
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from structlog.testing import capture_logs

import app.api.deps as deps
from app.api.deps import get_db
from app.main import create_app
from app.models.document import IndexStatus
from app.repositories.document import DocumentRepository
from fakes import hermetic_settings

REDIS_URL = "redis://localhost:6379/0"

# A stand-in for the commit-time version stamp the service hands over.
ENQUEUED_AT = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


class StubArqPool:
    """Records enqueue calls; optionally raises (Redis-down scripting)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.error = error

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((function, args))
        if self.error is not None:
            raise self.error

    async def aclose(self, close_connection_pool: bool | None = None) -> None:
        """No-op: lifespan closes whatever pool is installed; tolerate it."""


async def drain_enqueues() -> None:
    """Wait for every fire-and-forget enqueue task the enqueuer started."""
    await asyncio.gather(*deps._ArqEnqueuer._inflight)


@pytest.fixture(autouse=True)
def clean_shared_arq_pool() -> Iterator[None]:
    """Reset the shared-pool global around every test in this module.

    `_get_shared_arq_pool` assigns the stub for real (that is its job), so
    monkeypatching `create_pool` alone would leak the stub into other
    modules' lifespan runs.
    """
    deps._shared_arq_pool = None
    yield
    deps._shared_arq_pool = None


# --- mode selection (the hard dual-mode regression) ---


def test_empty_redis_url_keeps_the_background_tasks_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: hermetic_settings())

    def no_pool(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no Redis pool may be constructed in BackgroundTasks mode")

    monkeypatch.setattr(deps, "create_pool", no_pool)
    background_tasks = BackgroundTasks()

    enqueuer = deps.make_index_enqueuer(background_tasks)

    doc_id = UUID(int=42)
    enqueuer(doc_id, ENQUEUED_AT)
    # Exactly today's scheduling semantics: one background task invoking
    # run_indexing with the doc id — plus the version stamp the pipeline's
    # generation guard compares against.
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    assert task.func is deps.run_indexing
    assert task.args == (doc_id, ENQUEUED_AT)
    assert task.kwargs == {}
    assert deps._shared_arq_pool is None


async def test_configured_redis_url_enqueues_on_the_shared_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: hermetic_settings(REDIS_URL=REDIS_URL))
    stub = StubArqPool()
    monkeypatch.setattr(deps, "_shared_arq_pool", stub)

    enqueuer = deps.make_index_enqueuer(BackgroundTasks())
    doc_id = UUID(int=7)

    with capture_logs() as logs:
        enqueuer(doc_id, ENQUEUED_AT)
        await drain_enqueues()

    # The payload carries the version stamp as an ISO string (JSON-safe).
    assert stub.jobs == [("index_document", (str(doc_id), ENQUEUED_AT.isoformat()))]
    enqueued = [entry for entry in logs if entry["event"] == "index_enqueued"]
    assert len(enqueued) == 1
    assert enqueued[0]["document_id"] == str(doc_id)


async def test_pool_is_built_lazily_and_shared_across_enqueuers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: hermetic_settings(REDIS_URL=REDIS_URL))
    stub = StubArqPool()
    created: list[int] = []

    async def fake_create_pool(*args: Any, **kwargs: Any) -> StubArqPool:
        created.append(1)
        return stub

    monkeypatch.setattr(deps, "create_pool", fake_create_pool)

    for _ in range(2):
        # Fresh enqueuer per request (get_document_service is per-request)...
        enqueuer = deps.make_index_enqueuer(BackgroundTasks())
        enqueuer(UUID(int=1), ENQUEUED_AT)
        await drain_enqueues()

    assert len(created) == 1  # ...but only ONE pool per process
    assert len(stub.jobs) == 2


async def test_enqueue_failure_degrades_to_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: hermetic_settings(REDIS_URL=REDIS_URL))
    monkeypatch.setattr(deps, "_shared_arq_pool", StubArqPool(error=ConnectionError("redis down")))

    enqueuer = deps.make_index_enqueuer(BackgroundTasks())

    with capture_logs() as logs:
        enqueuer(UUID(int=9), ENQUEUED_AT)  # must not raise
        await drain_enqueues()

    failures = [entry for entry in logs if entry["event"] == "index_enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["error_class"] == "ConnectionError"
    assert "redis down" not in str(failures)  # no error content in logs


async def test_lifespan_closes_the_shared_arq_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool | None] = []

    class PoolSpy:
        async def aclose(self, close_connection_pool: bool | None = None) -> None:
            closed.append(close_connection_pool)

    monkeypatch.setattr(deps, "_shared_arq_pool", PoolSpy())
    app = create_app()

    async with app.router.lifespan_context(app):
        assert closed == []

    assert closed == [True]  # pool + its connection pool
    assert deps._shared_arq_pool is None


# --- API-level: the write path over the ARQ enqueuer (stubbed pool) ---


def arq_mode_client(
    app: Any, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, stub: StubArqPool
):
    """ASGI client with the REAL enqueuer wiring in ARQ mode (stubbed pool)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> Any:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(deps, "get_settings", lambda: hermetic_settings(REDIS_URL=REDIS_URL))
    monkeypatch.setattr(deps, "_shared_arq_pool", stub)
    return factory


@pytest.mark.db
async def test_document_create_and_update_enqueue_arq_jobs(
    app: Any, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubArqPool()
    factory = arq_mode_client(app, db_engine, monkeypatch, stub)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post("/api/v1/documents", json={"content": "# One\n\nfirst"})
        assert created.status_code == 201
        doc_id = created.json()["id"]

        updated = await client.patch(
            f"/api/v1/documents/{doc_id}", json={"content": "# Two\n\nsecond"}
        )
        assert updated.status_code == 200
        await drain_enqueues()

    assert [job[0] for job in stub.jobs] == ["index_document", "index_document"]
    # str(doc_id) crosses the queue as JSON, with the version stamp (ISO)
    # alongside — the generation guard's payload.
    assert stub.jobs[0][1][0] == doc_id
    assert stub.jobs[1][1][0] == doc_id
    create_version = datetime.fromisoformat(stub.jobs[0][1][1])
    update_version = datetime.fromisoformat(stub.jobs[1][1][1])

    # The queue owns indexing now: the document itself stays pending.
    async with factory() as session:
        document = await DocumentRepository(session).get_by_id(UUID(doc_id))
        assert document is not None
        assert document.index_status is IndexStatus.PENDING
        # The update job carries the document's CURRENT version; the create
        # job carries the older one it observed at its own commit.
        assert update_version == document.updated_at
        assert create_version < update_version
    app.dependency_overrides.clear()


@pytest.mark.db
async def test_enqueue_failure_still_writes_201_and_leaves_pending(
    app: Any, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = StubArqPool(error=ConnectionError("redis down"))
    factory = arq_mode_client(app, db_engine, monkeypatch, stub)
    transport = ASGITransport(app=app)

    with capture_logs() as logs:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            created = await client.post(
                "/api/v1/documents", json={"content": "# Doomed\n\nqueue down"}
            )
        await drain_enqueues()

    # The CRUD contract is intact: the write succeeded despite the queue.
    assert created.status_code == 201
    doc_id = UUID(created.json()["id"])
    assert created.json()["index_status"] == "pending"

    async with factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_id)
        assert document is not None
        assert document.index_status is IndexStatus.PENDING

    failures = [entry for entry in logs if entry["event"] == "index_enqueue_failed"]
    assert len(failures) == 1
    assert failures[0]["error_class"] == "ConnectionError"
    app.dependency_overrides.clear()
