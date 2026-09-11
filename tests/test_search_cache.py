"""Search result cache: epoch-keyed outcome caching at the retriever choke
point, epoch invalidation via the document-write seam, and the outcome JSON
round trip (fully offline — legs are stubbed, cache is a fake)."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

import app.rag.retriever as retriever_module
from app.core.cache import SEARCH_EPOCH_KEY
from app.rag.retriever import Retriever, SearchOutcome, _VectorLeg
from app.repositories.document_chunk import ChunkRow
from app.search.es import EsChunkHit
from fakes import GATES_OFF, FakeCache


class StubRetrieverWorld:
    """Counts leg invocations by stubbing `search_chunks` and `_vector_leg`."""

    def __init__(self) -> None:
        from uuid import uuid4

        self.document_id = uuid4()
        self.chunk_index = 0
        self.search_calls = 0
        self.vector_calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        doc_id, chunk_idx = self.document_id, self.chunk_index

        async def fake_search_chunks(client: object, *, index: str, body: dict) -> list[EsChunkHit]:
            self.search_calls += 1
            return [EsChunkHit(document_id=doc_id, chunk_index=chunk_idx, score=5.0)]

        async def fake_vector_leg(
            self_retriever: Retriever, query: str, *, tag: str | None
        ) -> _VectorLeg:
            self.vector_calls += 1
            row = ChunkRow(
                document_id=doc_id,
                chunk_index=chunk_idx,
                content="chunk body",
                document_title="Title",
                document_tags=["t"],
                distance=0.1,
            )
            return _VectorLeg(rows=[row], ran=True)

        monkeypatch.setattr(retriever_module, "search_chunks", fake_search_chunks)
        monkeypatch.setattr(Retriever, "_vector_leg", fake_vector_leg)

    def make_retriever(self, cache: FakeCache | None) -> Retriever:
        return Retriever(
            session_factory=None,  # type: ignore[arg-type]  # never reached: hydration is covered by the stubbed vector rows
            es_client=None,  # type: ignore[arg-type]
            embedding_provider=None,
            es_index="idx",
            cache=cache,
            cache_ttl_seconds=60,
            **GATES_OFF,
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> StubRetrieverWorld:
    w = StubRetrieverWorld()
    w.install(monkeypatch)
    return w


async def test_identical_query_within_epoch_hits_cache_without_legs(world):
    cache = FakeCache()
    retriever = world.make_retriever(cache)

    first = await retriever.retrieve("some query")
    second = await retriever.retrieve("some query")

    assert world.search_calls == 1 and world.vector_calls == 1  # legs NOT re-run
    assert second == first
    assert [c.key for c in second.items] == [(world.document_id, 0)]
    assert second.items[0].es_score == 5.0 and second.items[0].vector_distance == 0.1


async def test_epoch_bump_forces_a_recompute(world):
    cache = FakeCache()
    retriever = world.make_retriever(cache)
    await retriever.retrieve("q")

    await cache.incr(SEARCH_EPOCH_KEY)  # what DocumentService does after commit
    await retriever.retrieve("q")

    assert world.search_calls == 2 and world.vector_calls == 2


async def test_limit_and_tag_variants_key_independently(world):
    cache = FakeCache()
    retriever = world.make_retriever(cache)
    await retriever.retrieve("q", limit=5)
    await retriever.retrieve("q", limit=10)
    await retriever.retrieve("q", limit=10, tag="x")

    assert world.search_calls == 3  # each variant computed
    assert len(cache.store) == 3  # three distinct outcome keys (epoch is read, not written)


async def test_outcome_json_round_trip_field_exact(world):
    cache = FakeCache()
    retriever = world.make_retriever(cache)
    outcome = await retriever.retrieve("q")

    restored = SearchOutcome.from_json(outcome.to_json())
    assert restored.mode == outcome.mode
    assert restored.es_hits == outcome.es_hits
    assert restored.vector_hits == outcome.vector_hits
    assert restored.es_gated == outcome.es_gated
    assert restored.vector_gated == outcome.vector_gated
    assert restored.fused_gated == outcome.fused_gated
    assert restored.vector_rescued == outcome.vector_rescued
    a, b = restored.items[0], outcome.items[0]
    assert (a.key, a.score, a.es_rank, a.vector_rank) == (b.key, b.score, b.es_rank, b.vector_rank)
    assert a.content == b.content
    assert a.document_title == b.document_title
    assert a.document_tags == b.document_tags
    assert a.es_score == b.es_score
    assert a.vector_distance == b.vector_distance


async def test_hit_and_miss_events_are_logged_without_query_text(world):
    cache = FakeCache()
    retriever = world.make_retriever(cache)
    with capture_logs() as logs:
        await retriever.retrieve("sensitive-query-text")
        await retriever.retrieve("sensitive-query-text")
    assert any(e["event"] == "cache_miss" and e["domain"] == "search" for e in logs)
    assert any(e["event"] == "cache_hit" and e["domain"] == "search" for e in logs)
    # Query text never appears in the log stream (only q_length at the service).
    assert all("sensitive-query-text" not in str(e) for e in logs)


async def test_cache_none_wiring_keeps_compute_path(world):
    retriever = world.make_retriever(None)
    await retriever.retrieve("q")
    await retriever.retrieve("q")
    assert world.search_calls == 2  # no caching at all — pre-cache behavior
