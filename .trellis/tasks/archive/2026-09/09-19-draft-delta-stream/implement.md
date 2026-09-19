# Implementation Plan

1. Add `DraftDeltaEvent` (run_id, delta) to `schemas/agent_stream.py`; wire name `draft_delta` in the operations endpoint map.
2. Rework `_draft_events` to `agent.iter()`: forward output-tool `args_delta` fragments as `draft_delta`; take final validated output from the run result; persistence timing and terminal contract unchanged.
3. Update `tests/fakes.py` `scripted_draft_model` stream_function to emit args in ≥3 fragments; extend `tests/test_operations_api.py`: delta order + concatenation invariant, failure-after-deltas (single error, failed+resumable), existing assertions updated only where the new events are inserted.
4. Validation: `uv run ruff check .`, `uv run mypy src`, focused tests, full `KB_OIDC_ISSUER= uv run pytest`.
5. Update knowledge-base-flutter#4 body: new event order (run_started → draft_delta* → draft → done | error), delta semantics (raw JSON fragments, discard partials on error).

Review gates: exactly one terminal event; completed/failed committed before terminal event; other streams byte-identical; no partial-JSON parsing server-side.
