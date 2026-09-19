"""Chat session endpoints — thin: parse, one service call, map to schema.

Sessions are created implicitly by the chat endpoint (first question of a
conversation); this router owns the user-driven reads and soft delete.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import ChatSessionServiceDep, SessionWriteScope, TenantScope
from app.schemas.session import MessagePage, SessionDetail, SessionPage

router = APIRouter()


@router.get("", response_model=SessionPage)
async def list_sessions(
    service: ChatSessionServiceDep,
    tenant: TenantScope,
    cursor: Annotated[str | None, Query(description="Keyset cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionPage:
    """List chat sessions, most recently updated first, keyset-paginated."""
    return await service.list_sessions(tenant_id=tenant, cursor=cursor, limit=limit)


@router.get("/{session_id}", response_model=SessionDetail | MessagePage)
async def get_session(
    session_id: UUID,
    service: ChatSessionServiceDep,
    tenant: TenantScope,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(description="Keyset cursor from a previous page")] = None,
) -> SessionDetail | MessagePage:
    """Return one session; full detail by default, or one page of messages.

    Without `limit`/`cursor` the legacy full `SessionDetail` is returned
    (all messages, chronological). With `limit` the response is a
    `MessagePage`: the newest `limit` messages in ascending order plus a
    `next_cursor` (null when no older messages remain); each cursor step
    returns the page strictly older than the cursor.
    """
    if limit is None and cursor is None:
        return await service.get_session(session_id, tenant_id=tenant)
    return await service.get_session_page(
        session_id,
        tenant_id=tenant,
        cursor=cursor,
        limit=limit,  # None -> the service's default page size
    )


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID, service: ChatSessionServiceDep, tenant: SessionWriteScope
) -> None:
    """Soft-delete a session; it disappears from every read path."""
    await service.delete_session(session_id, tenant_id=tenant)
