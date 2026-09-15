"""Writing endpoint — thin: parse, one service call, map events to SSE.

The router never iterates the stream itself; it hands the event generator to
`sse-starlette` through the shared chat-event serializer. Failures inside the
generator have already been converted to terminal `error` events by the
service.
"""

from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.api.deps import ChatScope, WritingServiceDep
from app.api.v1.endpoints.chat import to_sse
from app.schemas.writing import WritingRequest

router = APIRouter()


@router.post("/suggest", response_class=EventSourceResponse, response_model=None)
async def suggest(
    payload: WritingRequest, service: WritingServiceDep, tenant: ChatScope
) -> EventSourceResponse:
    """Stream writing assistance for a draft as SSE."""
    return EventSourceResponse(
        to_sse(
            service.suggest(
                payload.draft, payload.instruction, tenant_id=tenant, limit=payload.limit
            )
        )
    )
