"""Document CRUD endpoints — thin: parse, one service call, map to schema."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import DocumentServiceDep
from app.schemas.document import (
    DocumentCreate,
    DocumentPage,
    DocumentRead,
    DocumentReadDetail,
    DocumentUpdate,
)

router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=DocumentRead)
async def create_document(payload: DocumentCreate, service: DocumentServiceDep) -> DocumentRead:
    """Create a document; title/tags derive from its front matter."""
    return await service.create_document(payload)


@router.get("", response_model=DocumentPage)
async def list_documents(
    service: DocumentServiceDep,
    cursor: Annotated[str | None, Query(description="Keyset cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    tag: Annotated[str | None, Query(description="Filter by tag membership")] = None,
) -> DocumentPage:
    """List documents, newest first, keyset-paginated."""
    return await service.list_documents(cursor=cursor, limit=limit, tag=tag)


@router.get("/{document_id}", response_model=DocumentReadDetail)
async def get_document(document_id: UUID, service: DocumentServiceDep) -> DocumentReadDetail:
    """Return one document including its full content."""
    return await service.get_document(document_id)


@router.patch("/{document_id}", response_model=DocumentRead)
async def update_document(
    document_id: UUID, payload: DocumentUpdate, service: DocumentServiceDep
) -> DocumentRead:
    """Partially update a document (at least one field)."""
    return await service.update_document(document_id, payload)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: UUID, service: DocumentServiceDep) -> None:
    """Soft-delete a document."""
    await service.delete_document(document_id)
