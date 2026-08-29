"""Chat endpoint — thin: parse, one service call, map events to SSE.

The router never iterates the stream itself; it hands the event generator to
`sse-starlette` in wire format. Failures inside the generator have already
been converted to terminal `error` events by the service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ChatServiceDep
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatRequest,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SourcesEvent,
)

router = APIRouter()

_EVENT_NAMES: dict[type[ChatStreamEvent], str] = {
    RunStartedEvent: "run_started",
    SourcesEvent: "sources",
    AnswerDeltaEvent: "answer_delta",
    DoneEvent: "done",
    ErrorEvent: "error",
}


async def _to_sse(events: AsyncIterator[ChatStreamEvent]) -> AsyncIterator[dict[str, str]]:
    """Serialize typed events to sse-starlette's dict shape.

    The event name is the discriminator; the payload is model-serialized JSON
    so clients parse one consistent shape per event.
    """
    async for event in events:
        yield {"event": _EVENT_NAMES[type(event)], "data": event.model_dump_json()}


@router.post("", response_class=EventSourceResponse, response_model=None)
async def chat(payload: ChatRequest, service: ChatServiceDep) -> EventSourceResponse:
    """Stream a knowledge-grounded answer as SSE."""
    return EventSourceResponse(_to_sse(service.ask(payload.question, limit=payload.limit)))
