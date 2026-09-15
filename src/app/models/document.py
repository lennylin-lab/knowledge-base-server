"""Document ORM model — the core knowledge-base entity."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.utils.ids import uuid7


class IndexStatus(StrEnum):
    """Lifecycle of the indexing pipeline for a document's content."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class Document(Base):
    """A Markdown knowledge document.

    `content` (front matter included) is the source of truth; `title` and
    `tags` are derived read-model columns maintained by the service layer.
    """

    __tablename__ = "documents"
    __table_args__ = (
        # Tag membership filter (?tag=) — GIN over the array.
        # Listing needs no extra index: UUIDv7 ids order by creation time, so
        # the keyset `ORDER BY id DESC` rides the primary-key index.
        Index("ix_documents_tags", "tags", postgresql_using="gin"),
        # Tenant listing keyset: every read filters tenant_id, then orders by
        # id DESC (uuid7 creation order) — the composite serves the scan.
        Index("ix_documents_tenant_id", "tenant_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # Owning tenant (Stage 5): non-null, every query filters it. FK to tenants.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="Untitled")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex digest of `content` (front matter included); the service
    # layer compares it to skip reindexing byte-identical saves. NULL means
    # "unknown, treat as changed" — pre-backfill rows reindex once.
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    index_status: Mapped[IndexStatus] = mapped_column(
        # values_callable: persist the lowercase *values* ("pending"), not the
        # member names ("PENDING") — the server_default below must match.
        SAEnum(
            IndexStatus,
            name="index_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=IndexStatus.PENDING.value,
    )
    # Reserved for multi-user: schema is ready, auth is not (PRD out of scope).
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
