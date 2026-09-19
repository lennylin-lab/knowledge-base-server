# Stream draft output token-by-token via draft_delta events

## Goal

Make `POST /operations/draft` surface the model's generation as it happens: forward the streamed structured-output JSON fragments as incremental `draft_delta` SSE events while generation is in flight, keeping the existing terminal contract (`draft` + `done`, or single `error`) unchanged.

## Background / Confirmed facts

- Today the draft stream is `run_started → draft → done | error` with the structured result atomic; users stare at a blank UI for the entire multi-minute generation (chat streams, draft does not — inconsistent UX).
- The model streams a structured-output tool call; raw tool-arg JSON arrives in small fragments (verified live against the upstream: `arguments` deltas like `{"`, `title`, `":"`, `春`…).
- pydantic-ai `run_stream` is already in place (task 09-19-draft-run-stream, commit 0c7bb29); the deltas are visible as `PartDeltaEvent` / `ToolCallPartDelta.args_delta` in the underlying stream.
- The gateway's 60s whole-request deadline still applies to streamed requests (task 09-19-draft-run-stream findings); that fix is a separate gateway-side task. Deltas improve perceived latency but do NOT lift the ceiling.
- Frontend integration issue knowledge-base-flutter#4 documents the OLD atomic contract and must be updated as part of this task.

## Requirements

- New additive event `draft_delta` (e.g. `DraftDeltaEvent`: run_id + raw `delta` string fragment) emitted for every tool-arg fragment the model streams, in order, before the terminal events.
- Terminal contract unchanged: validated final result still arrives as the existing flat `draft` event, then `done`; failure still exactly one terminal `error` (no `done`). Persistence timing rule unchanged (terminal state committed before terminal events).
- Deltas are raw JSON fragments (client concatenates); server does NOT parse partial JSON.
- Existing streams (chat ask, writing suggest, summarize, associations) byte-identical; inspect/resume/apply untouched.
- Tests: fake streams the output tool args in multiple fragments; assert draft_delta order, that concatenation of deltas equals the final tool-arg JSON, terminal contract, and failure-after-deltas behavior.

## Acceptance Criteria

- [ ] Draft stream wire order: `run_started` → `draft_delta`* → `draft` → `done`, or `run_started` → `draft_delta`* → single `error`.
- [ ] Delta concatenation equals the final structured output's JSON exactly (no loss/duplication).
- [ ] Failure after deltas commits `failed` (resumable) and emits the single terminal `error`.
- [ ] ruff + strict mypy + full suite pass; other SSE streams untouched.
- [ ] knowledge-base-flutter#4 updated to the new contract.

## Out of scope

- Gateway 60s deadline fix (separate gateway-side task).
- Server-side partial-JSON parsing / field-wise structured deltas.
