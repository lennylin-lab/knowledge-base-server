"""Chat session endpoints — thin: parse, one service call, map to schema.

Sessions are created implicitly by the chat endpoint (first question of a
conversation); this router owns the user-driven reads and soft delete.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import ChatSessionServiceDep
from app.schemas.session import SessionDetail, SessionPage

router = APIRouter()


@router.get("", response_model=SessionPage)
async def list_sessions(
    service: ChatSessionServiceDep,
    cursor: Annotated[str | None, Query(description="Keyset cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionPage:
    """List chat sessions, most recently updated first, keyset-paginated."""
    return await service.list_sessions(cursor=cursor, limit=limit)


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID, service: ChatSessionServiceDep) -> SessionDetail:
    """Return one session with its messages in chronological order."""
    return await service.get_session(session_id)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: UUID, service: ChatSessionServiceDep) -> None:
    """Soft-delete a session; it disappears from every read path."""
    await service.delete_session(session_id)
