"""PostgreSQL data access for document chunks — queries only, no commits."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NamedTuple
from uuid import UUID

from sqlalchemy import Row, any_, delete, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.document_chunk import DocumentChunk


class ChunkRow(NamedTuple):
    """A chunk hydrated with its live document's metadata (retrieval read model)."""

    document_id: UUID
    chunk_index: int
    content: str
    document_title: str
    document_tags: list[str]


# Shared shape for both retrieval reads: chunk columns + owning document,
# live documents only (`deleted_at IS NULL`) — PG is the single source of
# truth for visibility, so ES-side staleness cannot leak results. `Select` is
# generative/immutable, so deriving statements from this constant is safe.
_LIVE_CHUNK_SELECT = (
    select(
        DocumentChunk.document_id,
        DocumentChunk.chunk_index,
        DocumentChunk.content,
        Document.title.label("document_title"),
        Document.tags.label("document_tags"),
    )
    .join(Document, Document.id == DocumentChunk.document_id)
    .where(Document.deleted_at.is_(None))
)


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

    async def search_similar(
        self, embedding: Sequence[float], *, limit: int, tag: str | None = None
    ) -> Sequence[ChunkRow]:
        """Vector leg: nearest chunks of live documents, hydrated inline.

        Ordering delegates to pgvector cosine distance (HNSW-backed). The
        optional tag filter mirrors the ES leg's term filter so fused ranks
        are tag-consistent across both legs.
        """
        stmt = _LIVE_CHUNK_SELECT.order_by(
            DocumentChunk.embedding.cosine_distance(embedding)
        ).limit(limit)
        if tag is not None:
            stmt = stmt.where(literal(tag) == any_(Document.tags))
        rows = (await self._session.execute(stmt)).all()
        return [_as_chunk_row(row) for row in rows]

    async def get_live_chunks(
        self, keys: Sequence[tuple[UUID, int]]
    ) -> dict[tuple[UUID, int], ChunkRow]:
        """Hydrate `(document_id, chunk_index)` keys from live documents.

        Keys absent from the result were soft-deleted after indexing — the
        caller drops them. Callers bound the key list by `CANDIDATE_POOL`, so
        the tuple-IN list never explodes.
        """
        if not keys:
            return {}
        stmt = _LIVE_CHUNK_SELECT.where(
            tuple_(DocumentChunk.document_id, DocumentChunk.chunk_index).in_(list(keys))
        )
        rows = (await self._session.execute(stmt)).all()
        return {(row.document_id, row.chunk_index): _as_chunk_row(row) for row in rows}


def _as_chunk_row(row: Row[Any]) -> ChunkRow:
    """Map one joined row onto the `ChunkRow` read model."""
    return ChunkRow(
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        document_title=row.document_title,
        document_tags=list(row.document_tags),
    )
