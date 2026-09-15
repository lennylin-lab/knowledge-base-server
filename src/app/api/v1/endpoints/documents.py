"""Document CRUD endpoints — thin: parse, one service call, map to schema."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import (
    AssociationServiceDep,
    DocumentServiceDep,
    DocumentWriteScope,
    SummarizeServiceDep,
    TenantScope,
)
from app.schemas.agents import AssociationsResult, SummaryResult
from app.schemas.document import (
    DocumentCreate,
    DocumentPage,
    DocumentRead,
    DocumentReadDetail,
    DocumentUpdate,
)

router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DocumentRead)
async def create_document(
    payload: DocumentCreate, service: DocumentServiceDep, tenant: DocumentWriteScope
) -> DocumentRead:
    """Create a document; title/tags derive from its front matter."""
    return await service.create_document(payload, tenant_id=tenant)


@router.get("", response_model=DocumentPage)
async def list_documents(
    service: DocumentServiceDep,
    tenant: TenantScope,
    cursor: Annotated[str | None, Query(description="Keyset cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    tag: Annotated[
        list[str] | None,
        Query(description="Filter by tag membership; repeat to require several tags (AND)"),
    ] = None,
) -> DocumentPage:
    """List documents, newest first, keyset-paginated."""
    return await service.list_documents(tenant_id=tenant, cursor=cursor, limit=limit, tags=tag)


@router.get("/{document_id}", response_model=DocumentReadDetail)
async def get_document(
    document_id: UUID, service: DocumentServiceDep, tenant: TenantScope
) -> DocumentReadDetail:
    """Return one document including its full content."""
    return await service.get_document(document_id, tenant_id=tenant)


@router.patch("/{document_id}", response_model=DocumentRead)
async def update_document(
    document_id: UUID,
    payload: DocumentUpdate,
    service: DocumentServiceDep,
    tenant: DocumentWriteScope,
) -> DocumentRead:
    """Partially update a document (at least one field)."""
    return await service.update_document(document_id, payload, tenant_id=tenant)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID, service: DocumentServiceDep, tenant: DocumentWriteScope
) -> None:
    """Soft-delete a document."""
    await service.delete_document(document_id, tenant_id=tenant)


@router.post("/{document_id}/summary", response_model=SummaryResult)
async def summarize_document(
    document_id: UUID, service: SummarizeServiceDep, tenant: TenantScope
) -> SummaryResult:
    """Compute an LLM summary of the document (synchronous, not persisted)."""
    return await service.summarize_document(document_id, tenant_id=tenant)


@router.post("/{document_id}/associations", response_model=AssociationsResult)
async def associate_document(
    document_id: UUID, service: AssociationServiceDep, tenant: TenantScope
) -> AssociationsResult:
    """Compute LLM-curated related documents (synchronous, not persisted)."""
    return await service.associate_document(document_id, tenant_id=tenant)
