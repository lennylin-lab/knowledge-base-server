"""Hybrid retrieval: ES BM25 + pgvector cosine legs fused with Reciprocal
Rank Fusion.

ES returns ranked chunk keys only (source retrieval disabled); PostgreSQL is
the single source of truth for visibility and content — hydration joins live
documents, so soft-deleted (but still-indexed) chunks can never surface.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple
from uuid import UUID

import structlog
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import LLMProviderError, LLMRateLimitedError
from app.llm.embeddings import EmbeddingProvider
from app.repositories.document_chunk import ChunkRow, DocumentChunkRepository
from app.search.es import EsChunkHit, search_chunks
from app.search.queries import bm25_chunk_query

logger = structlog.get_logger(__name__)

# Standard RRF smoothing constant (tuning it belongs to a relevance-evaluation
# task with real data, not this codebase — see design.md tradeoffs).
RRF_K = 60
# Per-leg candidate pool entering fusion; also bounds the hydration IN-list.
CANDIDATE_POOL = 50

SearchMode = Literal["hybrid", "bm25"]


class ChunkKey(NamedTuple):
    """Identity of one indexed chunk: its document plus position."""

    document_id: UUID
    chunk_index: int


@dataclass(frozen=True)
class FusedHit:
    """One chunk after RRF fusion: fused score plus per-leg ranks (1-based)."""

    key: ChunkKey
    score: float
    es_rank: int | None
    vector_rank: int | None


def _rank_map(keys: Sequence[ChunkKey]) -> dict[ChunkKey, int]:
    """First-occurrence 1-based ranks (legs return unique keys; defensive)."""
    ranks: dict[ChunkKey, int] = {}
    for key in keys:
        ranks.setdefault(key, len(ranks) + 1)
    return ranks


def fuse_rrf(
    es_keys: Sequence[ChunkKey],
    vector_keys: Sequence[ChunkKey],
    *,
    k: int = RRF_K,
) -> list[FusedHit]:
    """Reciprocal Rank Fusion: `score = Σ_legs 1 / (k + rank_leg)`.

    Pure and total — empty legs are valid (single-leg or empty fusion).
    Ordering is deterministic: (score desc, es_rank asc, vector_rank asc,
    key); a rank the leg did not award sorts as infinitely bad.
    """
    es_ranks = _rank_map(es_keys)
    vector_ranks = _rank_map(vector_keys)

    hits: list[FusedHit] = []
    for key in es_ranks.keys() | vector_ranks.keys():
        es_rank = es_ranks.get(key)
        vector_rank = vector_ranks.get(key)
        score = sum(1.0 / (k + rank) for rank in (es_rank, vector_rank) if rank is not None)
        hits.append(FusedHit(key=key, score=score, es_rank=es_rank, vector_rank=vector_rank))

    def sort_key(hit: FusedHit) -> tuple[float, float, float, ChunkKey]:
        return (
            -hit.score,
            hit.es_rank if hit.es_rank is not None else math.inf,
            hit.vector_rank if hit.vector_rank is not None else math.inf,
            hit.key,
        )

    return sorted(hits, key=sort_key)


@dataclass(frozen=True)
class RetrievedChunk:
    """A fused hit hydrated with chunk text and live-document metadata."""

    key: ChunkKey
    score: float
    es_rank: int | None
    vector_rank: int | None
    content: str
    document_title: str
    document_tags: list[str]


@dataclass(frozen=True)
class SearchOutcome:
    """Retrieval result before schema mapping."""

    mode: SearchMode
    items: list[RetrievedChunk]
    es_hits: int
    vector_hits: int


@dataclass(frozen=True)
class _VectorLeg:
    """Outcome of the pgvector leg: rows plus whether it actually ran."""

    rows: list[ChunkRow]
    ran: bool


class Retriever:
    """Hybrid retrieval over one ES index and one PG database."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        es_client: AsyncElasticsearch,
        embedding_provider: EmbeddingProvider | None,
        es_index: str,
    ) -> None:
        self._session_factory = session_factory
        self._es_client = es_client
        self._embedding_provider = embedding_provider
        self._es_index = es_index

    async def retrieve(
        self, query: str, *, limit: int = 10, tag: str | None = None
    ) -> SearchOutcome:
        """Run both legs concurrently, fuse with RRF, hydrate the top hits.

        The tag filter applies to BOTH legs so fused ranks are tag-consistent.
        `mode` is `"hybrid"` when the vector leg ran, `"bm25"` when it could
        not (no provider configured, or the embed call failed mid-search).
        """
        body = bm25_chunk_query(query, size=CANDIDATE_POOL, tag=tag)
        es_result, vector_result = await asyncio.gather(
            search_chunks(self._es_client, index=self._es_index, body=body),
            self._vector_leg(query, tag=tag),
            return_exceptions=True,
        )
        # ES errors take precedence: a broken index is a 502 even if the
        # vector leg failed too. The vector leg degrades provider errors
        # internally, so anything surfacing here is a hard dependency failure.
        if isinstance(es_result, BaseException):
            raise es_result
        if isinstance(vector_result, BaseException):
            raise vector_result
        es_hits: list[EsChunkHit] = es_result
        vector_leg: _VectorLeg = vector_result

        es_keys = [ChunkKey(hit.document_id, hit.chunk_index) for hit in es_hits]
        vector_keys = [ChunkKey(row.document_id, row.chunk_index) for row in vector_leg.rows]
        top = fuse_rrf(es_keys, vector_keys)[:limit]

        hydrated: dict[tuple[UUID, int], ChunkRow] = {
            (row.document_id, row.chunk_index): row for row in vector_leg.rows
        }
        missing = [hit.key for hit in top if hit.key not in hydrated]
        if missing:
            async with self._session_factory() as session:
                hydrated.update(await DocumentChunkRepository(session).get_live_chunks(missing))

        items: list[RetrievedChunk] = []
        for hit in top:
            row = hydrated.get(hit.key)
            if row is None:
                # Soft-deleted (or never-indexed) since a leg saw it: dropped.
                continue
            items.append(
                RetrievedChunk(
                    key=hit.key,
                    score=hit.score,
                    es_rank=hit.es_rank,
                    vector_rank=hit.vector_rank,
                    content=row.content,
                    document_title=row.document_title,
                    document_tags=row.document_tags,
                )
            )
        mode: SearchMode = "hybrid" if vector_leg.ran else "bm25"
        return SearchOutcome(
            mode=mode,
            items=items,
            es_hits=len(es_hits),
            vector_hits=len(vector_leg.rows),
        )

    async def _vector_leg(self, query: str, *, tag: str | None) -> _VectorLeg:
        """Embed the query and search pgvector, hydrated against live documents.

        `ran=False` marks BM25-only operation: no provider configured, or the
        embed call failed (warn + degrade — search must stay available, never
        5xx over a missing vector leg). A PG failure after a successful embed
        propagates: both legs depend on PG for hydration, so that is a broken
        dependency, not a degradation.
        """
        if self._embedding_provider is None:
            return _VectorLeg(rows=[], ran=False)
        try:
            vectors = await self._embedding_provider.embed_texts([query])
        except (LLMProviderError, LLMRateLimitedError) as exc:
            # Error class only — provider detail stays out of logs.
            logger.warning("vector_search_degraded", error_class=type(exc).__name__)
            return _VectorLeg(rows=[], ran=False)
        async with self._session_factory() as session:
            rows = await DocumentChunkRepository(session).search_similar(
                vectors[0], limit=CANDIDATE_POOL, tag=tag
            )
        return _VectorLeg(rows=list(rows), ran=True)
