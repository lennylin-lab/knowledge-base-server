"""Chat endpoint — thin: parse, one service call, map events to SSE.

The router hands the event generator to `sse-starlette` in wire format;
failures inside the generator have already been converted to terminal
`error` events by the service. The one router-side iteration is the priming
pull in `chat`: sse-starlette commits its 200 status before it starts
draining the generator, so anything that must fail with a clean envelope
(a missing chat session → 404) has to raise before the response is built.
"""

from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ChatScope, ChatServiceDep
from app.api.v1.endpoints.chat_sse import primed_sse
from app.schemas.chat import ChatRequest

router = APIRouter()


@router.post("", response_class=EventSourceResponse, response_model=None)
async def chat(
    payload: ChatRequest, service: ChatServiceDep, tenant: ChatScope
) -> EventSourceResponse:
    """Stream a knowledge-grounded answer as SSE."""
    events = service.ask(
        payload.question, limit=payload.limit, session_id=payload.session_id, tenant_id=tenant
    )
    # Prime the generator through its first event BEFORE the SSE response is
    # constructed: sse-starlette sends `http.response.start` before pulling
    # the first body item, so an eager pre-stream failure (session 404) must
    # raise here to become a JSON envelope — inside the response it could
    # only ever be a broken stream.
    first = await events.__anext__()
    return EventSourceResponse(primed_sse(first, events))
