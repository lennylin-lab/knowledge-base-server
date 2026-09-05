"""Search orchestration: normalize, retrieve, map to response schemas."""

from __future__ import annotations

import time

import structlog

from app.rag.retriever import Retriever
from app.schemas.search import SearchHit, SearchResponse

logger = structlog.get_logger(__name__)


class SearchService:
    """User-facing search over the hybrid retriever."""

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def search(self, q: str, *, limit: int = 10, tag: str | None = None) -> SearchResponse:
        """Run one hybrid search and map the outcome to response schemas.

        The query text never reaches the logs — queries may contain sensitive
        phrasing, so the event carries `q_length` only (see logging spec).
        The `*_gated` counters expose how many candidates each relevance gate
        dropped (raw leg sizes minus survivors; the relative floor's drops
        are `fused_gated`) — counts only, never query text.
        """
        started = time.perf_counter()
        normalized_tag = tag.strip().lower() if tag else None
        outcome = await self._retriever.retrieve(q, limit=limit, tag=normalized_tag)
        response = SearchResponse(
            mode=outcome.mode,
            items=[
                SearchHit(
                    document_id=item.key.document_id,
                    document_title=item.document_title,
                    document_tags=list(item.document_tags),
                    chunk_index=item.key.chunk_index,
                    content=item.content,
                    score=item.score,
                    es_rank=item.es_rank,
                    vector_rank=item.vector_rank,
                    es_score=item.es_score,
                    vector_distance=item.vector_distance,
                )
                for item in outcome.items
            ],
        )
        logger.info(
            "search_executed",
            q_length=len(q),
            limit=limit,
            tag=normalized_tag,
            mode=outcome.mode,
            hit_count=len(response.items),
            es_hits=outcome.es_hits,
            vector_hits=outcome.vector_hits,
            es_gated=outcome.es_gated,
            vector_gated=outcome.vector_gated,
            fused_gated=outcome.fused_gated,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return response
