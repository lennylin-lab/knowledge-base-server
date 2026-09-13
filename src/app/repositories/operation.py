"""PostgreSQL data access for agent operations and revisions — queries only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operation import AgentOperation, DocumentRevision


class AgentOperationRepository:
    """Every SQL statement touching the `agent_operations` table lives here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, operation: AgentOperation) -> AgentOperation:
        """Insert and reload server-generated columns (id, timestamps)."""
        self._session.add(operation)
        await self._session.flush()
        await self._session.refresh(operation)
        return operation

    async def get_by_id(self, operation_id: UUID) -> AgentOperation | None:
        stmt = select(AgentOperation).where(AgentOperation.id == operation_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, key: str) -> AgentOperation | None:
        """The retry handle's resolution target, or None (partial unique index:
        NULL keys never match)."""
        stmt = select(AgentOperation).where(AgentOperation.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(self, operation: AgentOperation) -> AgentOperation:
        """Flush pending attribute changes and reload server-side values."""
        await self._session.flush()
        await self._session.refresh(operation)
        return operation

    async def list_for_document(
        self, document_id: UUID, *, limit: int = 50
    ) -> Sequence[AgentOperation]:
        """One document's operations, newest first (id = creation order)."""
        stmt = (
            select(AgentOperation)
            .where(AgentOperation.document_id == document_id)
            .order_by(AgentOperation.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()


class DocumentRevisionRepository:
    """Every SQL statement touching the `document_revisions` table lives here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, revision: DocumentRevision) -> DocumentRevision:
        self._session.add(revision)
        await self._session.flush()
        await self._session.refresh(revision)
        return revision

    async def get_by_id(self, revision_id: UUID) -> DocumentRevision | None:
        stmt = select(DocumentRevision).where(DocumentRevision.id == revision_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_operation_id(self, operation_id: UUID) -> DocumentRevision | None:
        """The one revision an apply produced (idempotent repeat reads this)."""
        stmt = select(DocumentRevision).where(DocumentRevision.operation_id == operation_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_document(
        self, document_id: UUID, *, limit: int = 100
    ) -> Sequence[DocumentRevision]:
        stmt = (
            select(DocumentRevision)
            .where(DocumentRevision.document_id == document_id)
            .order_by(DocumentRevision.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()
