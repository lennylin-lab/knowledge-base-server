"""Agent-operation ORM models — controlled agent-generated document drafts.

The agent-operation domain sits beside chat and documents: a run (or an
explicit API submission) persists a structured draft against a target
document plus the base version it was drafted from, and only an explicit
apply publishes it — atomically, with optimistic version checking and
idempotency. Drafts and interrupted runs NEVER enter `chat_messages`; this
table is the only durable record of an agent's proposed document content.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.utils.ids import uuid7


class OperationState(StrEnum):
    """Lifecycle of one agent operation.

    `running` = draft not yet finalized (agent in flight or submission
    awaiting completion); `completed` = durable draft, ready to inspect or
    apply; `interrupted` = run cut off mid-flight, draft kept for explicit
    resume; `failed` = run errored (error details on the row); `applied` =
    published as a document revision (terminal)."""

    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    APPLIED = "applied"


class AgentOperation(Base):
    """One controlled agent operation over a target document.

    `draft` (JSONB) is the structured proposed content (content + optional
    title); `base_document_version` is the target document's `updated_at`
    observed when the draft was made — the optimistic-concurrency anchor for
    apply. `result` carries the applied revision id; `error` carries failure
    details. `idempotency_key` is the caller-supplied retry handle (unique
    where present).
    """

    __tablename__ = "agent_operations"
    __table_args__ = (
        # Idempotent create/apply: the caller's retry handle must resolve to
        # exactly one operation. Partial: NULL keys (no-retry callers) are
        # exempt from uniqueness.
        Index(
            "ux_agent_operations_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Per-document operation history, newest first (id = creation order).
        Index("ix_agent_operations_document_id", "document_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # The target document. SET NULL (not CASCADE): an operation's audit trail
    # survives its document's deletion; applying to a deleted document fails
    # on the live-document check, not on a dangling FK.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # The document `updated_at` the draft was made against (None = drafted
    # without a known base; apply then refuses — no version to match).
    base_document_version: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[OperationState] = mapped_column(
        # values_callable: persist the lowercase *values* ("running"), not the
        # member names ("RUNNING") — the server_default below must match.
        SAEnum(
            OperationState,
            name="operation_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=OperationState.RUNNING.value,
    )
    # Structured proposed content: {"content": str, "title": str | null}.
    draft: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # Applied outcome: {"revision_id": <uuid>}; NULL until applied.
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # Failure details for failed/interrupted runs: {"error_class": ...}.
    error: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DocumentRevision(Base):
    """One published document revision — the immutable apply audit trail.

    Created exactly once per successful apply (idempotency guarantees no
    second row for the same operation). The snapshot is the content the
    apply published (post front-matter derivation), so a revision can be
    replayed or diffed without re-deriving anything.
    """

    __tablename__ = "document_revisions"
    __table_args__ = (
        # A document's revision history, chronological (id = creation order).
        Index("ix_document_revisions_document_id", "document_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # The operation that produced this revision; SET NULL keeps the revision
    # row when the operation is ever removed (revisions are the durable trail).
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_operations.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
