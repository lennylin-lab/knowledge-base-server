# Design: token-level streaming for summary / association agents

## Scope

Service layer (`services/agents.py`), event schemas (`schemas/agent_stream.py`),
endpoint name maps (`api/v1/endpoints/documents.py`). Agent modules and the
SSE serializer stay untouched. No persistence, no repository changes.

## New wire events (additive)

```python
class SummaryDeltaEvent(BaseModel):
    """Verbatim fragment of the streamed final summary pass."""
    run_id: str
    text: str

class AssociationItemEvent(BaseModel):
    """One complete curated association, streamed as soon as it is confirmed."""
    run_id: str
    position: int          # 1-based, order of the final list
    document_id: UUID
    title: str
    tags: list[str]
    reason: str
    signal: str
```

Both are appended to the `AgentStreamEvent` union and the documents endpoint's
`_EVENT_NAMES` (`summary_delta`, `association_item`). The final
`SummaryResultEvent` / `AssociationsResultEvent` are unchanged and still
emitted — a client that ignores deltas/items keeps today's contract exactly.

## SummarizeService

- `_run_pass` (non-streaming) stays for map passes.
- New `_run_streaming_pass(deps, prompt, max_tokens, run_id)`:
  `async with agent.run_stream(...)` + `result.stream_text(delta=True,
  debounce_by=None)`, yields `SummaryDeltaEvent(text=delta)` per non-empty
  delta, returns `(full_text, usage)` at the end. The final text is the
  accumulated stream — no second model call, and the `summary` result event
  carries exactly the concatenation.
- Single-chunk path: keep `summary_progress(map_pass)` → stream deltas →
  `summary_progress(reduce_pass)` (grammar unchanged, no separate model call
  behind it) → result → done.
- Multi-chunk path: map passes unchanged; only the reduce pass streams deltas.
- Cache hit / unchanged paths emit no deltas (never ran the model).

## AssociationService

- Replace `agent.run` with `async with agent.run_stream(prompt, deps=deps)`
  and iterate `result.stream_output(debounce_by=None)`: pydantic-ai yields
  partially-validated `AssociationsOutput` snapshots as tokens arrive.
- **Hold-back rule** (correctness): pydantic partial validation can expose a
  trailing, still-truncating list element. Item at index `i` is emitted only
  once a strictly later index exists in a snapshot; the remaining held-back
  items are emitted after the stream completes from the fully-validated
  `result.output`. Guarantees every streamed item is complete and final.
- Emission joins each pick onto candidate metadata (existing
  `_join_selections` logic, refactored to expose a per-item join + drop
  counting) BEFORE streaming, so hallucinated/duplicate ids are never
  streamed. Position = final-list order.
- The final `associations` result event is built from the same joined list —
  streamed items and final payload are identical by construction.

## Error discipline

Unchanged: `_summarize_events` / `_association_events` still only raise before
first yield; a failure mid-stream (e.g. provider drops the connection during
`run_stream`) is caught by the same `except` in the stream wrapper and surfaces
as the terminal `error` event. Deltas/items already sent are simply abandoned
(front-end already tolerates discarded partials per `DraftDeltaEvent`
precedent). Sync drain wrappers are untouched.

## Testing

Offline via pydantic-ai `FunctionModel`/scripted streams (project convention):
- summarize single- and multi-chunk: delta concatenation == result text.
- association scripted stream emitting snapshots with growing lists: item
  events arrive incrementally, match final payload, hallucinated ids never
  streamed.
- cache-hit paths emit no delta/item events.
- failure after first yield → terminal `error`, no result event.
