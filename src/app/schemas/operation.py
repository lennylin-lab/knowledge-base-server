"""Agent-operation request/response DTOs.

The create/apply contract is explicit by design: a draft is submitted (or
produced by the writing agent), inspected, and only then applied — there is
no auto-publish path anywhere in this schema set.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.document import IndexStatus
from app.models.operation import OperationState

DRAFT_MAX_CHARS = 50_000  # mirrors the writing endpoint's draft bound


class DraftContent(BaseModel):
    """Structured proposed document content."""

    content: str = Field(min_length=1, max_length=DRAFT_MAX_CHARS)
    title: str | None = None


class OperationCreate(BaseModel):
    """Submit one draft operation against a target document.

    `base_document_version` is the document `updated_at` the caller saw —
    apply refuses when the live document no longer matches. The optional
    `idempotency_key` makes create/apply retries resolve to the same
    operation instead of duplicating drafts.
    """

    document_id: UUID
    base_document_version: datetime
    draft: DraftContent
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class OperationRead(BaseModel):
    """Operation as returned in listings — draft payload excluded."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID | None
    base_document_version: datetime | None
    state: OperationState
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime


class OperationReadDetail(OperationRead):
    """Single-operation view: everything plus draft/result/error payloads."""

    draft: DraftContent | None
    result: dict[str, object] | None
    error: dict[str, object] | None


class OperationTransition(BaseModel):
    """Body for the resume endpoint: mark an interrupted/failed operation
    completed so it can be applied. `draft` optionally replaces the stored
    draft (the explicit resume path may amend it)."""

    draft: DraftContent | None = None


class RevisionRead(BaseModel):
    """One published document revision (immutable snapshot)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    operation_id: UUID | None
    title: str
    tags: list[str]
    created_at: datetime


class ApplyRequest(BaseModel):
    """Body for the apply endpoint. `expected_base_document_version` is the
    optional double-check against the operation's recorded base (belt to the
    service's suspenders): a mismatch is a 409 with zero writes."""

    expected_base_document_version: datetime | None = None


class ApplyResult(BaseModel):
    """Outcome of a successful (or idempotent-repeat) apply."""

    operation: OperationReadDetail
    revision: RevisionRead
    document: DocumentInResult


class DocumentInResult(BaseModel):
    """The target document after apply: version, derived columns, status."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    tags: list[str]
    index_status: IndexStatus
    updated_at: datetime
