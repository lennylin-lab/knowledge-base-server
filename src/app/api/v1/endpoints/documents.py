"""Document CRUD and agent endpoints — thin: parse, one service call, respond.

The agent routes (summary/associations) stream their service generators as
SSE through the shared serializer; the priming pull turns pre-stream
failures (missing/soft-deleted document → 404) into clean JSON envelopes,
while anything after the first event arrives as a terminal `error` event the
service already emitted.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    AssociationServiceDep,
    DocumentServiceDep,
    DocumentWriteScope,
    SummarizeServiceDep,
    TenantScope,
)
from app.api.v1.endpoints.sse import primed_sse
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    AgentStreamEvent,
    AssociationItemEvent,
    AssociationsResultEvent,
    SummaryDeltaEvent,
    SummaryProgressEvent,
    SummaryResultEvent,
)
from app.schemas.chat import ErrorEvent
from app.schemas.document import (
    DocumentCreate,
    DocumentPage,
    DocumentRead,
    DocumentReadDetail,
    DocumentUpdate,
)

router = APIRouter()

_EVENT_NAMES: dict[type[AgentStreamEvent], str] = {
    AgentRunStartedEvent: "run_started",
    SummaryProgressEvent: "summary_progress",
    SummaryDeltaEvent: "summary_delta",
    SummaryResultEvent: "summary",
    AssociationItemEvent: "association_item",
    AssociationsResultEvent: "associations",
    ErrorEvent: "error",
    AgentDoneEvent: "done",
}


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


@router.post("/{document_id}/summary", response_class=EventSourceResponse, response_model=None)
async def summarize_document(
    document_id: UUID, service: SummarizeServiceDep, tenant: TenantScope
) -> EventSourceResponse:
    """Stream an LLM summary of the document as SSE (never persisted)."""
    events = service.summarize_document_stream(document_id, tenant_id=tenant)
    # Prime through the first event BEFORE the response is built: the service
    # loads the document there, so a missing/soft-deleted document raises 404
    # as a JSON envelope instead of a broken stream (see chat for the idiom).
    first = await events.__anext__()
    return EventSourceResponse(primed_sse(first, events, _EVENT_NAMES))


@router.post("/{document_id}/associations", response_class=EventSourceResponse, response_model=None)
async def associate_document(
    document_id: UUID, service: AssociationServiceDep, tenant: TenantScope
) -> EventSourceResponse:
    """Stream LLM-curated related documents as SSE (never persisted)."""
    events = service.associate_document_stream(document_id, tenant_id=tenant)
    first = await events.__anext__()  # priming: the gather's 404 stays an envelope
    return EventSourceResponse(primed_sse(first, events, _EVENT_NAMES))
