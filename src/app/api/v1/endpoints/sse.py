"""Shared SSE wire serializer: one typed-event → SSE-frame implementation.

Every streaming endpoint owns a `dict[type[Event], str]` name map (the event
name is the wire discriminator) and delegates the actual serialization here —
one wire format, one place. The typed events themselves stay in `schemas/`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from pydantic import BaseModel


def to_sse_event[E: BaseModel](event: E, event_names: Mapping[type[E], str]) -> dict[str, str]:
    """Serialize one typed event to sse-starlette's dict shape.

    The event name comes from the caller's name map (keyed by concrete type);
    the payload is model-serialized JSON so clients parse one consistent shape
    per event.
    """
    return {"event": event_names[type(event)], "data": event.model_dump_json()}


async def to_sse[E: BaseModel](
    events: AsyncIterator[E], event_names: Mapping[type[E], str]
) -> AsyncIterator[dict[str, str]]:
    """Serialize a typed event stream to sse-starlette's dict shape."""
    async for event in events:
        yield to_sse_event(event, event_names)


async def primed_sse[E: BaseModel](
    first: E, rest: AsyncIterator[E], event_names: Mapping[type[E], str]
) -> AsyncIterator[dict[str, str]]:
    """`to_sse` with the first event already materialized.

    Endpoints that must fail with a clean HTTP envelope before the stream
    starts pull the first event themselves (the priming pattern) and hand it
    here alongside the exhausted-once iterator.
    """
    yield to_sse_event(first, event_names)
    async for event in rest:
        yield to_sse_event(event, event_names)
