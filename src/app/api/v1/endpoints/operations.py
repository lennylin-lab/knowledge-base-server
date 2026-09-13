"""Agent-operation endpoints — thin: parse, one service call, map to schema.

Create, inspect, list, resume, and apply are plain JSON endpoints; the
apply endpoint is the ONLY publish path for agent drafts (no silent
auto-publish exists anywhere). Domain failures surface as `AppError`
subclasses from the service; the shared handler renders the envelope.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import AgentOperationServiceDep
from app.schemas.operation import (
    ApplyRequest,
    ApplyResult,
    OperationCreate,
    OperationReadDetail,
    OperationTransition,
)

router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OperationReadDetail)
async def create_operation(
    payload: OperationCreate, service: AgentOperationServiceDep
) -> OperationReadDetail:
    """Submit a draft operation against a document (idempotent on key)."""
    return await service.create_operation(payload)


@router.post("/draft", response_model=OperationReadDetail)
async def draft_operation(
    document_id: UUID,
    service: AgentOperationServiceDep,
    instruction: str | None = None,
) -> OperationReadDetail:
    """Run the writing agent's structured draft against a live document and
    persist it as a `running` -> `completed` operation (never auto-applied)."""
    return await service.draft_document(document_id, instruction)


@router.get("/{operation_id}", response_model=OperationReadDetail)
async def get_operation(
    operation_id: UUID, service: AgentOperationServiceDep
) -> OperationReadDetail:
    """Inspect one operation, draft payload included."""
    return await service.get_operation(operation_id)


@router.get("/documents/{document_id}", response_model=list[OperationReadDetail])
async def list_operations(
    document_id: UUID, service: AgentOperationServiceDep
) -> list[OperationReadDetail]:
    """List a document's operations, newest first (explicit audit view)."""
    return await service.list_operations(document_id)


@router.post("/{operation_id}/resume", response_model=OperationReadDetail)
async def resume_operation(
    operation_id: UUID,
    service: AgentOperationServiceDep,
    payload: OperationTransition | None = None,
) -> OperationReadDetail:
    """Resume an interrupted/failed operation (optionally amending its
    draft); it becomes completed and applicable."""
    return await service.resume_operation(operation_id, payload or OperationTransition())


@router.post("/{operation_id}/apply", response_model=ApplyResult)
async def apply_operation(
    operation_id: UUID,
    service: AgentOperationServiceDep,
    payload: ApplyRequest | None = None,
) -> ApplyResult:
    """Publish a draft atomically: version-checked, idempotent, one revision,
    indexing enqueued after commit."""
    return await service.apply_operation(operation_id, payload or ApplyRequest())
