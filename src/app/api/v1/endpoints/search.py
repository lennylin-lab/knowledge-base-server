"""Search endpoint — thin: parse, one service call, map to schema."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SearchServiceDep, TenantScope
from app.schemas.search import SearchResponse

router = APIRouter()


@router.get("", response_model=SearchResponse)
async def search(
    service: SearchServiceDep,
    tenant: TenantScope,
    q: Annotated[str, Query(min_length=1, description="Free-text search query")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    tag: Annotated[str | None, Query(description="Filter by tag membership")] = None,
) -> SearchResponse:
    """Hybrid full-text (BM25) + vector search over document chunks."""
    return await service.search(q, tenant_id=tenant, limit=limit, tag=tag)
