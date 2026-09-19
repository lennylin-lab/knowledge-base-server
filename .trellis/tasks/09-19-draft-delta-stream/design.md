# Technical Design

`_draft_events` switches from `agent.run_stream` to `agent.iter()` so raw
stream events are reachable: consume `PartDeltaEvent` nodes, and for output
tool-call parts forward `args_delta` strings as `DraftDeltaEvent(run_id,
delta)` (additive schema in `schemas/agent_stream.py`; wire name
`draft_delta`). Retrieval-tool calls are skipped — only the final output
tool's fragments stream (guard by tool name == the registered output tool).
After the graph finishes, the final validated `DraftOutput` is taken from the
run result; the rest of the generator (persist completed → yield `draft` →
`done`) is byte-identical to today.

Failure semantics: any exception mid-stream (including provider cut at the
gateway deadline) commits `failed` and the wrapper emits the single terminal
`error` — deltas already sent stay sent; the client discards partials on
`error` (documented in the issue update). Concatenation invariant (deltas ==
final tool-arg JSON) holds because we forward every args fragment verbatim
and exactly once; the test fake streams args in ≥3 fragments to pin this.

The operations endpoint maps the new event through its existing event-name
map (`draft_delta`); nothing else in the SSE plumbing changes.
