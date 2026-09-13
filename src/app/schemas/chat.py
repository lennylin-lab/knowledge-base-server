"""Chat request DTO and the SSE stream event payloads."""

from __future__ import annotations

from typing import Any, Literal
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


StatusPhase = Literal["rewriting_query", "generating"]
"""Silent-work phases the service already performs, now observable.

`rewriting_query` — a follow-up turn's best-effort rewrite LLM call is about
to run. `generating` — the first answer text is about to stream (the
tool/retrieval phase is over). Informational only: never replaces the
terminal `done` / `error` semantics."""


class StatusEvent(BaseModel):
    """One phase transition of the run; purely informational progress."""

    phase: StatusPhase


class ToolCallStartedEvent(BaseModel):
    """A tool call is about to execute.

    `args` is the parsed JSON object the model sent (`search_knowledge`
    additionally carries the `limit` resolved from run deps, so clients can
    show "searching for X" without re-parsing anything)."""

    call_id: str
    tool_name: str
    args: dict[str, Any]


class ToolCallFinishedEvent(BaseModel):
    """A tool call completed.

    `status` is `failed` when the call degraded (an MCP wrapper's error
    string returned to the model, or a retry prompt) — the run itself may
    still complete with `done`."""

    call_id: str
    tool_name: str
    status: Literal["success", "failed"]
    latency_ms: float


class QueryRewrittenEvent(BaseModel):
    """The run prompt was rewritten from the raw follow-up question.

    Emitted only when the rewrite ran, produced non-empty output, and the
    result differs from the original (`applied` and `changed`). The
    persisted message and history keep the original question either way."""

    original: str
    rewritten: str
    applied: bool
    changed: bool


ChatStreamEvent = (
    RunStartedEvent
    | SourcesEvent
    | AnswerDeltaEvent
    | DoneEvent
    | ErrorEvent
    | StatusEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | QueryRewrittenEvent
)
"""Everything `ChatService.ask` can yield, in contract order."""
