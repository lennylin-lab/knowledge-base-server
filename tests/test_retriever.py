"""Retriever integration: both legs, fusion, relevance gates, visibility,
degradation.

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
from app.rag.retriever import (
    DEFAULT_BM25_MIN_SCORE,
    DEFAULT_MAX_QUERY_LENGTH,
    DEFAULT_VECTOR_MAX_DISTANCE,
    Retriever,
)
from app.services.document import DocumentService
from corpus import (
    E0,
    E1,
    KOTLIN_SECTION,
    PYTHON_SECTION,
    RESCUE_PROOF_QUERY,
    SHIFTED_DISTANCE,
    SHIFTED_QUERY,
    VECTOR_QUERY,
    distant_scripted_provider,
    neighbor_scripted_provider,
    seed_corpus,
    shifted_scripted_provider,
)
from fakes import GATES_OFF, ScriptedEmbeddingProvider

pytestmark = [pytest.mark.db, pytest.mark.es]


def make_retriever(
    session_factory: async_sessionmaker[AsyncSession],
    es_client: AsyncElasticsearch,
    es_index: str,
    provider: EmbeddingProvider | None,
    **thresholds: float,
) -> Retriever:
    """A retriever on the test infra; thresholds default to the gated constructor."""
    return Retriever(
        session_factory=session_factory,
        es_client=es_client,
        embedding_provider=provider,
        es_index=es_index,
        **thresholds,
    )


async def test_bm25_leg_ranks_distinctive_term_first_without_provider(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, provider=None, **GATES_OFF
    )

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
        session_factory, es_client, es_index_name, neighbor_scripted_provider(), **GATES_OFF
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
    retriever = make_retriever(session_factory, es_client, es_index_name, provider, **GATES_OFF)

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
    bm25 = make_retriever(session_factory, es_client, es_index_name, provider=None, **GATES_OFF)
    outcome = await bm25.retrieve("zorblat")
    assert outcome.es_hits == 1  # ES is stale — by design (PG is the filter)
    assert outcome.items == []

    # Vector leg: the live-document join excludes it before ranking.
    hybrid = make_retriever(
        session_factory, es_client, es_index_name, neighbor_scripted_provider(), **GATES_OFF
    )
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
    retriever = make_retriever(session_factory, es_client, es_index_name, provider, **GATES_OFF)

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
    retriever = make_retriever(session_factory, es_client, es_index_name, provider, **GATES_OFF)

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
    retriever = make_retriever(session_factory, es_client, es_index_name, provider, **GATES_OFF)
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


# --- relevance gates (constructor defaults = Settings defaults) ---


async def test_unrelated_vector_query_returns_zero_items_with_default_gates(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, distant_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, distant_scripted_provider()
    )

    outcome = await retriever.retrieve(VECTOR_QUERY)  # default limit=10

    assert outcome.mode == "hybrid"
    assert outcome.es_hits == 0  # no term overlap: BM25 leg empty on its own
    assert outcome.vector_hits == 2  # pgvector still returns its KNN candidates
    assert outcome.vector_gated == 2  # ...all beyond the cosine-distance ceiling
    assert outcome.items == []  # empty beats noise, despite limit=10


async def test_distinctive_bm25_query_passes_gate_and_carries_es_score(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, distant_scripted_provider())
    retriever = make_retriever(session_factory, es_client, es_index_name, provider=None)

    outcome = await retriever.retrieve("zorblat")

    assert outcome.mode == "bm25"
    assert outcome.es_hits == 1
    assert outcome.es_gated == 0
    assert len(outcome.items) == 1
    top = outcome.items[0]
    assert top.document_title == "Kotlin Notes"
    assert top.es_score is not None
    assert top.es_score >= DEFAULT_BM25_MIN_SCORE
    assert top.vector_distance is None


async def test_vector_neighbor_passes_distance_gate_and_carries_distance(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, neighbor_scripted_provider()
    )

    outcome = await retriever.retrieve(VECTOR_QUERY)

    assert outcome.mode == "hybrid"
    assert outcome.vector_hits == 2
    assert outcome.vector_gated == 1  # Python Notes sits at distance 1.0
    assert [item.document_title for item in outcome.items] == ["Kotlin Notes"]
    assert outcome.items[0].vector_distance is not None
    assert outcome.items[0].vector_distance <= DEFAULT_VECTOR_MAX_DISTANCE
    assert outcome.items[0].es_score is None  # the BM25 leg returned nothing


async def test_gates_apply_after_tag_filter_narrowing(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, neighbor_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, neighbor_scripted_provider()
    )

    # The tag filter keeps the Python row alive; its distance (1.0) then
    # fails the gate — the two filters compose, neither masks the other.
    outcome = await retriever.retrieve(VECTOR_QUERY, tag="python")

    assert outcome.vector_hits == 1
    assert outcome.vector_gated == 1
    assert outcome.items == []


async def test_gates_and_visibility_compose_to_empty(
    seed_indexed, session_factory, es_client, es_index_name
):
    kotlin_id, _ = await seed_corpus(seed_indexed, neighbor_scripted_provider())
    async with session_factory() as session:
        await DocumentService(session).delete_document(kotlin_id)
    retriever = make_retriever(
        session_factory, es_client, es_index_name, neighbor_scripted_provider()
    )

    # The only live chunk sits at distance 1.0: soft-delete + gate => nothing.
    outcome = await retriever.retrieve(VECTOR_QUERY)

    assert outcome.items == []


# --- head-rescue gate (short-query granularity shift) ---


async def test_rescued_vector_head_restores_short_query_recall(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, shifted_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, shifted_scripted_provider()
    )

    outcome = await retriever.retrieve(SHIFTED_QUERY)

    assert outcome.mode == "hybrid"
    assert outcome.es_hits == 0  # no term overlap: the vector leg decides alone
    assert outcome.vector_hits == 2
    assert outcome.vector_gated == 0  # the rescue tier admitted the shifted head
    assert outcome.vector_rescued == 2
    assert {item.document_title for item in outcome.items} == {
        "Kotlin Notes",
        "Python Notes",
    }
    # Admitted by rescue, NOT by the primary ceiling: every distance sits
    # above the ceiling yet inside the rescue window (float32 pgvector
    # storage rounds the scripted distance — compare approximately).
    distances = [item.vector_distance for item in outcome.items]
    assert all(distance == pytest.approx(SHIFTED_DISTANCE, abs=1e-3) for distance in distances)
    assert all(distance > DEFAULT_VECTOR_MAX_DISTANCE for distance in distances)


async def test_rescue_cap_keeps_rare_term_query_es_dominated(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, shifted_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, shifted_scripted_provider()
    )

    outcome = await retriever.retrieve(RESCUE_PROOF_QUERY)

    assert outcome.mode == "hybrid"
    assert outcome.vector_gated == 2  # head at 0.9: beyond the rescue cap
    assert outcome.vector_rescued == 0
    assert {item.document_title for item in outcome.items} == {
        "Kotlin Notes",
        "Python Notes",
    }
    assert all(item.es_score is not None for item in outcome.items)  # ES decided alone


async def test_unrelated_query_stays_empty_in_rescue_world(
    seed_indexed, session_factory, es_client, es_index_name
):
    await seed_corpus(seed_indexed, shifted_scripted_provider())
    retriever = make_retriever(
        session_factory, es_client, es_index_name, shifted_scripted_provider()
    )

    outcome = await retriever.retrieve(VECTOR_QUERY)  # orthogonal: distance 1.0

    assert outcome.vector_rescued == 0
    assert outcome.items == []  # the rescue tier is not a noise leak


async def test_over_long_query_reaches_legs_truncated(
    seed_indexed, session_factory, es_client, es_index_name
):
    provider = neighbor_scripted_provider()
    await seed_corpus(seed_indexed, provider)
    retriever = make_retriever(session_factory, es_client, es_index_name, provider, **GATES_OFF)

    await retriever.retrieve("x" * 300)

    # The search-time embed call (the last one, after indexing's) carried the
    # prefix only — the legs never saw the over-long query.
    assert provider.calls[-1] == ["x" * DEFAULT_MAX_QUERY_LENGTH]
