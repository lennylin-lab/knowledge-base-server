"""Bridge pydantic-ai's tool lifecycle events onto the chat SSE vocabulary.

`Agent.run_stream(event_stream_handler=...)` delivers `FunctionToolCallEvent`
/ `FunctionToolResultEvent` for every tool the agent executes while
`stream_text` keeps yielding answer text — the handler here buffers typed
`ToolCallStartedEvent` / `ToolCallFinishedEvent` onto per-phase queues that
the orchestrating service drains alongside its `sources` batches. Started
and finished events live on SEPARATE queues on purpose: a tool's `sources`
flush (via `SourceCollector.on_append`) happens between its start and its
result, so draining started → sources → finished reproduces the honest
progress order (`tool_call_started` → `sources` → `tool_call_finished`).

MCP soft failures never raise: the wrapper returns a
`"tool {name} failed: {error_class}"` string to the model (`mcp/tools.py`),
detected here by prefix and mapped to `tool_call_finished.status="failed"` —
the run itself still reaches `done`.

Layering: this module imports pydantic-ai and `schemas/` only — no FastAPI,
no agent modules — so both `ChatService` and `WritingService` can share it.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    RetryPromptPart,
)

from app.schemas.chat import ToolCallFinishedEvent, ToolCallStartedEvent

# The retrieval tool whose started-event args gain the run's `limit` (from
# deps) so clients can display the effective page size without re-parsing.
_SEARCH_TOOL_NAME = "search_knowledge"


def _parse_tool_args(part: Any) -> dict[str, Any]:
    """Best-effort decode of a tool-call part's `args` into a JSON object.

    pydantic-ai stores model-supplied arguments as a JSON string or an
    already-parsed dict (or nothing, for a malformed call). Any decode
    failure degrades to `{}` — the args are display-only progress data, and
    a broken payload must never break the stream."""
    args = getattr(part, "args", None)
    if isinstance(args, dict):
        return dict(args)
    if isinstance(args, str):
        try:
            decoded = json.loads(args)
        except ValueError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _is_tool_failure(event: FunctionToolResultEvent) -> bool:
    """Whether the tool call degraded rather than returned real content.

    Two shapes count: a `RetryPromptPart` (the framework asked the model to
    retry — validation failure or a `ModelRetry`), and an MCP wrapper's
    error string (`"tool {name} failed: {error_class}"`, from
    `mcp/tools.py`) returned to the model as ordinary content."""
    if isinstance(event.part, RetryPromptPart):
        return True
    content = event.part.content
    return isinstance(content, str) and content.startswith(f"tool {event.part.tool_name} failed:")


def _deps_limit(deps: Any) -> int | None:
    """The run's retrieval page size from deps, when present.

    `ChatDeps` and `WritingDeps` both carry `limit`; the bridge duck-types
    rather than importing agent modules (services own that import)."""
    limit = getattr(deps, "limit", None)
    return limit if isinstance(limit, int) else None


@dataclass
class RunEventBridge:
    """Per-run buffer mapping agent tool events onto typed SSE events.

    One bridge per run (never per service — all state here is request
    scoped). The owning service drains `pending_started` and
    `pending_finished` around its `sources` batches; see the module
    docstring for why the queues are separate."""

    pending_started: list[ToolCallStartedEvent] = field(default_factory=list)
    pending_finished: list[ToolCallFinishedEvent] = field(default_factory=list)
    _started_at: dict[str, float] = field(default_factory=dict)

    async def on_agent_event(
        self, ctx: RunContext[Any], stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        """The `event_stream_handler` sink: buffer tool lifecycle events.

        Non-tool events (part starts/deltas, thinking) are ignored — answer
        text keeps flowing through `stream_text` in the owning service."""
        async for event in stream:
            match event:
                case FunctionToolCallEvent():
                    self._started_at[event.tool_call_id] = time.perf_counter()
                    args = _parse_tool_args(event.part)
                    if event.part.tool_name == _SEARCH_TOOL_NAME:
                        limit = _deps_limit(ctx.deps)
                        if limit is not None:
                            args["limit"] = limit
                    self.pending_started.append(
                        ToolCallStartedEvent(
                            call_id=event.tool_call_id,
                            tool_name=event.part.tool_name,
                            args=args,
                        )
                    )
                case FunctionToolResultEvent():
                    started_at = self._started_at.pop(event.tool_call_id, None)
                    elapsed = time.perf_counter() - started_at if started_at is not None else 0.0
                    self.pending_finished.append(
                        ToolCallFinishedEvent(
                            call_id=event.tool_call_id,
                            # A retry prompt can carry no tool name; the wire
                            # field stays non-null with an empty fallback.
                            tool_name=event.part.tool_name or "",
                            status="failed" if _is_tool_failure(event) else "success",
                            latency_ms=round(elapsed * 1000, 2),
                        )
                    )
                case _:
                    pass

    def drain_started(self) -> list[ToolCallStartedEvent]:
        """Take all pending started events, leaving the buffer empty."""
        events = list(self.pending_started)
        self.pending_started.clear()
        return events

    def drain_finished(self) -> list[ToolCallFinishedEvent]:
        """Take all pending finished events, leaving the buffer empty."""
        events = list(self.pending_finished)
        self.pending_finished.clear()
        return events
