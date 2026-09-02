"""Chat endpoint — thin: parse, one service call, map events to SSE.

The router hands the event generator to `sse-starlette` in wire format;
failures inside the generator have already been converted to terminal
`error` events by the service. The one router-side iteration is the priming
pull in `chat`: sse-starlette commits its 200 status before it starts
draining the generator, so anything that must fail with a clean envelope
(a missing chat session → 404) has to raise before the response is built.
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


def to_sse_event(event: ChatStreamEvent) -> dict[str, str]:
    """Serialize one typed event to sse-starlette's dict shape.

    The event name is the discriminator; the payload is model-serialized JSON
    so clients parse one consistent shape per event. Public because every
    endpoint streaming the chat event vocabulary shares it (writing today):
    one wire format, one place.
    """
    return {"event": _EVENT_NAMES[type(event)], "data": event.model_dump_json()}


async def to_sse(events: AsyncIterator[ChatStreamEvent]) -> AsyncIterator[dict[str, str]]:
    """Serialize a typed event stream to sse-starlette's dict shape."""
    async for event in events:
        yield to_sse_event(event)


async def _primed_sse(
    first: ChatStreamEvent, rest: AsyncIterator[ChatStreamEvent]
) -> AsyncIterator[dict[str, str]]:
    """`to_sse` with the first event already materialized (see `chat`)."""
    yield to_sse_event(first)
    async for event in rest:
        yield to_sse_event(event)


@router.post("", response_class=EventSourceResponse, response_model=None)
async def chat(payload: ChatRequest, service: ChatServiceDep) -> EventSourceResponse:
    """Stream a knowledge-grounded answer as SSE."""
    events = service.ask(payload.question, limit=payload.limit, session_id=payload.session_id)
    # Prime the generator through its first event BEFORE the SSE response is
    # constructed: sse-starlette sends `http.response.start` before pulling
    # the first body item, so an eager pre-stream failure (session 404) must
    # raise here to become a JSON envelope — inside the response it could
    # only ever be a broken stream.
    first = await events.__anext__()
    return EventSourceResponse(_primed_sse(first, events))
