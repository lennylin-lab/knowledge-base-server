# Execution Plan: Document summary and associations SSE endpoints

## Stage 1: Shared SSE plumbing

- [x] Extract a shared SSE serializer helper (name-map + `to_sse`) so chat
      and the new agent streams share one wire-format implementation; chat
      delegates and its tests stay green.
- [x] Add `src/app/schemas/agent_stream.py` with the event models and
      `AgentStreamEvent` union exactly as specified in `design.md` §2
      (reuse `ErrorEvent` from `schemas/chat.py`).

## Stage 2: Service stream generators

- [x] Convert `SummarizeService` to expose `summarize_document_stream`
      yielding `run_started` → progress per pass → `summary` → `done`,
      with the 404 gate and cache check before the first yield per
      `design.md` §3-4; keep the coroutine `summarize_document` as a
      draining wrapper (error streams re-raise the AppError).
- [x] Same for `AssociationService.associate_document_stream` (gather →
      no-candidate/cache shortcut → single LLM call → `associations` →
      `done`).
- [x] Offline service tests: event order/payloads, multi-pass progress
      indices, cache-hit stream without model call, no-candidate path,
      provider failure → single terminal `error`, wrapper parity with old
      results; extend existing fakes as needed.

## Stage 3: Endpoints

- [x] Switch both document routes to `EventSourceResponse` with the priming
      idiom; keep `TenantScope` deps and paths/methods unchanged.
- [x] API tests via the shared `parse_sse` helper: content-type, full event
      sequences for success/cache/error, 404 JSON envelope pre-stream for
      missing and soft-deleted documents, tenant scoping.

## Stage 4: Docs and quality gates

- [x] Update the SSE/error documentation (error-handling spec streaming
      section and/or `docs/`) with the new event contract; no duplication of
      chat wire-format docs.
- [x] Run: `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src`, `uv run pytest` — all green; no new log content
      beyond ids/lengths/error classes.

## Validation Commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/test_documents_service.py tests/test_chat_api.py -q
uv run pytest
```

## Risk and Rollback Points

- **Chat serializer refactor (Stage 1)** is the riskiest shared change —
  run the full chat/writing API tests immediately after it; if green, the
  rest is additive.
- Rollback = revert endpoint handlers to the coroutine service calls
  (service wrappers preserve the old surface, so this is always available).
- Do not change cache keys, summarization logic, candidate gathering, or
  provider configuration.

## Implementation Evidence (sanitized, 2026-09-15)

- Stage 1: new `src/app/api/v1/endpoints/sse.py` (generic
  `to_sse_event`/`to_sse`/`primed_sse`, PEP 695 type params);
  `src/app/schemas/agent_stream.py` (`AgentRunStartedEvent`,
  `SummaryProgressEvent`, `SummaryResultEvent(SummaryResult)`,
  `AssociationsResultEvent(AssociationsResult)`, `AgentDoneEvent`,
  `AgentStreamEvent` union reusing chat's `ErrorEvent`). Chat's
  `to_sse_event`/`to_sse`/`_primed_sse` delegate to the shared module; chat
  event names unchanged. Chat/writing tests run immediately after:
  109 passed.
- Stage 2: `services/agents.py` — public stream methods wrap internal
  `_summarize_events`/`_association_events` generators; the `yielded` flag
  preserves pre-stream AppError propagation (404) while post-run_started
  failures become exactly one terminal `error`. Fixed grammar confirmed:
  single-chunk summaries emit map(1/2)+reduce(2/2). Coroutine wrappers
  return the result event (it IS the result payload) and re-raise AppErrors.
  Service tests: 6 summarize + 6 association stream tests added
  (order/payloads, multi-pass indices, cache-hit without model call,
  no-candidate, terminal error, wrapper parity). 30 → 42 service/cache tests
  green.
- Stage 3: `documents.py` routes now `EventSourceResponse` with the priming
  pull; `TenantScope`/paths/methods unchanged; `response_model=None`.
  API tests rewritten/added via `parse_sse`: content-type, full sequences
  (success, map/reduce progress, cache hit, provider failure → terminal
  `error`), 404 JSON envelope pre-stream for missing + soft-deleted,
  503 no-key gate unchanged. Discrepancy note vs design.md §4 draft text:
  cache lookup happens AFTER `run_started` is yielded (design.md §2 cache
  sequence shows run_started first) — dispatch summary's "cache check before
  first yield" read was not implementable together with that sequence;
  design.md §2/§4 followed. 404 gate does remain strictly pre-first-yield.
- Stage 4: error-handling spec streaming section gained the document-agent
  event-order table + pre-stream rule; sync-endpoint paragraph updated.
  Gates: ruff check clean, ruff format --check clean (174 files), mypy src
  clean (81 files), full pytest 661 passed / 11 deselected (live marks).
  No log content beyond ids/lengths/token counts/error classes added.
