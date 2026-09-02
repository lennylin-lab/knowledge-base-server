"""Chat session request/response DTOs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.chat import MessageRole


class MessageRead(BaseModel):
    """One stored chat message, as returned inside a session detail."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: MessageRole
    content: str
    run_id: UUID | None
    created_at: datetime


class SessionRead(BaseModel):
    """Chat session as returned in lists — messages excluded."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionRead):
    """Single-session view: everything in `SessionRead` plus its messages."""

    messages: list[MessageRead]


class SessionPage(BaseModel):
    """One page of a keyset-paginated session list."""

    items: list[SessionRead]
    next_cursor: str | None
