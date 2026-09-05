"""Search response DTOs."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class SearchHit(BaseModel):
    """One fused chunk hit with its owning document's metadata.

    `score` is the RRF fused score; per-leg ranks are 1-based and `None` when
    that leg did not return the chunk. `es_score`/`vector_distance` carry the
    raw per-leg relevance signal when that leg ranked the chunk (`None`
    otherwise) so clients and tests can audit the retriever's relevance gates.
    """

    document_id: UUID
    document_title: str
    document_tags: list[str]
    chunk_index: int
    content: str
    score: float
    es_rank: int | None
    vector_rank: int | None
    es_score: float | None = None
    vector_distance: float | None = None


class SearchResponse(BaseModel):
    """Search result: what produced it (`mode`) and the top hits."""

    mode: Literal["hybrid", "bm25"]
    items: list[SearchHit]
