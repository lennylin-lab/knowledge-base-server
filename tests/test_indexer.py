"""Indexing pipeline: status transitions, replace semantics, failure paths.

ES operations are injected recording fakes here (the indexer suite stays `db`
only); the real ES functions are covered by test_es_store.py and by the
end-to-end API test below (db + es).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.api.deps import get_db
from app.core.exceptions import LLMProviderError, SearchIndexError
from app.models.document import Document, IndexStatus
from app.models.document_chunk import EMBEDDING_DIM, DocumentChunk
from app.rag.chunker import chunk_markdown
from app.rag.indexer import IndexingPipeline
from app.repositories.document import DocumentRepository
from app.repositories.document_chunk import DocumentChunkRepository
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.services.document import DocumentService
from fakes import RecordingEsStore

pytestmark = pytest.mark.db

# Sections >= target (800) so each becomes its own chunk deterministically.
TWO_SECTIONS = f"# One\n\n{'a' * 900}\n\n# Two\n\n{'b' * 900}"
ONE_SECTION = f"# Only\n\n{'c' * 900}"


def make_pipeline(
    session_factory: async_sessionmaker[AsyncSession],
    provider: object,
    es_store: RecordingEsStore,
) -> IndexingPipeline:
    return IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=provider,
        es_client=object(),  # opaque handle for the injected fakes
        es_index="kb_documents_test",
        ensure_index=es_store.ensure_index,
        replace_chunks=es_store.replace_chunks,
    )


async def seed_document(session_factory: async_sessionmaker[AsyncSession], content: str) -> UUID:
    async with session_factory() as session:
        service = DocumentService(session)  # no enqueuer: explicit triggering
        created = await service.create_document(DocumentCreate(content=content))
        return created.id


async def index_status_of(
    session_factory: async_sessionmaker[AsyncSession], doc_id: UUID
) -> IndexStatus:
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_id)
        assert document is not None
        return document.index_status


async def chunk_rows(
    session_factory: async_sessionmaker[AsyncSession], doc_id: UUID
) -> list[DocumentChunk]:
    async with session_factory() as session:
        return list(await DocumentChunkRepository(session).list_for_document(doc_id))


async def test_process_document_marks_done_and_stores_chunks_everywhere(
    session_factory, fake_embedding_provider
):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(
        session_factory, f"---\ntitle: Indexed\ntags: [x, y]\n---\n\n{TWO_SECTIONS}"
    )

    result = await pipeline.process_document(doc_id)

    assert result is IndexStatus.DONE
    assert await index_status_of(session_factory, doc_id) is IndexStatus.DONE

    chunks = await chunk_rows(session_factory, doc_id)
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert [len(chunk.embedding) for chunk in chunks] == [EMBEDDING_DIM] * 2
    assert any(chunk.embedding[0] != 0.0 for chunk in chunks)
    assert chunks[0].content.startswith("# One")
    assert chunks[1].content.startswith("# Two")

    assert es_store.ensure_calls == ["kb_documents_test"]
    assert len(es_store.replace_calls) == 1
    call = es_store.replace_calls[0]
    assert call["document_id"] == doc_id
    assert call["title"] == "Indexed"
    assert call["tags"] == ["x", "y"]
    assert len(call["chunks"]) == 2


async def test_update_fully_replaces_chunks_in_both_stores(
    session_factory, fake_embedding_provider
):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(session_factory, TWO_SECTIONS)
    assert await pipeline.process_document(doc_id) is IndexStatus.DONE
    assert len(await chunk_rows(session_factory, doc_id)) == 2

    async with session_factory() as session:
        await DocumentService(session).update_document(doc_id, DocumentUpdate(content=ONE_SECTION))

    assert await pipeline.process_document(doc_id) is IndexStatus.DONE

    chunks = await chunk_rows(session_factory, doc_id)
    assert len(chunks) == 1  # no stale rows from the previous version
    assert chunks[0].content == ONE_SECTION
    assert len(es_store.replace_calls) == 2
    assert len(es_store.replace_calls[1]["chunks"]) == 1


async def test_embedding_failure_marks_failed_then_retry_succeeds(
    session_factory, fake_embedding_provider
):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(session_factory, TWO_SECTIONS)
    fake_embedding_provider.error = LLMProviderError("provider exploded with secret detail")

    with capture_logs() as logs:
        result = await pipeline.process_document(doc_id)

    assert result is IndexStatus.FAILED
    assert await index_status_of(session_factory, doc_id) is IndexStatus.FAILED
    assert await chunk_rows(session_factory, doc_id) == []
    assert es_store.replace_calls == []  # ES never touched on embed failure

    failures = [entry for entry in logs if entry["event"] == "document_index_failed"]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    assert failures[0]["error_class"] == "LLMProviderError"
    assert "secret detail" not in str(failures)  # no content in logs

    # Retry after failure: the document is still processable.
    fake_embedding_provider.error = None
    assert await pipeline.process_document(doc_id) is IndexStatus.DONE
    assert await index_status_of(session_factory, doc_id) is IndexStatus.DONE


async def test_es_failure_marks_failed_but_keeps_staged_chunks(
    session_factory, fake_embedding_provider
):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(session_factory, TWO_SECTIONS)
    es_store.error = SearchIndexError("Search index operation failed")

    result = await pipeline.process_document(doc_id)

    assert result is IndexStatus.FAILED
    # PG chunks committed before the ES stage — staging survives the failure.
    assert len(await chunk_rows(session_factory, doc_id)) == 2

    es_store.error = None
    assert await pipeline.process_document(doc_id) is IndexStatus.DONE


async def test_zero_chunk_document_completes_done(session_factory, fake_embedding_provider):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(session_factory, "---\ntitle: Empty\n---\n")

    result = await pipeline.process_document(doc_id)

    assert result is IndexStatus.DONE
    assert await chunk_rows(session_factory, doc_id) == []
    assert es_store.replace_calls[0]["chunks"] == []


async def test_missing_document_is_a_noop(session_factory, fake_embedding_provider):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)

    result = await pipeline.process_document(uuid4())

    assert result is None
    assert es_store.ensure_calls == []
    assert es_store.replace_calls == []


async def test_soft_deleted_document_is_skipped(session_factory, fake_embedding_provider):
    es_store = RecordingEsStore()
    pipeline = make_pipeline(session_factory, fake_embedding_provider, es_store)
    doc_id = await seed_document(session_factory, TWO_SECTIONS)
    async with session_factory() as session:
        await DocumentService(session).delete_document(doc_id)

    result = await pipeline.process_document(doc_id)

    assert result is None
    assert await chunk_rows(session_factory, doc_id) == []
    assert es_store.replace_calls == []
    async with session_factory() as session:
        document = await session.get(Document, doc_id)
        assert document is not None
        assert document.index_status is IndexStatus.PENDING  # untouched


# --- end-to-end: API write -> BackgroundTasks -> pipeline (real ES) ---


@pytest.fixture
async def indexing_client(
    app, db_engine, es_client, es_index_name, fake_embedding_provider, monkeypatch
):
    """ASGI client wiring the REAL deps enqueuer onto a test pipeline.

    Only `run_indexing` is patched (with the same call shape the deps lambda
    schedules); the FastAPI BackgroundTasks adapter itself runs for real.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    pipeline = IndexingPipeline(
        session_factory=factory,
        embedding_provider=fake_embedding_provider,
        es_client=es_client,
        es_index=es_index_name,
    )

    async def run(doc_id: UUID) -> None:
        await pipeline.process_document(doc_id)

    monkeypatch.setattr("app.api.deps.run_indexing", run)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.es
async def test_api_write_triggers_background_indexing_end_to_end(
    indexing_client, session_factory, es_client, es_index_name
):
    content = f"---\ntitle: E2E Note\ntags: [e2e]\n---\n\n{TWO_SECTIONS}"
    created = await indexing_client.post("/api/v1/documents", json={"content": content})

    assert created.status_code == 201
    doc_id = created.json()["id"]

    # BackgroundTasks run before the ASGI call completes, so by now: done.
    fetched = await indexing_client.get(f"/api/v1/documents/{doc_id}")
    assert fetched.status_code == 200
    assert fetched.json()["index_status"] == "done"

    expected_chunks = chunk_markdown(content)
    assert len(expected_chunks) == 2
    assert len(await chunk_rows(session_factory, UUID(doc_id))) == 2

    await es_client.indices.refresh(index=es_index_name)
    es_count = await es_client.count(index=es_index_name, query={"term": {"document_id": doc_id}})
    assert es_count["count"] == 2

    # Update path: response unaffected, stores fully replaced with less content.
    patched = await indexing_client.patch(
        f"/api/v1/documents/{doc_id}", json={"content": f"---\ntitle: Shrunk\n---\n\n{ONE_SECTION}"}
    )
    assert patched.status_code == 200
    assert patched.json()["index_status"] == "pending"

    refetched = await indexing_client.get(f"/api/v1/documents/{doc_id}")
    assert refetched.json()["index_status"] == "done"
    assert len(await chunk_rows(session_factory, UUID(doc_id))) == 1
    await es_client.indices.refresh(index=es_index_name)
    es_count_after = await es_client.count(
        index=es_index_name, query={"term": {"document_id": doc_id}}
    )
    assert es_count_after["count"] == 1


@pytest.mark.es
async def test_api_response_unaffected_when_background_indexing_fails(
    indexing_client, session_factory, fake_embedding_provider
):
    fake_embedding_provider.error = LLMProviderError("provider down")

    created = await indexing_client.post(
        "/api/v1/documents", json={"content": f"---\ntitle: Doomed\n---\n\n{ONE_SECTION}"}
    )

    # The write itself succeeded; the pipeline failure never touches the path.
    assert created.status_code == 201
    doc_id = UUID(created.json()["id"])
    async with session_factory() as session:
        document = await session.get(Document, doc_id)
        assert document is not None
        assert document.index_status is IndexStatus.FAILED
