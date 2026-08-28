"""DocumentChunk ORM model — one embedded chunk per row (derived data)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, SmallInteger, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Spec constant, not a runtime setting — changing the dimension is a dedicated
# new-column + backfill migration (see database-guidelines.md). The Settings
# field of the same name only configures provider calls.
EMBEDDING_DIM = 1536


class DocumentChunk(Base):
    """A chunk of a document's body with its embedding vector.

    Chunks are derived data: re-indexing a document deletes and re-inserts all
    of its rows in one transaction; they are never updated in place.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_idx"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Untyped mapped_column on purpose: pgvector vectors have no Python-side
    # mapped scalar type worth faking; the column itself is NOT NULL.
    embedding = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
