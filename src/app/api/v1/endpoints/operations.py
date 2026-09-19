"""Agent-operation endpoints — thin: parse, one service call, map to schema.

Create, inspect, list, resume, and apply are plain JSON endpoints; the apply
endpoint is the ONLY publish path for agent drafts (no silent auto-publish
exists anywhere). The draft run streams typed SSE events through the shared
serializer, with the priming pattern turning pre-stream failures (drafting
not configured → 503, missing/soft-deleted document → 404) into clean JSON
envelopes while anything after the first event arrives as the terminal
`error` event the service already emitted. Domain failures on the JSON
endpoints surface as `AppError` subclasses from the service; the shared
handler renders the envelope.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sse_starlette.sse import EventSourceResponse

from app.api.deps import (
    AgentOperationServiceDep,
    OperationApplyScope,
    OperationCreateScope,
    TenantScope,
)
from app.api.v1.endpoints.sse import primed_sse
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    AgentStreamEvent,
    DraftDeltaEvent,
    OperationDraftEvent,
)
from app.schemas.chat import ErrorEvent
from app.schemas.operation import (
    ApplyRequest,
    ApplyResult,
    OperationCreate,
    OperationReadDetail,
    OperationTransition,
)

router = APIRouter()

_EVENT_NAMES: dict[type[AgentStreamEvent], str] = {
    AgentRunStartedEvent: "run_started",
    DraftDeltaEvent: "draft_delta",
    OperationDraftEvent: "draft",
    ErrorEvent: "error",
    AgentDoneEvent: "done",
}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OperationReadDetail)
async def create_operation(
    payload: OperationCreate, service: AgentOperationServiceDep, tenant: OperationCreateScope
) -> OperationReadDetail:
    """Submit a draft operation against a document (idempotent on key)."""
    return await service.create_operation(payload, tenant_id=tenant)


@router.post("/draft", response_class=EventSourceResponse, response_model=None)
async def draft_operation(
    document_id: UUID,
    service: AgentOperationServiceDep,
    tenant: OperationCreateScope,
    instruction: str | None = None,
) -> EventSourceResponse:
    """Stream the writing agent's structured draft as SSE; the operation is
    persisted `running` -> `completed`/`failed` (never auto-applied)."""
    events = service.draft_document_stream(document_id, instruction, tenant_id=tenant)
    # Prime through the first event BEFORE the response is built: the no-key
    # 503 gate and the document 404 raise there, so they stay JSON envelopes
    # instead of a broken stream (same idiom as summarize/associations).
    first = await events.__anext__()
    return EventSourceResponse(primed_sse(first, events, _EVENT_NAMES))


@router.get("/{operation_id}", response_model=OperationReadDetail)
async def get_operation(
    operation_id: UUID, service: AgentOperationServiceDep, tenant: TenantScope
) -> OperationReadDetail:
    """Inspect one operation, draft payload included."""
    return await service.get_operation(operation_id, tenant_id=tenant)


@router.get("/documents/{document_id}", response_model=list[OperationReadDetail])
async def list_operations(
    document_id: UUID, service: AgentOperationServiceDep, tenant: TenantScope
) -> list[OperationReadDetail]:
    """List a document's operations, newest first (explicit audit view)."""
    return await service.list_operations(document_id, tenant_id=tenant)


@router.post("/{operation_id}/resume", response_model=OperationReadDetail)
async def resume_operation(
    operation_id: UUID,
    service: AgentOperationServiceDep,
    tenant: OperationCreateScope,
    payload: OperationTransition | None = None,
) -> OperationReadDetail:
    """Resume an interrupted/failed operation (optionally amending its
    draft); it becomes completed and applicable."""
    return await service.resume_operation(
        operation_id, payload or OperationTransition(), tenant_id=tenant
    )


@router.post("/{operation_id}/apply", response_model=ApplyResult)
async def apply_operation(
    operation_id: UUID,
    service: AgentOperationServiceDep,
    tenant: OperationApplyScope,
    payload: ApplyRequest | None = None,
) -> ApplyResult:
    """Publish a draft atomically: version-checked, idempotent, one revision,
    indexing enqueued after commit."""
    return await service.apply_operation(operation_id, payload or ApplyRequest(), tenant_id=tenant)
