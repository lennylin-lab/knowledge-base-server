"""Chat request DTO and the SSE stream event payloads."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.search import SearchHit

SearchMode = Literal["hybrid", "bm25"]
"""Retrieval mode backing the run, as wired at construction time."""


class ChatRequest(BaseModel):
    """One chat turn: a question plus retrieval sizing.

    `session_id` absent starts a new persisted session (title derived from
    the question); present continues that session (404 when it does not
    exist). A service wired without persistence ignores it entirely.
    """

    question: str = Field(min_length=1, description="Natural-language question")
    limit: int = Field(default=8, ge=1, le=20, description="Max chunks per retrieval")
    session_id: UUID | None = Field(
        default=None, description="Session to continue; absent starts a new one"
    )


class RunStartedEvent(BaseModel):
    """First event of every run; `mode` is the configured retrieval mode.

    `session_id` is the persisted conversation (None in stateless mode).
    """

    run_id: str
    mode: SearchMode
    session_id: UUID | None = None


class SourcesEvent(BaseModel):
    """One retrieval batch, flushed as soon as the tool call returned.

    Multiple events per run are possible (one per tool call); clients append.
    """

    items: list[SearchHit]


class AnswerDeltaEvent(BaseModel):
    """One streamed answer fragment."""

    text: str


class DoneEvent(BaseModel):
    """Terminal success event."""

    run_id: str
    outcome: Literal["success"]
    tool_calls: int
    latency_ms: float
    session_id: UUID | None = None


class ErrorEvent(BaseModel):
    """Terminal failure event; the stream closes right after it.

    Payload mirrors the error envelope's `{code, message}` pair — SSE cannot
    change the status code once the stream has started (error-handling spec).
    """

    code: str
    message: str


ChatStreamEvent = RunStartedEvent | SourcesEvent | AnswerDeltaEvent | DoneEvent | ErrorEvent
"""Everything `ChatService.ask` can yield, in contract order."""
