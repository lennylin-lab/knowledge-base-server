# Chat SSE progress events

## Goal

Give the frontend observable progress during QA streaming so users are not
staring at a spinner while silent work happens (query rewrite, tool calls,
retrieval). Add additive SSE events for **phase status**, **tool-call
lifecycle**, and **query rewrite transparency** — without changing existing
event semantics or citation ordering.

## Background

Current chat SSE (`services/chat.py::ask`) emits five event types:
`run_started` → `sources`* → `answer_delta`* → `done` | `error`
(`schemas/chat.py`, wired in `api/v1/endpoints/chat.py`). The service uses
`result.stream_text()` only, so tool-call and rewrite phases are invisible
to clients.

Known silent gaps (source-verified):

1. **After `run_started`**, `_rewrite_query` runs a non-streaming LLM call
   when history exists and rewrite is enabled (`services/chat.py:490-494`).
   No SSE until rewrite completes.
2. **During agent run**, pydantic-ai invokes tools (`search_knowledge`, MCP
   wrappers) but the service only flushes `sources` when a tool **returns**
   (`SourceCollector.on_append`). No signal while the model decides or the
   tool executes.
3. **Rewrite outcome** is logged server-side (`query_rewrite` with lengths
   only) but never exposed on the wire — follow-up turns can show a
   different retrieval query with no client-visible explanation.

Writing (`services/agents.py::WritingService.suggest`) shares the same SSE
vocabulary via `to_sse_event` but has no rewrite step; tool-call progress
events should still apply there.

## Requirements

### R1 — Phase status events

- Add a `status` SSE event with a typed `phase` field covering the silent
  work the service already performs.
- Phases (closed enum for MVP):
  - `rewriting_query` — emitted immediately before `_rewrite_query` when
    rewrite will run (rewrite agent wired **and** non-empty history).
  - `generating` — emitted once when the first `answer_delta` is about to
    be yielded (marks transition from tool/retrieval work to visible answer
    streaming).
- `status` events are **informational only**; they never replace terminal
  `done` / `error` semantics.

### R2 — Tool-call lifecycle events

- Add `tool_call_started` and `tool_call_finished` SSE events mapped from
  pydantic-ai's `FunctionToolCallEvent` / `FunctionToolResultEvent` via
  `run_stream(..., event_stream_handler=...)`.
- Each started event carries: `call_id`, `tool_name`, `args` (JSON object).
- Each finished event carries: `call_id`, `tool_name`, `status`
  (`success` | `failed`), `latency_ms`.
- For `search_knowledge`, `args` includes the retrieval `query` and `limit`
  (from deps — not re-parsed from JSON args).
- For MCP tools (`mcp_*`), `args` is the kwargs dict the model sent; no
  truncation beyond existing content-length norms (user-initiated run).
- MCP soft failures (error string returned to model, run continues) map to
  `status: failed` on `tool_call_finished`; the stream still reaches `done`.
- Existing `sources` events remain the authoritative retrieval **result**
  batch; tool-call events describe **progress**, not hit payloads.

### R3 — Query rewrite transparency

- Add `query_rewritten` SSE event when rewrite runs and produces a
  non-empty output that differs from the original question (byte-level
  `applied: true`, `changed: true`).
- Payload: `original`, `rewritten`, `applied: bool`, `changed: bool`.
- Emitted after rewrite completes, **before** the agent `run_stream` starts
  (and after any `status: rewriting_query`).
- When rewrite is skipped (first turn, stateless, flag off, no rewrite
  agent) or degrades (failure, empty output, unchanged text): **no event**
  — same as today.
- Persisted message and `message_history` keep the **original** question
  (existing R3 from rewrite task — unchanged).

### R4 — Contract preservation

- **Citation order invariant** unchanged: every `sources` batch still
  precedes the `answer_delta` text that may cite it
  (`services/chat.py:507-515`).
- **Carried-sources-forward** invariant unchanged: carried batch remains
  the first `sources` after `run_started`; new events must not interleave
  in a way that makes clients think carried hits are fresh retrieval.
- Existing five event types and payloads are backward compatible; clients
  that ignore unknown events keep working.
- `agents/` stays stream-free; event mapping lives in `services/`.
- After the first event, the generator still never raises — failures become
  terminal `error` (error-handling spec).

### R5 — Shared vocabulary

- New events are added to `ChatStreamEvent`, `_EVENT_NAMES` in
  `api/v1/endpoints/chat.py`, and apply to both chat and writing endpoints
  that share `to_sse_event`.

## Acceptance Criteria

- [ ] AC1: Follow-up turn with rewrite enabled emits, in order:
      `run_started` → `status(rewriting_query)` → `query_rewritten` (when
      changed) → `tool_call_started` → `sources` → `tool_call_finished` →
      `answer_delta`* → `done`.
- [ ] AC2: First turn / stateless / rewrite disabled emits **no**
      `status(rewriting_query)` and **no** `query_rewritten`; event
      sequence is otherwise identical to pre-change behavior modulo new
      tool-call events when retrieval runs.
- [ ] AC3: Rewrite failure or empty output: no `query_rewritten`; stream
      reaches `done` (existing degrade behavior preserved).
- [ ] AC4: MCP tool soft failure yields `tool_call_finished` with
      `status: failed` and the run still ends with `done`.
- [ ] AC5: Carried-sources follow-up: carried `sources` batch still first
      after `run_started`; progress events do not appear between
      `run_started` and the carried batch.
- [ ] AC6: Writing `/suggest` emits `tool_call_started` / `tool_call_finished`
      when the model calls `search_knowledge`; no rewrite events.
- [ ] AC7: API wire test (`test_chat_api.py`) parses new event names and
      payloads; service-order tests cover rewrite + tool-call interleaving.
- [ ] AC8: `ruff`, strict `mypy`, full test suite green; no question or
      tool-return text in structlog (lengths/classes only — logging-guidelines).

## Out of scope

- `thinking_delta` / reasoning-model chain-of-thought exposure (explicitly
  deferred — item 3 from brainstorm).
- `heartbeat` / `ping` keepalive events.
- Token usage on the wire (stays in logs + future task).
- Frontend UI implementation (separate repo).
- Changing `SearchHit`, `SourcesEvent`, `DoneEvent`, or `ErrorEvent` shapes.
- Persisting rewrite or tool-call metadata to the database.

## Decisions (resolved)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Feature flag | None | Additive wire events; no behavior change for ignoring clients |
| Tool args on wire | Full for KB/MCP | User's own session data; enables "searching for X" UI |
| `status` phases | `rewriting_query`, `generating` only | Covers main silent gaps without event spam |
| Rewrite event gate | Only when `changed` | Avoid noise when rewrite returns same text |
| Writing scope | Tool-call events only | No rewrite step in writing service |
