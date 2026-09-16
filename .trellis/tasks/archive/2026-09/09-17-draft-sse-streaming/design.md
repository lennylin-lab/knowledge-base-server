# Technical Design

Rework `AgentOperationService.draft_document` from a coroutine returning the
operation into an async generator yielding typed SSE events, keeping the
existing persistence timing: commit `running` first, run the structured-output
model call, commit `completed`/`failed` BEFORE yielding the terminal event so
a client that stops reading never sees success for unpersisted work.

Event vocabulary (additive to the canonical SSE table in
`.trellis/spec/backend/error-handling.md`): reuse `RunStartedEvent`,
tool-call events (when retrieval tools fire), `AgentDoneEvent`, and chat's
`ErrorEvent` shape for the terminal error dialect; add one new
`OperationDraftEvent` (operation_id, state, draft content) emitted right
before `AgentDoneEvent`. Pre-stream failures (dep missing → 503, document
not found → 404) resolve before the `EventSourceResponse` is constructed
(priming pattern). Mid-stream provider failure commits `failed`, then yields
exactly one `error` event and closes.

The endpoint maps the generator through the same sse-starlette plumbing the
writing/summarize endpoints use; `run_id` binding follows the suggest stream
discipline. Non-LLM endpoints and the suggest stream are untouched.
