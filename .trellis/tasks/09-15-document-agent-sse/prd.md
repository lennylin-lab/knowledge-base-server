# Document summary and associations SSE endpoints

## Goal

Stream document summary and association endpoint results over SSE while preserving existing result, error, and tenant contracts.

## Background

The document router currently exposes two synchronous agent endpoints:

- `POST /api/v1/documents/{document_id}/summary` returns `SummaryResult`.
- `POST /api/v1/documents/{document_id}/associations` returns
  `AssociationsResult`.

Both services run provider calls before returning. Summarization may execute
sequential map/reduce passes (`services/agents.py::SummarizeService`), while
association gathers deterministic candidates and then performs one structured
LLM call (`services/agents.py::AssociationService`).

The project already uses `sse-starlette` and `EventSourceResponse` for chat and
writing. Their shared conventions include typed event payloads, a stable SSE
serializer, tenant-scoped dependencies, and terminal `error` events after a
stream has started (`api/v1/endpoints/chat.py`, `schemas/chat.py`, and the
backend error-handling spec).

## Confirmed constraints

- Keep both existing HTTP methods and paths unless a later product decision
  explicitly changes them.
- Keep document lookup, soft-delete behavior, tenant isolation, cache behavior,
  and provider error taxonomy unchanged.
- Keep `SummaryResult` and `AssociationsResult` as the canonical completed
  result data; do not persist generated output as part of this task.
- Routers remain thin; stream orchestration belongs in services and wire
  serialization belongs at the API boundary. Agent modules remain framework
  and SSE free.
- The backend is the only repository in scope; frontend changes are out of
  scope.

## Requirements

- R1. Return `text/event-stream` from both endpoints and expose a documented,
  typed event contract for progress, result data, completion, and failures.
- R2. Preserve the current successful result fields and values in the SSE
  stream so clients can reconstruct the same `SummaryResult` or
  `AssociationsResult` without a second request.
- R3. Preserve pre-stream HTTP error behavior (including missing/soft-deleted
  documents and unavailable providers) and convert failures after HTTP 200 into
  the terminal SSE error shape required by the backend streaming contract.
- R4. Preserve ordering and completion semantics for multi-pass summaries,
  association candidate validation, cache hits, and tenant-scoped reads.
- R5. Add offline service/API tests that consume the stream and assert event
  names, payloads, ordering, terminal behavior, and no raw user content in
  logs. Existing synchronous service semantics remain covered.
- R6. Update the relevant backend SSE/error documentation and any shared
  serializers or schemas without duplicating the chat wire-format logic.

## Acceptance Criteria

- [ ] AC1: Both existing POST routes respond with `Content-Type` beginning
      `text/event-stream` on a successful streamed request.
- [ ] AC2: A successful summary stream contains the complete summary result,
      including `document_id`, `summary`, `model`, and `latency_ms`, and ends
      with an unambiguous terminal success event.
- [ ] AC3: A successful association stream contains the complete association
      result, including deterministic candidate metadata and model reasons, and
      ends with an unambiguous terminal success event.
- [ ] AC4: Missing and soft-deleted documents still produce the existing 404
      JSON envelope before the SSE response starts; an upstream/provider
      failure after streaming begins produces one terminal SSE `error` event.
- [ ] AC5: Summary map/reduce pass order and association result filtering are
      unchanged; cache hits produce a valid stream without an unnecessary model
      call.
- [ ] AC6: API and service tests parse the stream through the shared SSE test
      helper and cover success, error, cache, long-summary, no-candidate, and
      provider-failure paths without live network calls.
- [ ] AC7: `ruff`, strict `mypy`, focused tests, and the full test suite pass;
      logs contain identifiers, lengths, and error classes only, never document
      content, prompts, summaries, or association reasons.

## Out of scope

- Frontend client implementation or UI behavior.
- New persistence, background jobs, resumability, or cancellation APIs for
  agent results.
- Changes to the underlying summarization, candidate gathering, association
  filtering, cache keys/TTLs, or provider configuration.
- Chat or writing event semantics unrelated to the shared serializer needed by
  these endpoints.

## Resolved decision

SSE event granularity: **typed progress/result events** (decided 2026-09-15).
Summary streams emit `run_started`, one or more progress events (one per
map pass plus the reduce pass), a final complete `summary` payload, then
`done`. Association streams emit `run_started`, a final complete
`associations` payload, then `done`. Event names, payloads, and error
shapes are specified in `design.md`.
