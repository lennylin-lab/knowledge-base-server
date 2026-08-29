"""Retriever integration: both legs, fusion, visibility, degradation.

The corpus is seeded through the real IndexingPipeline (service write ->
chunk -> embed -> PG + ES) with a scripted provider, so the vector leg's
neighbors are known and the BM25 corpus is small and distinctive. Requires
db + es (auto-skipped when either is unreachable).
"""

from __future__ import annotations

import pytest
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.core.exceptions import LLMProviderError, SearchIndexError
from app.llm.embeddings import EmbeddingProvider
from app.rag.retriever import Retriever
from app.services.document import DocumentService
from corpus import (
    E0,
    E1,
    KOTLIN_SECTION,
    PYTHON_SECTION,
    VECTOR_QUERY,
    neighbor_scripted_provider,
    seed_corpus,
)
from fakes import ScriptedEmbeddingProvider

pytestmark = [pytest.mark.db, pytest.mark.es]


def make_retriever(
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index: str,
    provider: EmbeddingProvider | None,
) -> Retriever:
    return Retriever(
        session_factory=session_factory,
        es_client=es_client,
        embedding_provider=provider,
        es_index=es_index,
    )


async def test_bm25_leg_ranks_distinctive_term_first_without_provider(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = make_retriever(session_factory, es_client, es_index_name, provider=None)

    outcome = await retriever.retrieve("zorblat")

    assert outcome.mode == "bm25"
    assert outcome.es_hits == 1
    assert outcome.vector_hits == 0
    assert len(outcome.items) == 1
    top = outcome.items[0]
    assert top.document_title == "Kotlin Notes"
    assert top.document_tags == ["kotlin"]
    assert top.content == KOTLIN_SECTION
    assert top.key.chunk_index == 0
    assert top.es_rank == 1
    assert top.vector_rank is None


async def test_vector_leg_top_ranks_scripted_neighbor(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, neighbor_scripted_provider()
    )

    outcome = await retriever.retrieve(VECTOR_QUERY)

    assert outcome.mode == "hybrid"
    assert outcome.es_hits == 0  # BM25-empty query: vector leg decides alone
    assert outcome.vector_hits == 2
    assert outcome.items[0].document_title == "Kotlin Notes"
    assert outcome.items[0].es_rank is None
    assert outcome.items[0].vector_rank == 1
    assert {item.document_title for item in outcome.items} == {
        "Kotlin Notes",
        "Python Notes",
    }


async def test_fused_result_carries_both_leg_ranks(
    seed_indexed, session_factory, es_client, es_index_name
):
    provider = neighbor_scripted_provider()
    provider.vectors["zorblat"] = E0  # the BM25 query rides the vector leg too
    await seed_corpus(seed_indexed, provider)
    retriever = make_retriever(session_factory, es_client, es_index_name, provider)

    outcome = await retriever.retrieve("zorblat")

    assert outcome.mode == "hybrid"
    assert outcome.es_hits == 1  # only the Kotlin document contains "zorblat"
    by_title = {item.document_title: item for item in outcome.items}
    assert by_title["Kotlin Notes"].es_rank == 1
    assert by_title["Kotlin Notes"].vector_rank == 1
    assert outcome.items[0].document_title == "Kotlin Notes"
    assert by_title["Python Notes"].es_rank is None
    assert by_title["Python Notes"].vector_rank == 2


async def test_soft_deleted_document_chunks_never_surface_on_either_leg(
    seed_indexed, session_factory, es_client, es_index_name
):
    kotlin_id, _ = await seed_corpus(seed_indexed, neighbor_scripted_provider())
    async with session_factory() as session:
        await DocumentService(session).delete_document(kotlin_id)

    # BM25 leg: ES still returns the stale doc; PG hydration must drop it.
    bm25 = make_retriever(session_factory, es_client, es_index_name, provider=None)
    outcome = await bm25.retrieve("zorblat")
    assert outcome.es_hits == 1  # ES is stale — by design (PG is the filter)
    assert outcome.items == []

    # Vector leg: the live-document join excludes it before ranking.
    hybrid = make_retriever(session_factory, es_client, es_index_name, neighbor_scripted_provider())
    vector_outcome = await hybrid.retrieve(VECTOR_QUERY)
    assert all(item.document_title != "Kotlin Notes" for item in vector_outcome.items)
    assert [item.document_title for item in vector_outcome.items] == ["Python Notes"]

    # The sibling stays fully visible.
    sibling = await bm25.retrieve("quibnard")
    assert [item.document_title for item in sibling.items] == ["Python Notes"]


async def test_tag_filter_narrows_both_legs(
    seed_indexed, session_factory, es_client, es_index_name
):
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_SECTION] = E0
    provider.vectors[PYTHON_SECTION] = E0
    provider.vectors["notes"] = E0  # matches both chunks' embeddings
    await seed_corpus(seed_indexed, provider)
    retriever = make_retriever(session_factory, es_client, es_index_name, provider)

    unfiltered = await retriever.retrieve("notes")
    assert {item.document_title for item in unfiltered.items} == {
        "Kotlin Notes",
        "Python Notes",
    }

    filtered = await retriever.retrieve("notes", tag="kotlin")
    assert filtered.es_hits == 1
    assert filtered.vector_hits == 1
    assert [item.document_title for item in filtered.items] == ["Kotlin Notes"]
    assert filtered.items[0].document_tags == ["kotlin"]


async def test_limit_trims_fused_results(seed_indexed, session_factory, es_client, es_index_name):
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_SECTION] = E0
    provider.vectors[PYTHON_SECTION] = E0
    provider.vectors["notes"] = E0
    await seed_corpus(seed_indexed, provider)
    retriever = make_retriever(session_factory, es_client, es_index_name, provider)

    outcome = await retriever.retrieve("notes", limit=1)

    assert len(outcome.items) == 1


async def test_es_failure_raises_search_index_error(session_factory, es_client, es_index_name):
    retriever = make_retriever(
        session_factory, es_client, f"{es_index_name}-never-created", provider=None
    )

    with pytest.raises(SearchIndexError) as exc_info:
        await retriever.retrieve("anything")

    assert exc_info.value.details["operation"] == "search_chunks"


async def test_provider_failure_mid_search_degrades_to_bm25_with_warning(
    seed_indexed, session_factory, es_client, es_index_name
):
    provider = neighbor_scripted_provider()
    await seed_corpus(seed_indexed, provider)
    retriever = make_retriever(session_factory, es_client, es_index_name, provider)
    provider.error = LLMProviderError("provider down with secret detail")

    with capture_logs() as logs:
        outcome = await retriever.retrieve("zorblat")

    assert outcome.mode == "bm25"
    assert outcome.vector_hits == 0
    assert [item.document_title for item in outcome.items] == ["Kotlin Notes"]
    degraded = [entry for entry in logs if entry["event"] == "vector_search_degraded"]
    assert len(degraded) == 1
    assert degraded[0]["log_level"] == "warning"
    assert degraded[0]["error_class"] == "LLMProviderError"
    assert "secret detail" not in str(logs)  # provider detail stays out of logs
