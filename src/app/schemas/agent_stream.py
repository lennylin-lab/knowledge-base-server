"""SSE stream event payloads for the document agent endpoints.

The summarize and associations endpoints stream typed events over the same
wire discipline as chat (error-handling spec): an opening `run_started`,
optional informational progress, one complete result event, then a terminal
`done` — or a terminal `error` (chat's `ErrorEvent`, reused verbatim so the
two streams never grow a second error dialect) when a failure happens after
HTTP 200. The result events carry the full existing `SummaryResult` /
`AssociationsResult` payloads flat, so a client reading only the result event
sees exactly the old synchronous response body.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.agents import AssociationsResult, SummaryResult
from app.schemas.chat import ErrorEvent

AgentKind = Literal["summary", "associations", "draft"]
"""Which agent run the stream carries."""


class AgentRunStartedEvent(BaseModel):
    """First event of every agent stream; identifies the run and document."""

    run_id: str
    kind: AgentKind
    document_id: UUID


ProgressPhase = Literal["map_pass", "reduce_pass"]
"""Summarize orchestration phases observable on the wire.

`map_pass` — one chunk is being summarized (one event per pass, in order).
`reduce_pass` — the combine pass over the chunk summaries. Progress events
are informational only and keep a fixed grammar (map passes + the reduce
pass, even when the document fits a single chunk); they never replace the
terminal `done` / `error` semantics."""


class SummaryProgressEvent(BaseModel):
    """One summarize orchestration pass is about to run.

    `pass_index` is 1-based; `passes_total` is the map passes plus the reduce
    pass, so the reduce pass always reports `pass_index == passes_total`."""

    phase: ProgressPhase
    pass_index: int
    passes_total: int


class SummaryResultEvent(SummaryResult):
    """The completed summary — the full `SummaryResult` payload, flat."""


class AssociationsResultEvent(AssociationsResult):
    """The completed association result — the full `AssociationsResult`
    payload (deterministic candidate metadata plus LLM reasons), flat."""


class OperationDraftEvent(BaseModel):
    """The completed draft operation — additive draft-stream result event.

    Emitted AFTER the operation's terminal state (`completed`) is committed,
    right before the terminal `done`: a client that stops reading never sees
    a draft for unpersisted work. The payload is flat (operation identity,
    state, draft content) — never an auto-publish signal; applying stays the
    explicit `POST /operations/{id}/apply` endpoint.
    """

    operation_id: UUID
    state: Literal["completed"]
    content: str
    title: str | None


class AgentDoneEvent(BaseModel):
    """Terminal success event; the stream closes right after it."""

    run_id: str
    outcome: Literal["success"]
    latency_ms: float


AgentStreamEvent = (
    AgentRunStartedEvent
    | SummaryProgressEvent
    | SummaryResultEvent
    | AssociationsResultEvent
    | OperationDraftEvent
    | AgentDoneEvent
    | ErrorEvent
)
"""Everything the summarize/association streams can yield, in contract order."""
