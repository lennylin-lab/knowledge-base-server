"""Chat session business logic: listing, detail, soft delete, title derivation.

Sessions are created implicitly by `ChatService.ask` (first question of a
conversation); this service owns every user-driven read and delete.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.chat import ChatSession
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.schemas.session import MessageRead, SessionDetail, SessionPage, SessionRead
from app.utils.cursor import decode_cursor, encode_cursor

logger = structlog.get_logger(__name__)

# Session titles derive from the first question: single line, truncated.
TITLE_MAX_LENGTH = 80


def derive_title(question: str, *, max_length: int = TITLE_MAX_LENGTH) -> str:
    """Session title from its first question — one line, ~80 chars, no LLM.

    Whitespace collapses to single spaces (a title never spans lines); an
    over-long question truncates with an ellipsis so the total stays within
    `max_length`. A whitespace-only question falls back to the column default.
    """
    single_line = " ".join(question.split())
    if not single_line:
        return "New chat"
    if len(single_line) <= max_length:
        return single_line
    return single_line[: max_length - 1].rstrip() + "…"


class ChatSessionService:
    """Orchestrates session reads, soft delete, and cursor pagination."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._sessions = ChatSessionRepository(session)
        self._messages = ChatMessageRepository(session)

    async def list_sessions(
        self, *, tenant_id: UUID, cursor: str | None = None, limit: int = 20
    ) -> SessionPage:
        """Keyset-paginated listing, most recently updated first, scoped to
        the caller's tenant."""
        decoded = decode_cursor(cursor) if cursor is not None else None
        rows = await self._sessions.list_page(tenant_id=tenant_id, cursor=decoded, limit=limit)

        next_cursor: str | None = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last.updated_at, last.id)

        return SessionPage(
            items=[SessionRead.model_validate(row) for row in rows],
            next_cursor=next_cursor,
        )

    async def get_session(self, session_id: UUID, *, tenant_id: UUID) -> SessionDetail:
        """Return one live session with its messages, chronological."""
        chat_session = await self._get_or_raise(session_id, tenant_id=tenant_id)
        messages: Sequence[MessageRead] = [
            MessageRead.model_validate(message)
            for message in await self._messages.list_for_session(session_id, tenant_id=tenant_id)
        ]
        return SessionDetail(
            id=chat_session.id,
            title=chat_session.title,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
            messages=list(messages),
        )

    async def delete_session(self, session_id: UUID, *, tenant_id: UUID) -> None:
        """Soft-delete a session; it disappears from every read path."""
        chat_session = await self._get_or_raise(session_id, tenant_id=tenant_id)
        await self._sessions.soft_delete(chat_session)
        await self._session.commit()
        logger.info("session_deleted", session_id=str(chat_session.id))

    async def _get_or_raise(self, session_id: UUID, *, tenant_id: UUID) -> ChatSession:
        """Fetch one live session; soft-deleted counts as missing."""
        chat_session = await self._sessions.get_by_id(session_id, tenant_id=tenant_id)
        if chat_session is None:
            # One non-leaky 404 for both "does not exist" and "another
            # tenant's session".
            raise NotFoundError(f"Chat session {session_id} not found")
        return chat_session
