"""PostgreSQL data access for chat sessions and messages — queries only, no
commits (transaction boundaries belong to services)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import ChatMessage, ChatSession


class ChatSessionRepository:
    """Every SQL statement touching the `chat_sessions` table lives here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, chat_session: ChatSession) -> ChatSession:
        """Insert and reload server-generated columns (id, timestamps)."""
        self._session.add(chat_session)
        await self._session.flush()
        await self._session.refresh(chat_session)
        return chat_session

    async def get_by_id(self, session_id: UUID) -> ChatSession | None:
        """Fetch one non-deleted session."""
        stmt = select(ChatSession).where(
            ChatSession.id == session_id, ChatSession.deleted_at.is_(None)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        *,
        cursor: tuple[datetime, UUID] | None = None,
        limit: int = 20,
    ) -> Sequence[ChatSession]:
        """Keyset-paginated listing in `updated_at DESC, id DESC` order.

        Fetches `limit + 1` rows so the caller can tell whether another page
        exists without a separate count query.
        """
        stmt = select(ChatSession).where(ChatSession.deleted_at.is_(None))
        if cursor is not None:
            # Row-value comparison: PG compares (updated_at, id)
            # lexicographically — exactly the keyset predicate for the
            # DESC, DESC ordering. Plain scalars in tuple_ are
            # runtime-supported (auto-literalized); the stubs only type
            # expressions.
            stmt = stmt.where(
                tuple_(ChatSession.updated_at, ChatSession.id) < tuple_(*cursor)  # type: ignore[arg-type]
            )
        stmt = stmt.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc()).limit(limit + 1)
        return (await self._session.execute(stmt)).scalars().all()

    async def touch(self, session_id: UUID) -> None:
        """Advance `updated_at` to now; the caller owns the transaction.

        Message inserts do not touch their session's row, so recency ordering
        (`updated_at DESC`) needs this explicit bump per persisted turn.
        """
        await self._session.execute(
            update(ChatSession).where(ChatSession.id == session_id).values(updated_at=func.now())
        )

    async def soft_delete(self, chat_session: ChatSession) -> None:
        """Mark deleted; hard delete is never exposed in the MVP."""
        # Deliberate SQL-expression assignment: now() is bound server-side so
        # the DB clock stays authoritative (same policy as the column defaults).
        chat_session.deleted_at = func.now()


class ChatMessageRepository:
    """Every SQL statement touching the `chat_messages` table lives here.

    Messages carry no soft-delete of their own: every read joins its live
    session, so a deleted session's messages vanish with it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, message: ChatMessage) -> ChatMessage:
        """Insert and reload server-generated columns (id, created_at)."""
        self._session.add(message)
        await self._session.flush()
        await self._session.refresh(message)
        return message

    async def list_for_session(self, session_id: UUID) -> Sequence[ChatMessage]:
        """All messages of one live session, chronological."""
        stmt = (
            select(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatSession.id == session_id, ChatSession.deleted_at.is_(None))
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def list_recent_for_session(
        self, session_id: UUID, *, limit: int
    ) -> Sequence[ChatMessage]:
        """Newest-first bounded read of one live session's messages.

        The bound is a message-count cap for the history-window read; the
        char budget that governs what reaches the model is applied by the
        caller (`services.chat.select_history_window`).
        """
        stmt = (
            select(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatSession.id == session_id, ChatSession.deleted_at.is_(None))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()
