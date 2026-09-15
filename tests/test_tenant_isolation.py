"""Cross-tenant isolation regressions (Stage 5).

Every read, write, delete, search and operation transition is tenant-scoped;
a cross-tenant id resolves as NOT FOUND (one non-leaky 404 — existence in
another tenant is never disclosed). Covers documents, chat sessions and
history, agent operations (including the optimistic-concurrency apply), the
pgvector leg, ES at the query-builder level and end-to-end, and the
background indexing paths (the payload carries the tenant; an out-of-scope
or legacy job skips instead of running unscoped).

`db`-marked tests auto-skip without PostgreSQL; `es`-marked ones without
Elasticsearch; everything else is fully offline.
"""

from __future__ import annotations

import dataclasses
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.agents.qa import ChatDeps
from app.core.exceptions import NotFoundError
from app.models.chat import ChatSession
from app.models.document import IndexStatus
from app.models.operation import AgentOperation, OperationState
from app.models.tenant import DEFAULT_TENANT_ID, Tenant
from app.rag.indexer import IndexingPipeline
from app.rag.retriever import Retriever
from app.rag.worker import index_document
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.repositories.document import DocumentRepository
from app.repositories.document_chunk import DocumentChunkRepository
from app.repositories.operation import AgentOperationRepository, DocumentRevisionRepository
from app.schemas.document import DocumentCreate, DocumentUpdate
from app.schemas.operation import ApplyRequest, DraftContent, OperationCreate, OperationTransition
from app.search.queries import bm25_chunk_query
from app.services.chat import ChatService
from app.services.document import DocumentService
from app.services.operation import AgentOperationService
from app.services.session import ChatSessionService
from fakes import StubRetriever, scripted_chat_model

# A second, distinct tenant: nothing of tenant A may ever surface here.
TENANT_B_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
TENANT_B_SLUG = "tenant-b"

DOC_A = "---\ntitle: Tenant A Doc\ntags: [alpha]\n---\n\nTenant A body."
DOC_B = "---\ntitle: Tenant B Doc\ntags: [beta]\n---\n\nTenant B body."


async def _ensure_tenant_b(session: AsyncSession) -> None:
    session.add(Tenant(id=TENANT_B_ID, slug=TENANT_B_SLUG, name="Tenant B"))
    await session.commit()


@pytest.fixture(autouse=True)
async def tenant_b(db_engine):
    """Seed tenant B (the isolation probe tenant) for every db test."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await _ensure_tenant_b(session)


async def _make_document(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID, content: str
) -> UUID:
    async with session_factory() as session:
        created = await DocumentService(session).create_document(
            DocumentCreate(content=content), tenant_id=tenant_id
        )
        return created.id


async def _make_session(session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID) -> UUID:
    async with session_factory() as session:
        chat_session = await ChatSessionRepository(session).create(
            ChatSession(tenant_id=tenant_id, title="t")
        )
        await session.commit()
        return chat_session.id


# --- documents ---


@pytest.mark.db
async def test_document_get_update_delete_invisible_cross_tenant(db_session, session_factory):
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    service_b = DocumentService(db_session)

    with pytest.raises(NotFoundError):
        await service_b.get_document(doc_a, tenant_id=TENANT_B_ID)
    with pytest.raises(NotFoundError):
        await service_b.update_document(
            doc_a, DocumentUpdate(title="hacked"), tenant_id=TENANT_B_ID
        )
    with pytest.raises(NotFoundError):
        await service_b.delete_document(doc_a, tenant_id=TENANT_B_ID)

    # Tenant B's listing never contains tenant A's document; tenant A still
    # sees its own after all the rejected cross-tenant attempts (zero writes).
    assert (await service_b.list_documents(tenant_id=TENANT_B_ID)).items == []
    async with session_factory() as session:
        listed_a = await DocumentService(session).list_documents(tenant_id=DEFAULT_TENANT_ID)
    assert [item.id for item in listed_a.items] == [doc_a]


@pytest.mark.db
async def test_document_create_uses_callers_tenant(db_session, session_factory):
    doc_b = await _make_document(session_factory, TENANT_B_ID, DOC_B)
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_b, tenant_id=TENANT_B_ID)
        assert document is not None
        assert document.tenant_id == TENANT_B_ID


# --- chat sessions and history ---


@pytest.mark.db
async def test_session_get_delete_list_invisible_cross_tenant(db_session, session_factory):
    session_a = await _make_session(session_factory, DEFAULT_TENANT_ID)
    service_b = ChatSessionService(db_session)

    with pytest.raises(NotFoundError):
        await service_b.get_session(session_a, tenant_id=TENANT_B_ID)
    with pytest.raises(NotFoundError):
        await service_b.delete_session(session_a, tenant_id=TENANT_B_ID)
    assert (await service_b.list_sessions(tenant_id=TENANT_B_ID)).items == []


@pytest.mark.db
async def test_session_messages_scoped_by_tenant(db_session, session_factory):
    session_a = await _make_session(session_factory, DEFAULT_TENANT_ID)
    messages_b = ChatMessageRepository(db_session)
    assert await messages_b.list_for_session(session_a, tenant_id=TENANT_B_ID) == []
    assert (
        await messages_b.list_recent_for_session(session_a, tenant_id=TENANT_B_ID, limit=10) == []
    )


@pytest.mark.db
async def test_chat_ask_cross_tenant_session_raises_not_found(session_factory):
    session_a = await _make_session(session_factory, DEFAULT_TENANT_ID)
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(tool_calls=[], answer_parts=["unused"]),
        mode="bm25",
        session_factory=session_factory,
        token_counter=lambda text: len(text),
    )
    with pytest.raises(NotFoundError):
        # Tenant B asking on tenant A's session: one non-leaky 404.
        async for _ in service.ask("hi", session_id=session_a, tenant_id=TENANT_B_ID):
            pass


@pytest.mark.db
async def test_chat_ask_creates_session_in_callers_tenant(session_factory):
    service = ChatService(
        StubRetriever(),
        scripted_chat_model(tool_calls=[], answer_parts=["answer"]),
        mode="bm25",
        session_factory=session_factory,
        token_counter=lambda text: len(text),
    )
    events = [event async for event in service.ask("hi", tenant_id=TENANT_B_ID)]
    session_id = next(e.session_id for e in events if getattr(e, "session_id", None))
    async with session_factory() as session:
        chat_session = await ChatSessionRepository(session).get_by_id(
            session_id, tenant_id=TENANT_B_ID
        )
        assert chat_session is not None
        assert chat_session.tenant_id == TENANT_B_ID


@pytest.mark.db
async def test_chat_agent_retrieval_tool_receives_tenant_scope(session_factory):
    retriever = StubRetriever()
    service = ChatService(
        retriever,
        scripted_chat_model(tool_calls=["zorblat"], answer_parts=["answer"]),
        mode="bm25",
        session_factory=session_factory,
        token_counter=lambda text: len(text),
    )
    async for _ in service.ask("question", tenant_id=TENANT_B_ID):
        pass
    assert retriever.calls
    assert all(call[2] == TENANT_B_ID for call in retriever.calls)


# --- agent operations (including the apply flow) ---


@pytest.mark.db
async def test_operation_read_resume_apply_invisible_cross_tenant(db_session, session_factory):
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    async with session_factory() as session:
        operation = await AgentOperationRepository(session).create(
            AgentOperation(
                tenant_id=DEFAULT_TENANT_ID,
                document_id=doc_a,
                state=OperationState.COMPLETED,
                draft={"content": DOC_A, "title": None},
            )
        )
        await session.commit()
        operation_id = operation.id

    service_b = AgentOperationService(db_session)
    with pytest.raises(NotFoundError):
        await service_b.get_operation(operation_id, tenant_id=TENANT_B_ID)
    with pytest.raises(NotFoundError):
        await service_b.resume_operation(operation_id, OperationTransition(), tenant_id=TENANT_B_ID)
    with pytest.raises(NotFoundError):
        await service_b.apply_operation(operation_id, ApplyRequest(), tenant_id=TENANT_B_ID)
    with pytest.raises(NotFoundError):
        await service_b.list_operations(doc_a, tenant_id=TENANT_B_ID)


@pytest.mark.db
async def test_apply_flow_tenant_scoped_end_to_end(db_session, session_factory):
    """The optimistic-concurrency apply keeps the tenant boundary: a tenant B
    caller can neither see tenant A's operation nor its document; tenant A's
    own apply writes a revision carrying the SAME tenant."""
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_a, tenant_id=DEFAULT_TENANT_ID)
        assert document is not None
    async with session_factory() as session:
        service = AgentOperationService(session)
        operation = await service.create_operation(
            OperationCreate(
                document_id=doc_a,
                base_document_version=document.updated_at,
                draft=DraftContent(content=DOC_A, title=None),
            ),
            tenant_id=DEFAULT_TENANT_ID,
        )
        base_version = operation.base_document_version

    # Tenant B's apply: the operation does not exist for it — zero writes.
    service_b = AgentOperationService(db_session)
    with pytest.raises(NotFoundError):
        await service_b.apply_operation(operation.id, ApplyRequest(), tenant_id=TENANT_B_ID)

    async with session_factory() as session:
        service_a = AgentOperationService(session)
        result = await service_a.apply_operation(
            operation.id,
            ApplyRequest(expected_base_document_version=base_version),
            tenant_id=DEFAULT_TENANT_ID,
        )
        assert result.revision is not None
        revision_id = result.revision.id

    async with session_factory() as session:
        revision = await DocumentRevisionRepository(session).get_by_id(
            revision_id, tenant_id=DEFAULT_TENANT_ID
        )
        assert revision is not None
        assert revision.tenant_id == DEFAULT_TENANT_ID
        # Tenant B cannot read the revision, the operation, or the document.
        assert (
            await DocumentRevisionRepository(session).get_by_id(revision_id, tenant_id=TENANT_B_ID)
            is None
        )
        assert (
            await AgentOperationRepository(session).get_by_id(operation.id, tenant_id=TENANT_B_ID)
            is None
        )
        assert await DocumentRepository(session).get_by_id(doc_a, tenant_id=TENANT_B_ID) is None


# --- pgvector leg + hydration ---


@pytest.mark.db
async def test_vector_leg_and_hydration_scoped_by_tenant(db_session, session_factory):
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    async with session_factory() as session:
        chunks = DocumentChunkRepository(session)
        await chunks.replace_for_document(doc_a, ["chunk of A"], [[0.1] * 1536])

        rows = await chunks.search_similar([0.1] * 1536, tenant_id=DEFAULT_TENANT_ID, limit=10)
        assert [row.document_id for row in rows] == [doc_a]
        # Tenant B's leg returns nothing — the PG join filters on tenant.
        assert await chunks.search_similar([0.1] * 1536, tenant_id=TENANT_B_ID, limit=10) == []
        assert await chunks.get_live_chunks([(doc_a, 0)], tenant_id=TENANT_B_ID) == {}
        assert await chunks.first_chunk_content(doc_a, tenant_id=TENANT_B_ID) is None
        assert await chunks.find_neighbor_documents(doc_a, tenant_id=TENANT_B_ID, limit=10) == []


# --- ES: query builder (offline) + end-to-end search ---


def test_bm25_query_carries_structural_tenant_filter():
    body = bm25_chunk_query("q", size=10, tenant_id=str(DEFAULT_TENANT_ID))
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": str(DEFAULT_TENANT_ID)}} in filters


def test_bm25_query_tenant_and_tag_filters_combine():
    body = bm25_chunk_query("q", size=10, tenant_id="t-1", tag="x")
    assert body["query"]["bool"]["filter"] == [
        {"term": {"tenant_id": "t-1"}},
        {"term": {"tags": "x"}},
    ]


@pytest.mark.db
@pytest.mark.es
async def test_search_never_leaks_across_tenants_end_to_end(
    db_session, session_factory, es_client, es_index_name, fake_embedding_provider
):
    """Both docs are indexed into the SAME ES index (the stale-index attack
    surface); retrieval still returns each tenant only its own chunks."""
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    doc_b = await _make_document(session_factory, TENANT_B_ID, DOC_B)
    pipeline = IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=fake_embedding_provider,
        es_client=es_client,
        es_index=es_index_name,
    )
    assert await pipeline.process_document(doc_a, DEFAULT_TENANT_ID) is IndexStatus.DONE
    assert await pipeline.process_document(doc_b, TENANT_B_ID) is IndexStatus.DONE

    retriever = Retriever(
        session_factory=session_factory,
        es_client=es_client,
        embedding_provider=fake_embedding_provider,
        es_index=es_index_name,
        max_query_length=256,
    )
    outcome_a = await retriever.retrieve("body", limit=10, tenant_id=DEFAULT_TENANT_ID)
    outcome_b = await retriever.retrieve("body", limit=10, tenant_id=TENANT_B_ID)
    assert {item.key.document_id for item in outcome_a.items} == {doc_a}
    assert {item.key.document_id for item in outcome_b.items} == {doc_b}


# --- background indexing paths ---


@pytest.mark.db
async def test_pipeline_skips_out_of_tenant_document(session_factory, fake_embedding_provider):
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    pipeline = IndexingPipeline(
        session_factory=session_factory,
        embedding_provider=fake_embedding_provider,
        es_client=object(),  # never reached: the job skips before any ES call
        es_index="never",
    )
    result = await pipeline.process_document(doc_a, TENANT_B_ID)
    assert result is None  # clean skip; zero embedding work
    assert fake_embedding_provider.calls == []
    assert await index_status_of(session_factory, doc_a) is IndexStatus.PENDING


async def index_status_of(
    session_factory: async_sessionmaker[AsyncSession], doc_id: UUID
) -> IndexStatus:
    async with session_factory() as session:
        document = await DocumentRepository(session).get_by_id(doc_id, tenant_id=DEFAULT_TENANT_ID)
        assert document is not None
        return document.index_status


@pytest.mark.db
async def test_index_status_update_is_tenant_scoped(db_session, session_factory):
    doc_a = await _make_document(session_factory, DEFAULT_TENANT_ID, DOC_A)
    async with session_factory() as session:
        repo = DocumentRepository(session)
        await repo.set_index_status(doc_a, IndexStatus.DONE, tenant_id=TENANT_B_ID)
        await session.commit()
    # Tenant B's UPDATE matched nothing: tenant A's row is untouched.
    assert await index_status_of(session_factory, doc_a) is IndexStatus.PENDING


async def test_legacy_queue_payload_without_tenant_is_skipped() -> None:
    """A pre-Stage-5 ARQ payload (no tenant) must never run unscoped: the
    worker skips it with a warning and touches no database."""
    valid_id = "018f0000-0000-7000-8000-000000000000"
    with capture_logs() as logs:
        await index_document({"job_try": 1}, valid_id)
    warnings = [e for e in logs if e["event"] == "index_job_missing_tenant"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"


def test_chat_deps_require_tenant_scope() -> None:
    """The agent deps dataclass has no default tenant: the scope field is
    required, so a run context without an explicit tenant cannot be built
    (and a deprecated default-to-default-tenant path can never reappear)."""
    fields = {f.name: f for f in dataclasses.fields(ChatDeps)}
    assert "tenant_id" in fields
    assert fields["tenant_id"].default is dataclasses.MISSING
    assert fields["tenant_id"].default_factory is dataclasses.MISSING
