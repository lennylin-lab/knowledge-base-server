# Implement: token-level streaming for summary / association agents

## Checklist

1. [ ] `schemas/agent_stream.py`: add `SummaryDeltaEvent`, `AssociationItemEvent`;
       extend `AgentStreamEvent` union; update module docstring contract order.
2. [ ] `api/v1/endpoints/documents.py`: add `"summary_delta"` /
       `"association_item"` to `_EVENT_NAMES`.
3. [ ] `services/agents.py` — SummarizeService:
   - add `_run_streaming_pass` (run_stream + stream_text(delta=True));
   - single-chunk and reduce pass switch to it; map passes unchanged;
   - accumulated text feeds the result event / cache put.
4. [ ] `services/agents.py` — AssociationService:
   - switch to `run_stream` + `stream_output(debounce_by=None)`;
   - per-item join with hold-back rule (emit index i when a later index is
     seen; flush held-back items from `result.output` after the stream);
   - final result event from the same joined list.
5. [ ] Tests (offline, FunctionModel/scripted):
   - summarize delta-concatenation == final summary (single + multi chunk);
   - association incremental item events, held-back flush, no hallucinated ids;
   - cache-hit emits no deltas/items; error mid-stream → terminal `error`.
6. [ ] Update `tests` fixtures/stubs if FunctionModel needs a streaming
       response shape (pydantic-ai 1.x supports async-defined stream functions).

## Validation commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

## Rollback

Single commit; revert restores the atomic-event behavior (wire change is
additive, so no contract rollback needed for clients).

## Review gates

- After step 4: re-read services/agents.py generator for the "nothing escapes
  after first yield" invariant.
- After step 5: full pytest, then trellis-check.
