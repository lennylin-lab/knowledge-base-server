"""Live ARQ round trip (compose Redis): enqueue -> burst worker drain -> done.

Deselected by default (`-m "not live_redis"`); run explicitly with
`uv run pytest -m live_redis` while `docker compose up redis` (and
PostgreSQL) are running — a 1s probe auto-skips when Redis is unreachable.
The pipeline is double-backed onto the test DB: this exercises the queue
mechanics (enqueue, job_try accounting, worker pickup) without touching
real providers.
"""

from __future__ import annotations

import os

import pytest
from arq import Worker, create_pool, func
from arq.connections import RedisSettings

import app.rag.worker as worker_module
from app.models.document import IndexStatus
from app.rag.indexer import IndexingPipeline
from app.rag.worker import INDEX_DOCUMENT_TASK
from app.repositories.document import DocumentRepository
from app.schemas.document import DocumentCreate
from app.services.document import DocumentService
from fakes import FakeEmbeddingProvider, RecordingEsStore, StubEsClient

TEST_REDIS_URL = os.environ.get("KB_TEST_REDIS_URL", "redis://localhost:6379")

pytestmark = [pytest.mark.live_redis, pytest.mark.db]


async def test_enqueue_worker_drain_round_trip_marks_document_done(session_factory, monkeypatch):
    async with session_factory() as session:
        created = await DocumentService(session).create_document(
            DocumentCreate(content="# Live\n\nredis round trip")
        )
    doc_id = created.id

    es_store = RecordingEsStore()
    pipeline = IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=FakeEmbeddingProvider(),
        es_client=StubEsClient(),
        es_index="kb_documents_live_redis_test",
        ensure_index=es_store.ensure_index,
        replace_chunks=es_store.replace_chunks,
    )
    # The worker's seams onto test doubles: the real run_indexing_raw would
    # build from dev settings (real OpenAI / ES).
    monkeypatch.setattr(worker_module, "run_indexing_raw", pipeline.process_document_raising)
    monkeypatch.setattr(worker_module, "SessionFactory", session_factory)

    pool = await create_pool(RedisSettings.from_dsn(TEST_REDIS_URL))
    try:
        await pool.enqueue_job(INDEX_DOCUMENT_TASK, str(doc_id))
        worker = Worker(
            functions=[func(worker_module.index_document, name=INDEX_DOCUMENT_TASK)],
            redis_pool=pool,
            burst=True,  # drain the queue, then stop
            handle_signals=False,  # no signal handlers inside the test loop
            max_tries=3,
        )
        completed = await worker.run_check()
    finally:
        await pool.aclose(close_connection_pool=True)

    assert completed == 1  # the job ran once, successfully
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_id)
        assert document is not None
        assert document.index_status is IndexStatus.DONE
