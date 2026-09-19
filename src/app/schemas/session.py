"""Chat session request/response DTOs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.models.chat import MessageRole


class SourceRef(BaseModel):
    """Slim citation entry projected from a stored `ChatMessage.sources` hit.

    List position IS the citation number (first element = `[1]`); no explicit
    index field. Deliberately excludes `content` and the internal relevance
    signals (`score`/`es_rank`/`vector_distance`/`es_score`) — history replay
    needs only the jump-to-document mapping, not the retrieval internals.
    """

    document_id: UUID
    document_title: str
    document_tags: list[str]
    chunk_index: int


class MessageRead(BaseModel):
    """One stored chat message, as returned inside a session detail."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: MessageRole
    content: str
    run_id: UUID | None
    created_at: datetime
    sources: list[SourceRef] | None = None

    @field_validator("sources", mode="before")
    @classmethod
    def _project_sources(
        cls, value: list[dict[str, object]] | list[SourceRef] | None
    ) -> list[SourceRef] | None:
        """Project stored `SearchHit` dicts down to `SourceRef`, tolerantly.

        The column holds `SearchHit.model_dump(mode="json")` dicts written by
        this system; extra keys are ignored and a malformed entry is skipped
        rather than failing the whole read (missing citations on one message
        beat a 500 on the session).
        """
        if value is None:
            return None
        refs: list[SourceRef] = []
        for hit in value:
            try:
                refs.append(SourceRef.model_validate(hit))
            except ValidationError:
                continue
        return refs


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


class MessagePage(BaseModel):
    """One page of a keyset-paginated session-detail message history.

    Returned by `GET /sessions/{id}` when `limit` (or `cursor`) is given;
    without those params the full `SessionDetail` is returned unchanged.
    Items are chronological; `next_cursor` points at older messages and is
    null when none remain.
    """

    items: list[MessageRead]
    next_cursor: str | None


class SessionPage(BaseModel):
    """One page of a keyset-paginated session list."""

    items: list[SessionRead]
    next_cursor: str | None
