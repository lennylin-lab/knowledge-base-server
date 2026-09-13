# Design — Chat SSE progress events

## Architecture

```
Client  ←── SSE (to_sse_event) ──  api/v1/endpoints/chat.py
                                         ↑
                              services/chat.py::ask
                              services/agents.py::WritingService.suggest
                                         ↑
                    ┌────────────────────┴────────────────────┐
                    │  Event mapper (new, services layer)      │
                    │  - emit StatusEvent, QueryRewrittenEvent │
                    │  - drain tool-call queue from handler    │
                    └────────────────────┬────────────────────┘
                                         ↑
              run_stream(..., event_stream_handler=on_agent_event)
                                         ↑
                              pydantic-ai Agent (qa / writing)
                    SourceCollector.on_append → SourcesEvent (unchanged)
```

Layering unchanged: `agents/` has no SSE imports. Tool-call observation
uses pydantic-ai's public `event_stream_handler` hook; phase/rewrite events
are emitted directly from the service orchestration code.

## New wire types (`schemas/chat.py`)

```python
StatusPhase = Literal["rewriting_query", "generating"]

class StatusEvent(BaseModel):
    phase: StatusPhase

class ToolCallStartedEvent(BaseModel):
    call_id: str
    tool_name: str
    args: dict[str, Any]

class ToolCallFinishedEvent(BaseModel):
    call_id: str
    tool_name: str
    status: Literal["success", "failed"]
    latency_ms: float

class QueryRewrittenEvent(BaseModel):
    original: str
    rewritten: str
    applied: bool
    changed: bool
```

Extend `ChatStreamEvent` union and `_EVENT_NAMES`:

| Event name | Type |
|------------|------|
| `status` | `StatusEvent` |
| `tool_call_started` | `ToolCallStartedEvent` |
| `tool_call_finished` | `ToolCallFinishedEvent` |
| `query_rewritten` | `QueryRewrittenEvent` |

## Event order contract (chat)

Canonical order after this change:

```
run_started
→ sources*                    # carried batch first when carry-forward on
→ status(rewriting_query)?    # only when rewrite will run
→ query_rewritten?            # only when changed
→ [agent loop]
    tool_call_started
    → sources                 # on_append flush (unchanged)
    → tool_call_finished
    → (repeat per tool call)
    → status(generating)?     # once, before first answer_delta
    → answer_delta*
→ sources*                    # final drain (unchanged)
→ done | error
```

**Hard rules:**

1. Carried `sources` precedes all progress events except nothing may precede
   `run_started`.
2. `sources` still precedes `answer_delta` that cites it (existing drain
   hook preserved).
3. `tool_call_started` precedes its matching `tool_call_finished`.
4. `tool_call_finished` for `search_knowledge` precedes or coincides with
   the `sources` flush for that call (sources flush happens in
   `on_append`; finished event comes from pydantic-ai after tool returns —
   order: started → sources → finished is acceptable and honest).

## Tool-call observation

### Handler pattern

Introduce a small helper in `services/` (e.g. `stream_bridge.py` or methods
on a `RunEventBridge` dataclass) used by both `ChatService` and
`WritingService`:

```python
@dataclass
class RunEventBridge:
    pending_tool_events: list[ChatStreamEvent]
    tool_started_at: dict[str, float]

    async def on_agent_event(
        self, ctx: RunContext[Any], stream: AsyncIterable[AgentStreamEvent]
    ) -> None:
        async for event in stream:
            match event:
                case FunctionToolCallEvent():
                    self.pending_tool_events.append(
                        ToolCallStartedEvent(
                            call_id=event.tool_call_id,
                            tool_name=event.part.tool_name,
                            args=_parse_tool_args(event.part),
                        )
                    )
                case FunctionToolResultEvent():
                    status = "failed" if _is_failed_result(event) else "success"
                    ...
                    self.pending_tool_events.append(ToolCallFinishedEvent(...))
```

The main `ask` loop pattern becomes:

```python
bridge = RunEventBridge()
async with self._agent.run_stream(
    ...,
    event_stream_handler=bridge.on_agent_event,
) as result:
    async for delta in result.stream_text(delta=True, debounce_by=None):
        for event in _drain(bridge.pending_tool_events):
            yield event
        for batch in _drain(pending_sources):
            yield SourcesEvent(items=batch)
        if delta:
            if not generating_sent:
                yield StatusEvent(phase="generating")
                generating_sent = True
            yield AnswerDeltaEvent(text=delta)
    # final drains unchanged
```

`_drain` already exists for sources; reuse the same list-drain idiom.

### MCP failure detection

MCP wrapper returns `"tool {name} failed: {error_class}"` strings
(`mcp/tools.py:96`). `tool_call_finished.status = "failed"` when the result
content starts with that prefix; otherwise `success`. No new exception path.

### `search_knowledge` args enrichment

The handler sees JSON args from the model (`{"query": "..."}`). Prefer
passing through parsed args as-is — the model's query is what retrieval uses.
Include `limit` from deps in started event args for client display:
`{"query": "...", "limit": 8}`.

## Rewrite transparency

Refactor `_rewrite_query` to return a small result dataclass:

```python
@dataclass(frozen=True)
class RewriteOutcome:
    query: str           # resolved run prompt (rewritten or original)
    event: QueryRewrittenEvent | None  # None when no wire event warranted
```

Emit logic in `ask`:

```python
if self._rewrite_agent is not None and turn and turn.history:
    yield StatusEvent(phase="rewriting_query")
outcome = await self._resolve_rewrite(question, turn.history if turn else [])
if outcome.event is not None:
    yield outcome.event
retrieval_question = outcome.query
```

Logging stays length-only; full strings go to SSE only (user's own data).

## Writing service

`WritingService.suggest` adopts the same `RunEventBridge` + drain loop.
No rewrite/status(`rewriting_query`) events. `status(generating)` before
first `answer_delta` optional but recommended for consistency.

## Compatibility & migration

- **Backward compatible**: additive events only.
- **Frontend**: should ignore unknown `event` names (already best practice).
- **Spec updates** (Phase 3.3, not blocking start): extend
  `error-handling.md` canonical SSE list and add a chat-guidelines scenario
  for progress-event ordering.

## Risks

| Risk | Mitigation |
|------|------------|
| `event_stream_handler` + `stream_text` race | Drain bridge queue in the same loop iteration as sources drain; test order assertions |
| Event spam on multi-tool runs | Acceptable for MVP; each tool call is user-visible progress |
| Test fakes only script `stream_function` | Extend `FakeQaModel` or add handler-aware assertions; verify handler fires with FunctionModel |
| Writing regression | Mirror drain loop; add `test_writing_api` event-name check |

## Rollback

Revert schema additions + service loop changes. No migration. Old clients
unaffected either direction.
