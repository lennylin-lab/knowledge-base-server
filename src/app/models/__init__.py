"""ORM models. Importing this package registers every table on Base.metadata
(what Alembic autogen compares against)."""

from __future__ import annotations

from app.models.document import Document, IndexStatus
from app.models.document_chunk import EMBEDDING_DIM, DocumentChunk

__all__ = ["EMBEDDING_DIM", "Document", "DocumentChunk", "IndexStatus"]
