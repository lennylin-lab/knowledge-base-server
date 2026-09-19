"""Chat-stream SSE wire serializer — typed facade over the shared `sse` module.

Endpoints streaming the chat event vocabulary (chat, writing, later siblings)
import `to_sse` / `primed_sse` from here. Payloads live in `schemas/chat.py`;
the generic serializer lives in `sse.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.api.v1.endpoints.sse import primed_sse as _shared_primed_sse
from app.api.v1.endpoints.sse import to_sse as _shared_to_sse
from app.api.v1.endpoints.sse import to_sse_event as _shared_to_sse_event
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    QueryRewrittenEvent,
    RunStartedEvent,
    SourcesEvent,
    StatusEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)

_EVENT_NAMES: dict[type[ChatStreamEvent], str] = {
    RunStartedEvent: "run_started",
    SourcesEvent: "sources",
    AnswerDeltaEvent: "answer_delta",
    DoneEvent: "done",
    ErrorEvent: "error",
    StatusEvent: "status",
    ToolCallStartedEvent: "tool_call_started",
    ToolCallFinishedEvent: "tool_call_finished",
    QueryRewrittenEvent: "query_rewritten",
}


def to_sse_event(event: ChatStreamEvent) -> dict[str, str]:
    """Serialize one typed event to sse-starlette's dict shape."""
    return _shared_to_sse_event(event, _EVENT_NAMES)


async def to_sse(events: AsyncIterator[ChatStreamEvent]) -> AsyncIterator[dict[str, str]]:
    """Serialize a typed event stream to sse-starlette's dict shape."""
    async for frame in _shared_to_sse(events, _EVENT_NAMES):
        yield frame


async def primed_sse(
    first: ChatStreamEvent, rest: AsyncIterator[ChatStreamEvent]
) -> AsyncIterator[dict[str, str]]:
    """`to_sse` with the first event already materialized (see `chat`)."""
    async for frame in _shared_primed_sse(first, rest, _EVENT_NAMES):
        yield frame
