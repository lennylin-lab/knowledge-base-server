"""Hybrid retrieval: ES BM25 + pgvector cosine legs fused with Reciprocal
Rank Fusion.

ES returns ranked chunk keys only (source retrieval disabled); PostgreSQL is
the single source of truth for visibility and content — hydration joins live
documents, so soft-deleted (but still-indexed) chunks can never surface.

Relevance gates keep weak matches from padding the results: the BM25 leg's
noise guard is term coverage (an ES `minimum_should_match` on the prose
field, threaded from Settings through the query builder; the absolute
`_score` floor is a disabled-by-default operator escape hatch — see
`DEFAULT_BM25_MIN_SCORE` below), and the vector leg drops hits beyond a
cosine-distance ceiling. The fused ranking applies a relative floor
against the top hit — empty results beat noise on small corpora. When the
vector ceiling empties its leg, a rescue tier admits that leg's clustered
head (short keyword queries sit systematically farther from long chunks, so
the absolute ceiling alone would silence semantic recall exactly when it is
needed) — but only as a backstop for lexical failure: the gated BM25 leg
must have no surviving hits (when BM25 already answered, the semantic
backstop would only inject the same-domain cluster), and the leg must be
plausibly on-domain: a leg whose closest hit is already beyond the on-domain
trigger stays empty (empty beats noise), and the rescue cap remains the
absolute backstop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple
from uuid import UUID

import structlog
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import SEARCH_EPOCH_KEY, Cache, cache_key
from app.core.exceptions import LLMProviderError, LLMRateLimitedError
from app.llm.embeddings import EmbeddingProvider
from app.repositories.document_chunk import ChunkRow, DocumentChunkRepository
from app.search.es import EsChunkHit, search_chunks
from app.search.queries import DEFAULT_BM25_MIN_COVERAGE, bm25_chunk_query

logger = structlog.get_logger(__name__)

# Standard RRF smoothing constant (tuning it belongs to a relevance-evaluation
# task with real data, not this codebase — see design.md tradeoffs).
RRF_K = 60
# Per-leg candidate pool entering fusion; also bounds the hydration IN-list.
CANDIDATE_POOL = 50

# Gate defaults, mirroring the Settings fields in core/config.py (a drift-guard
# unit test keeps the two in sync). Wiring always injects the configured
# values (api/deps.py); these cover direct construction (tests, tooling).
# The absolute BM25 score floor is RETIRED as a live gate (default 0.0): BM25
# score scales are query-dependent (measured 2026-09-08, task
# 09-08-es-bm25-scoring D5: top-hit scores spanned 4.46-28.39 across probe
# queries with min/top ratios 0.045-0.586 within result sets), so no absolute
# floor separates weak-but-real hits from noise. The live BM25 noise gate is
# the scale-free term coverage (`bm25_min_coverage`, an ES
# `minimum_should_match` on the prose leaf — see search/queries.py).
DEFAULT_BM25_MIN_SCORE = 0.0
DEFAULT_VECTOR_MAX_DISTANCE = 0.45
DEFAULT_VECTOR_RESCUE_MARGIN = 0.15
DEFAULT_VECTOR_RESCUE_MAX_DISTANCE = 0.85
# On-domain rescue trigger: rescue fires only when the leg's minimum distance
# is at or below this (a plausibly on-domain leg); a noisier leg stays empty.
# Calibrated 2026-09-10 on the real 15-doc corpus (in-domain short-keyword
# leg_min 0.473-0.652, off-domain queries 0.656-0.774; precision-first policy
# — see task 09-10-irrelevant-query-noise-gates design.md). `>= 2.0` disables
# the trigger: rescue then fires whenever the primary tier is empty.
DEFAULT_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE = 0.62
DEFAULT_RRF_MIN_RELATIVE = 0.35
DEFAULT_MAX_QUERY_LENGTH = 256

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


def filter_es_hits(hits: Sequence[EsChunkHit], *, min_score: float) -> list[EsChunkHit]:
    """BM25 gate: drop hits whose ES `_score` is below the absolute floor.

    `min_score <= 0` disables the gate (every hit passes).
    """
    if min_score <= 0:
        return list(hits)
    return [hit for hit in hits if hit.score >= min_score]


def filter_vector_rows(rows: Sequence[ChunkRow], *, max_distance: float) -> list[ChunkRow]:
    """Vector gate: drop rows whose cosine distance exceeds the ceiling.

    `max_distance >= 2.0` — cosine distance's maximum — disables the gate.
    Rows without a measured distance pass: the gate judges distance, and
    `search_similar` always measures one for the vector leg.
    """
    if max_distance >= 2.0:
        return list(rows)
    return [row for row in rows if row.distance is None or row.distance <= max_distance]


def filter_vector_rows_with_rescue(
    rows: Sequence[ChunkRow],
    *,
    max_distance: float,
    rescue_margin: float,
    rescue_max_distance: float,
    rescue_trigger_max_distance: float,
    bm25_leg_empty: bool,
) -> tuple[list[ChunkRow], int]:
    """Two-tier vector gate: the absolute ceiling plus a head-rescue tier.

    The primary tier is `filter_vector_rows` exactly. Only when it empties
    the leg (and rows exist) does the rescue tier admit rows within
    `min(leg_min + rescue_margin, rescue_max_distance)` — the clustered head
    of a leg shifted up wholesale — so short keyword queries keep semantic
    recall without loosening the primary ceiling for everyone. Rescue is the
    lexical-failure backstop: `bm25_leg_empty` must hold (the gated BM25 leg
    produced no surviving hits — a vocabulary mismatch the exact-term leg
    could not answer); when BM25 already has matches, the emptied vector leg
    stays empty instead of injecting the same-domain cluster. The on-domain
    trigger gates WHETHER rescue fires at all: a leg whose minimum distance
    exceeds `rescue_trigger_max_distance` is not plausibly on-domain, so it
    stays empty instead of rescuing its noise band.
    `rescue_trigger_max_distance >= 2.0` — cosine distance's maximum —
    disables the trigger (rescue fires whenever the primary tier empties the
    leg). The rescue WINDOW logic is unchanged: `rescue_margin <= 0` or
    `rescue_max_distance <= 0` still disables rescue (behavior identical to
    the single-tier gate), and the cap remains the absolute backstop for the
    window. Returns the kept rows and how many were admitted ONLY via the
    rescue tier (0 whenever the primary tier has survivors or the BM25 leg
    is non-empty).
    """
    if max_distance >= 2.0:
        return list(rows), 0
    primary = [row for row in rows if row.distance is None or row.distance <= max_distance]
    if primary or not rows:
        return primary, 0
    if not bm25_leg_empty:
        # BM25 already answered lexically: the backstop exists for vocabulary
        # mismatch, so the emptied vector leg stays empty rather than
        # admitting the broad same-domain cluster.
        return [], 0
    if rescue_margin <= 0 or rescue_max_distance <= 0:
        return [], 0
    # Every remaining row carries a measured distance: a None-distance row
    # would have passed the primary tier fail-open and blocked this branch.
    measured = [row.distance for row in rows if row.distance is not None]
    if not measured:  # defensive; unreachable given the fail-open primary tier
        return [], 0
    measured_min = min(measured)
    if measured_min > rescue_trigger_max_distance:
        # Off-domain leg: not even the closest hit is plausibly on-domain, so
        # the rescue window would admit pure noise. Empty beats noise.
        return [], 0
    window = min(measured_min + rescue_margin, rescue_max_distance)
    rescued = [row for row in rows if row.distance is not None and row.distance <= window]
    return rescued, len(rescued)


def truncate_query(query: str, *, max_length: int) -> str:
    """Query length cap: the retriever's single enforcement point.

    `max_length <= 0` disables the cap. Truncation — not rejection — is the
    only behavior that can serve both the API and the agent tools, and the
    bound keeps analyzed CJK queries far under Lucene's clause limit and
    embedding providers' token limits. Callers logging `q_length` keep
    seeing the raw caller-provided length; truncation is retriever-internal.
    """
    if 0 < max_length < len(query):
        return query[:max_length]
    return query


def apply_relative_score_floor(hits: Sequence[FusedHit], *, min_relative: float) -> list[FusedHit]:
    """Relative gate: keep hits scoring at least `min_relative` of the top hit.

    `hits` must be RRF-ordered (score desc, as `fuse_rrf` returns); the top
    hit always survives, so a lone result is never dropped. `min_relative <=
    0` disables the gate; a non-positive top score keeps nothing — there is
    no meaningful fraction of it.
    """
    if not hits or min_relative <= 0:
        return list(hits)
    top = hits[0].score
    if top <= 0:
        return []
    cutoff = top * min_relative
    return [hit for hit in hits if hit.score >= cutoff]


@dataclass(frozen=True)
class RetrievedChunk:
    """A fused hit hydrated with chunk text and live-document metadata.

    `es_score`/`vector_distance` carry the raw per-leg signal when that leg
    ranked the chunk (`None` otherwise) so callers and tests can audit the
    relevance gates; `score` stays the RRF fused score.
    """

    key: ChunkKey
    score: float
    es_rank: int | None
    vector_rank: int | None
    content: str
    document_title: str
    document_tags: list[str]
    es_score: float | None = None
    vector_distance: float | None = None


@dataclass(frozen=True)
class SearchOutcome:
    """Retrieval result before schema mapping.

    `es_hits`/`vector_hits` are the raw leg sizes before their gates; the
    `*_gated` counters record how many candidates each gate dropped, and
    `vector_rescued` how many rows the rescue tier admitted — the audit
    trail for the `search_executed` event (which never carries query text).
    """

    mode: SearchMode
    items: list[RetrievedChunk]
    es_hits: int
    vector_hits: int
    es_gated: int = 0
    vector_gated: int = 0
    fused_gated: int = 0
    vector_rescued: int = 0

    def to_json(self) -> bytes:
        """Explicit JSON serialization for the result cache: frozen
        dataclasses with UUIDs/enums never pickle into the cache — the
        field set is pinned by a round-trip test."""
        return json.dumps(
            {
                "mode": self.mode,
                "items": [
                    {
                        "document_id": str(item.key.document_id),
                        "chunk_index": item.key.chunk_index,
                        "score": item.score,
                        "es_rank": item.es_rank,
                        "vector_rank": item.vector_rank,
                        "content": item.content,
                        "document_title": item.document_title,
                        "document_tags": item.document_tags,
                        "es_score": item.es_score,
                        "vector_distance": item.vector_distance,
                    }
                    for item in self.items
                ],
                "es_hits": self.es_hits,
                "vector_hits": self.vector_hits,
                "es_gated": self.es_gated,
                "vector_gated": self.vector_gated,
                "fused_gated": self.fused_gated,
                "vector_rescued": self.vector_rescued,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> SearchOutcome:
        """Inverse of `to_json`; raises ValueError on an unusable payload
        (the caller recomputes)."""
        payload = json.loads(raw)
        return cls(
            mode=payload["mode"],
            items=[
                RetrievedChunk(
                    key=ChunkKey(UUID(item["document_id"]), item["chunk_index"]),
                    score=item["score"],
                    es_rank=item["es_rank"],
                    vector_rank=item["vector_rank"],
                    content=item["content"],
                    document_title=item["document_title"],
                    document_tags=item["document_tags"],
                    es_score=item["es_score"],
                    vector_distance=item["vector_distance"],
                )
                for item in payload["items"]
            ],
            es_hits=payload["es_hits"],
            vector_hits=payload["vector_hits"],
            es_gated=payload["es_gated"],
            vector_gated=payload["vector_gated"],
            fused_gated=payload["fused_gated"],
            vector_rescued=payload["vector_rescued"],
        )


@dataclass(frozen=True)
class _VectorLeg:
    """Outcome of the pgvector leg: rows plus whether it actually ran."""

    rows: list[ChunkRow]
    ran: bool


class Retriever:
    """Hybrid retrieval over one ES index and one PG database.

    The relevance thresholds arrive constructor-injected (Settings values via
    `api/deps.py`); defaults cover direct construction and mirror the
    Settings defaults.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        es_client: AsyncElasticsearch,
        embedding_provider: EmbeddingProvider | None,
        es_index: str,
        bm25_min_score: float = DEFAULT_BM25_MIN_SCORE,
        bm25_min_coverage: str = DEFAULT_BM25_MIN_COVERAGE,
        vector_max_distance: float = DEFAULT_VECTOR_MAX_DISTANCE,
        vector_rescue_margin: float = DEFAULT_VECTOR_RESCUE_MARGIN,
        vector_rescue_max_distance: float = DEFAULT_VECTOR_RESCUE_MAX_DISTANCE,
        vector_rescue_trigger_max_distance: float = DEFAULT_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE,
        rrf_min_relative: float = DEFAULT_RRF_MIN_RELATIVE,
        max_query_length: int = DEFAULT_MAX_QUERY_LENGTH,
        cache: Cache | None = None,
        cache_ttl_seconds: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._es_client = es_client
        self._embedding_provider = embedding_provider
        self._es_index = es_index
        self._bm25_min_score = bm25_min_score
        self._bm25_min_coverage = bm25_min_coverage
        self._vector_max_distance = vector_max_distance
        self._vector_rescue_margin = vector_rescue_margin
        self._vector_rescue_max_distance = vector_rescue_max_distance
        self._vector_rescue_trigger_max_distance = vector_rescue_trigger_max_distance
        self._rrf_min_relative = rrf_min_relative
        self._max_query_length = max_query_length
        # Optional best-effort result cache (None = today's behavior). The
        # epoch rides IN the key and every document write bumps it, so a
        # cached outcome can only be served while the corpus is unchanged —
        # soft-deleted or edited content can never surface stale.
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        tenant_id: UUID,
        tag: str | None = None,
    ) -> SearchOutcome:
        """Run both legs concurrently, gate, fuse with RRF, hydrate the top hits.

        The tenant filter applies to BOTH legs (ES term filter + PG join
        filter) and to the hydration read, so fused ranks are tenant-consistent
        and a stale index can never leak another tenant's chunks. The tag
        filter applies to BOTH legs so fused ranks are tag-consistent.
        `mode` is `"hybrid"` when the vector leg ran, `"bm25"` when it could
        not (no provider configured, or the embed call failed mid-search).

        Relevance gates sit between the legs and the response: an absolute
        floor per leg (BM25 `_score`, cosine distance — applied only to legs
        that actually ran), then a relative floor on the fused scores BEFORE
        the `limit` slice so a weak tail never consumes slots. When nothing
        survives, `items` is empty by design — empty beats noise.

        The query length cap is enforced here, once, before any leg work: the
        API and the agent tools share this single enforcement point, so an
        over-long caller query reaches both legs as its truncated prefix
        (standard-analyzer CJK turns a multi-thousand-char query into a
        Lucene clause-limit failure). Callers logging `q_length` keep seeing
        the raw caller-provided length — truncation is retriever-internal.
        """
        query = truncate_query(query, max_length=self._max_query_length)
        cache_key_search = await self._search_cache_key(
            query, limit=limit, tag=tag, tenant_id=tenant_id
        )
        if cache_key_search is not None and self._cache is not None:
            raw = await self._cache.get(cache_key_search)
            outcome: SearchOutcome | None = None
            if raw is not None:
                try:
                    outcome = SearchOutcome.from_json(raw)
                except (ValueError, KeyError, TypeError):
                    outcome = None  # unusable payload (e.g. pre-bump format)
            if outcome is not None:
                logger.info("cache_hit", domain="search")
                return outcome
            logger.info("cache_miss", domain="search")
        body = bm25_chunk_query(
            query,
            size=CANDIDATE_POOL,
            tenant_id=str(tenant_id),
            tag=tag,
            min_score=self._bm25_min_score,
            min_coverage=self._bm25_min_coverage,
        )
        es_result, vector_result = await asyncio.gather(
            search_chunks(self._es_client, index=self._es_index, body=body),
            self._vector_leg(query, tenant_id=tenant_id, tag=tag),
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

        # Absolute gates, one per leg (a leg that did not run has nothing to
        # gate). ES already pruned at `min_score`; the Python-side re-check
        # keeps the gate authoritative regardless of ES scoring quirks. The
        # vector gate is two-tier: rescue only ever fires when the ceiling
        # empties the leg AND the gated BM25 leg is empty (the lexical-failure
        # backstop) AND the leg is plausibly on-domain (the trigger), so a
        # living primary tier is bit-identical to the single-tier behavior.
        kept_es_hits = filter_es_hits(es_hits, min_score=self._bm25_min_score)
        kept_vector_rows, vector_rescued = filter_vector_rows_with_rescue(
            vector_leg.rows,
            max_distance=self._vector_max_distance,
            rescue_margin=self._vector_rescue_margin,
            rescue_max_distance=self._vector_rescue_max_distance,
            rescue_trigger_max_distance=self._vector_rescue_trigger_max_distance,
            bm25_leg_empty=(len(kept_es_hits) == 0),
        )

        es_keys = [ChunkKey(hit.document_id, hit.chunk_index) for hit in kept_es_hits]
        vector_keys = [ChunkKey(row.document_id, row.chunk_index) for row in kept_vector_rows]
        fused = fuse_rrf(es_keys, vector_keys)
        floored = apply_relative_score_floor(fused, min_relative=self._rrf_min_relative)
        top = floored[:limit]
        es_scores = {ChunkKey(hit.document_id, hit.chunk_index): hit.score for hit in kept_es_hits}

        hydrated: dict[tuple[UUID, int], ChunkRow] = {
            (row.document_id, row.chunk_index): row for row in kept_vector_rows
        }
        missing = [hit.key for hit in top if hit.key not in hydrated]
        if missing:
            async with self._session_factory() as session:
                hydrated.update(
                    await DocumentChunkRepository(session).get_live_chunks(
                        missing, tenant_id=tenant_id
                    )
                )

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
                    es_score=es_scores.get(hit.key),
                    vector_distance=row.distance,
                    content=row.content,
                    document_title=row.document_title,
                    document_tags=row.document_tags,
                )
            )
        mode: SearchMode = "hybrid" if vector_leg.ran else "bm25"
        outcome = SearchOutcome(
            mode=mode,
            items=items,
            es_hits=len(es_hits),
            vector_hits=len(vector_leg.rows),
            es_gated=len(es_hits) - len(kept_es_hits),
            vector_gated=len(vector_leg.rows) - len(kept_vector_rows),
            fused_gated=len(fused) - len(floored),
            vector_rescued=vector_rescued,
        )
        if cache_key_search is not None and self._cache is not None:
            await self._cache.set(
                cache_key_search,
                outcome.to_json(),
                ttl_seconds=self._cache_ttl_seconds,
            )
        return outcome

    async def _search_cache_key(
        self, truncated_query: str, *, limit: int, tag: str | None, tenant_id: UUID
    ) -> str | None:
        """Cache key built AFTER `truncate_query` (the key must match what the
        legs would see). The tenant id is part of the key — one tenant's
        cached outcome can never be served to another. The epoch comes from
        the same cache handle; a cache fault or caching-off returns `None`
        (compute path, no caching)."""
        if self._cache is None:
            return None
        epoch_raw = await self._cache.get(SEARCH_EPOCH_KEY)
        try:
            epoch = int(epoch_raw) if epoch_raw is not None else 0
        except ValueError:
            epoch = 0
        digest = hashlib.sha256(truncated_query.encode("utf-8")).hexdigest()
        return cache_key(
            "search", str(tenant_id), epoch, digest, limit, tag if tag is not None else "-"
        )

    async def _vector_leg(self, query: str, *, tenant_id: UUID, tag: str | None) -> _VectorLeg:
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
                vectors[0], tenant_id=tenant_id, limit=CANDIDATE_POOL, tag=tag
            )
        return _VectorLeg(rows=list(rows), ran=True)
