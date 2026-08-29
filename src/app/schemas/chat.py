"""Chat request DTO and the SSE stream event payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.search import SearchHit

SearchMode = Literal["hybrid", "bm25"]
"""Retrieval mode backing the run, as wired at construction time."""


class ChatRequest(BaseModel):
    """One stateless chat turn: a question plus retrieval sizing."""

    question: str = Field(min_length=1, description="Natural-language question")
    limit: int = Field(default=8, ge=1, le=20, description="Max chunks per retrieval")


class RunStartedEvent(BaseModel):
    """First event of every run; `mode` is the configured retrieval mode."""

    run_id: str
    mode: SearchMode


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


class ErrorEvent(BaseModel):
    """Terminal failure event; the stream closes right after it.

    Payload mirrors the error envelope's `{code, message}` pair — SSE cannot
    change the status code once the stream has started (error-handling spec).
    """

    code: str
    message: str


ChatStreamEvent = RunStartedEvent | SourcesEvent | AnswerDeltaEvent | DoneEvent | ErrorEvent
"""Everything `ChatService.ask` can yield, in contract order."""
