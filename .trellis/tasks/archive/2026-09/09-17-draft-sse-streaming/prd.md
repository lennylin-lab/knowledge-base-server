# Draft endpoint SSE streaming

## Goal

Convert `POST /operations/draft` from a synchronous JSON response into an SSE event stream, so every LLM-producing endpoint in the project streams over SSE instead of blocking on JSON.

## Background / Confirmed facts

- `POST /operations/draft` (task 09-13-agent-document-persistence, branch `feat/agent-document-persistence`) is the only LLM-calling endpoint that returns JSON; it runs `build_draft_agent` synchronously and returns the completed operation.
- All other LLM endpoints (chat `ask`, writing `suggest`, summarize, associations) stream typed SSE events; the canonical vocabulary is the SSE table in `.trellis/spec/backend/error-handling.md`.
- Draft persistence state machine: `running` row committed before the model call → `completed` (structured draft) on success / `failed` (+ error details) on provider failure → resume path for interrupted/failed.
- Streaming rules: priming pattern for pre-stream failures; once SSE 200 is sent, mid-stream failure emits exactly one terminal `error` event then closes; nothing escapes the stream once the first event is yielded.

## Requirements

- `POST /operations/draft` responds as an SSE event stream using the existing event vocabulary (run-started / tool events / done / error) plus a draft result event carrying the structured `DraftContent`.
- Operation state transitions keep their current timing semantics: `running` persisted before the model call; terminal state (`completed`/`failed`) persisted before the corresponding terminal stream event is emitted.
- Pre-stream failures (missing API key, document not found) stay HTTP error responses via the priming pattern.
- Mid-stream provider failure: emit `error` event, operation left `failed` and resumable — same durability as today.
- No silent auto-publish; the stream ends with the draft, never an apply.
- Event wire shapes stay additive; chat/writing/summarize/associations streams remain byte-identical.

## Acceptance Criteria

- [ ] `POST /operations/draft` returns `text/event-stream` with the canonical event order (run-started → [tool events] → draft result → done), or exactly one terminal `error`.
- [ ] A completed stream always leaves the operation `completed` with structured draft persisted; an errored stream leaves it `failed` with error details, inspectable/resumable.
- [ ] Pre-stream failures remain HTTP status-code errors (priming pattern), never a 200 stream.
- [ ] Existing streams (chat ask, writing suggest, summarize, associations) produce byte-identical event sequences (covered by existing tests passing unchanged).
- [ ] New endpoint contract tests cover success, provider failure, and pre-stream failure ordering.

## Out of scope

- Streaming token deltas of the draft (structured output is atomic by design).
- Changes to inspect/resume/apply endpoints (non-LLM, stay JSON).
- Frontend consumption changes.

## Key decisions

- Reuse chat's SSE event vocabulary and error dialect (one `error` event shape); add a single additive draft-result event rather than a new dialect.
