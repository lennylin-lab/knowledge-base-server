"""PostgreSQL data access for documents — queries only, no commits."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import any_, func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, IndexStatus


class DocumentRepository:
    """Every SQL statement touching the `documents` table lives here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, document: Document) -> Document:
        """Insert and reload server-generated columns (id, timestamps, defaults)."""
        self._session.add(document)
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def get_by_id(self, doc_id: UUID) -> Document | None:
        """Fetch one non-deleted document."""
        stmt = select(Document).where(Document.id == doc_id, Document.deleted_at.is_(None))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        *,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 20,
        tag: str | None = None,
    ) -> Sequence[Document]:
        """Keyset-paginated listing in `created_at DESC, id DESC` order.

        Fetches `limit + 1` rows so the caller can tell whether another page
        exists without a separate count query.
        """
        stmt = select(Document).where(Document.deleted_at.is_(None))
        if tag is not None:
            stmt = stmt.where(literal(tag) == any_(Document.tags))
        if cursor is not None:
            # Row-value comparison: PG compares (created_at, id) lexicographically,
            # which is exactly the keyset predicate for the DESC, DESC ordering.
            # Plain scalars in tuple_ are runtime-supported (auto-literalized);
            # the stubs only type expressions.
            stmt = stmt.where(
                tuple_(Document.created_at, Document.id) < tuple_(*cursor)  # type: ignore[arg-type]
            )
        stmt = stmt.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
        return (await self._session.execute(stmt)).scalars().all()

    async def update(self, document: Document) -> Document:
        """Flush pending attribute changes and reload server-side values."""
        await self._session.flush()
        await self._session.refresh(document)
        return document

    async def soft_delete(self, document: Document) -> None:
        """Mark deleted; hard delete is never exposed in the MVP."""
        # Deliberate SQL-expression assignment: now() is bound server-side so
        # the DB clock stays authoritative, same policy as the column defaults.
        document.deleted_at = func.now()

    async def set_index_status(self, doc_id: UUID, status: IndexStatus) -> None:
        """One UPDATE, no ORM load; the caller owns the transaction boundary."""
        await self._session.execute(
            update(Document).where(Document.id == doc_id).values(index_status=status)
        )

    async def list_by_index_status(
        self, statuses: Sequence[IndexStatus], *, limit: int
    ) -> Sequence[Document]:
        """Live documents in any of `statuses`, oldest first (bounded sweep)."""
        stmt = (
            select(Document)
            .where(Document.index_status.in_(list(statuses)), Document.deleted_at.is_(None))
            .order_by(Document.created_at.asc(), Document.id.asc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()
