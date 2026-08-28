"""PostgreSQL data access for document chunks — queries only, no commits."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk


class DocumentChunkRepository:
    """Every SQL statement touching the `document_chunks` table lives here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_document(
        self,
        document_id: UUID,
        chunks: Sequence[str],
        embeddings: Sequence[Sequence[float]],
    ) -> int:
        """Atomically stage a whole-document replacement: delete then insert.

        Chunks are derived data, never updated in place — a re-index always
        swaps the full set. Flushes (does not commit): the transaction boundary
        belongs to the calling service.
        """
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"chunks/embeddings length mismatch: {len(chunks)} != {len(embeddings)}"
            )
        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        self._session.add_all(
            DocumentChunk(
                document_id=document_id,
                chunk_index=index,
                content=content,
                # Vector column accepts a plain sequence of floats.
                embedding=list(vector),
            )
            for index, (content, vector) in enumerate(zip(chunks, embeddings, strict=True))
        )
        await self._session.flush()
        return len(chunks)

    async def list_for_document(self, document_id: UUID) -> Sequence[DocumentChunk]:
        """All chunks of one document, ordered by position in the source."""
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()
