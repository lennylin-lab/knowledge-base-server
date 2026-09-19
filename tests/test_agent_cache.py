"""Agent result caches: summarize (content-hash keyed) and association
(TTL keyed) hit/miss semantics with a scripted model and a fake cache.

Uses the db fixture like the sibling service tests; the cache itself is the
offline `FakeCache` (zero Redis connections)."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppError
from app.models.document import Document as DocumentModel
from app.models.tenant import DEFAULT_TENANT_ID
from app.services.agents import AssociationService, SummarizeService
from app.services.document import (
    DocumentCreate,
    DocumentService,
    DocumentUpdate,
)
from fakes import FakeCache, scripted_association_model, scripted_summarize_model

pytestmark = pytest.mark.db

MODEL_NAME = "test-chat-model"
SHORT_DOC = "---\ntitle: Short Note\ntags: [alpha]\n---\n\nOne short paragraph about zorblat."
EDITED_DOC = "---\ntitle: Short Note\ntags: [alpha]\n---\n\nDifferent content entirely, quibnard."

ALPHA_SOURCE = (
    "---\ntitle: Alpha Notes\ntags: [alpha]\n---\n\n"
    "One short paragraph about the zorblat alpha process."
)
ALPHA_NEIGHBOR = (
    "---\ntitle: Alpha Companion\ntags: [alpha, other]\n---\n\n"
    "One short paragraph about the quibnard companion notes."
)


def failing_model() -> FunctionModel:
    """Scripted provider failure (scripted_summarize_model has no fail seam)."""

    async def function(messages: list[ModelMessage], info: object) -> ModelResponse:
        raise RuntimeError("boom")

    return FunctionModel(function, model_name="failing")


def make_summarizer(
    session_factory: async_sessionmaker[AsyncSession],
    model: FunctionModel,
    cache: FakeCache | None,
) -> SummarizeService:
    return SummarizeService(
        model,
        MODEL_NAME,
        session_factory=session_factory,
        cache=cache,
        cache_ttl_seconds=0,
    )


def make_associator(
    session_factory: async_sessionmaker[AsyncSession],
    model: FunctionModel,
    cache: FakeCache | None,
) -> AssociationService:
    return AssociationService(
        model,
        MODEL_NAME,
        session_factory=session_factory,
        cache=cache,
        cache_ttl_seconds=600,
    )


async def make_document(session: AsyncSession, content: str) -> object:
    return await DocumentService(session).create_document(
        DocumentCreate(content=content), tenant_id=DEFAULT_TENANT_ID
    )


async def test_second_summarize_unchanged_document_skips_the_agent(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    prompts: list[str] = []
    cache = FakeCache()
    service = make_summarizer(
        session_factory, scripted_summarize_model(["First summary."], prompts=prompts), cache
    )

    first = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    second = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]

    assert first.summary == "First summary."
    assert second.summary == "First summary."  # payload equals the computed one
    assert second.model == MODEL_NAME
    assert len(prompts) == 1  # agent NOT run on the hit
    assert cache.set_calls == 1  # only the computed result was stored


async def test_different_summary_max_tokens_misses_cache(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    prompts: list[str] = []
    cache = FakeCache()
    model = scripted_summarize_model(["First summary.", "Second summary."], prompts=prompts)
    first_service = SummarizeService(
        model,
        MODEL_NAME,
        session_factory=session_factory,
        cache=cache,
        summary_max_tokens=250,
    )
    second_service = SummarizeService(
        model,
        MODEL_NAME,
        session_factory=session_factory,
        cache=cache,
        summary_max_tokens=400,
    )

    first = await first_service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    second = await second_service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]

    assert first.summary == "First summary."
    assert second.summary == "Second summary."
    assert len(prompts) == 2
    assert cache.set_calls == 2


async def test_edited_content_changes_hash_so_cache_misses(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    prompts: list[str] = []
    cache = FakeCache()
    service = make_summarizer(
        session_factory,
        scripted_summarize_model(["First.", "Second."], prompts=prompts),
        cache,
    )
    await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]

    async with session_factory() as session:
        await DocumentService(session).update_document(
            created.id,
            DocumentUpdate(content=EDITED_DOC),  # type: ignore[arg-type]
            tenant_id=DEFAULT_TENANT_ID,
        )

    result = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    assert result.summary == "Second."  # fresh run after the edit
    assert len(prompts) == 2


async def test_null_content_hash_document_is_never_cached(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    prompts: list[str] = []
    cache = FakeCache()
    service = make_summarizer(
        session_factory, scripted_summarize_model(["Once."], prompts=prompts), cache
    )
    async with session_factory() as session:
        await session.execute(
            update(DocumentModel).where(DocumentModel.id == created.id).values(content_hash=None)
        )
        await session.commit()

    first = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    second = await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    assert first.summary == second.summary == "Once."
    assert len(prompts) == 2  # always a miss
    assert cache.set_calls == 0  # never stored


async def test_summarize_error_result_is_never_cached(db_session, session_factory):
    created = await make_document(db_session, SHORT_DOC)
    cache = FakeCache()
    service = make_summarizer(
        session_factory,
        failing_model(),
        cache,
    )
    with pytest.raises(AppError):
        await service.summarize_document(created.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    assert cache.set_calls == 0


async def test_second_association_within_ttl_skips_the_agent(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    prompts: list[str] = []
    cache = FakeCache()
    service = make_associator(
        session_factory,
        scripted_association_model(
            [{"document_id": str(neighbor.id), "reason": "Shared tags."}], prompts=prompts
        ),
        cache,
    )

    first = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    second = await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]

    assert [item.document_id for item in first.associations] == [neighbor.id]
    assert [item.document_id for item in second.associations] == [neighbor.id]
    assert second.associations[0].reason == "Shared tags."  # payload equality
    assert len(prompts) == 1  # agent NOT run on the hit
    assert cache.set_calls == 1


async def test_association_null_content_hash_is_never_cached(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    neighbor = await make_document(db_session, ALPHA_NEIGHBOR)
    prompts: list[str] = []
    cache = FakeCache()
    service = make_associator(
        session_factory,
        scripted_association_model(
            [{"document_id": str(neighbor.id), "reason": "Shared tags."}], prompts=prompts
        ),
        cache,
    )
    async with session_factory() as session:
        await session.execute(
            update(DocumentModel).where(DocumentModel.id == source.id).values(content_hash=None)
        )
        await session.commit()

    await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    assert len(prompts) == 2
    assert cache.set_calls == 0


async def test_association_error_result_is_never_cached(db_session, session_factory):
    source = await make_document(db_session, ALPHA_SOURCE)
    await make_document(db_session, ALPHA_NEIGHBOR)  # candidate source
    cache = FakeCache()
    service = make_associator(
        session_factory,
        failing_model(),
        cache,
    )
    with pytest.raises(AppError):
        await service.associate_document(source.id, tenant_id=DEFAULT_TENANT_ID)  # type: ignore[attr-defined]
    assert cache.set_calls == 0
