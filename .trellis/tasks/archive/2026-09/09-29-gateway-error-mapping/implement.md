# Implementation Plan

Validation baseline (after each step; full set before commit):

```bash
uv run ruff format --check . && uv run ruff check .
uv run mypy .
uv run pytest -q          # default marks (offline + db + es; live auto-skip)
```

## Step 1 — Exception classes + Retry-After plumbing (R2/R3)

- [ ] `core/exceptions.py`: 8 new `AppError` subclasses (design §1, generic
      messages, statuses per the table); `AppError.retry_after_seconds`
      attribute (default `None`); `error_response(..., headers=None)` +
      `app_error_handler` emits `Retry-After` when set.
- [ ] `tests/test_error_envelope.py`: header present on a 429 carrying the
      attribute, absent otherwise; envelope body unchanged.

## Step 2 — Mapper (R1, M1)

- [ ] `llm/gateway_errors.py`: `_GATEWAY_CODE_MAP` (all 17 documented codes),
      envelope parsing (tolerant), `map_provider_error` with the branch order
      from design §2, `gateway_error_mapped` structured log, quota reason,
      Retry-After threading (`ModelHTTPError.retry_after`; SDK httpx headers).
- [ ] `tests/test_gateway_errors.py`: per-row fixture matrix (AC1), unknown
      code / non-dict body / missing `error` key / transport fallbacks,
      Retry-After parse/absent, quota reason, AppError idempotency, SDK-path
      fixture with stubbed httpx response.
- [ ] Boundary: unknown code on HTTP 429 still → `LLMRateLimitedError`
      (byte-identical legacy fallback per status).

## Step 3 — Service call sites (R1, M1)

- [ ] `services/chat.py` + `services/agents.py`: delete both `_as_app_error`
      bodies, import + call `map_provider_error`.
- [ ] `services/operation.py`: drop `from app.services.agents import
      _as_app_error`, use the shared mapper.
- [ ] `llm/embeddings.py`: route the two SDK except-branches through the
      mapper; keep the two local contract-check raises unchanged.
- [ ] Existing chat/summarize/association/writing/operation service tests
      stay green (their scripted failures now flow the same mapper).

## Step 4 — ARQ worker classification (R6, M1)

- [ ] `rag/worker.py::is_transient_index_error`: explicit transient tuple
      (rate-limited, upstream-unavailable, upstream-timeout, search-index,
      OSError); permanent classes fall through to settle-failed;
      `llm/errors.py::is_permanent_provider_error` kept for legacy
      SDK-cause paths.
- [ ] Worker tests: auth-shape Gateway failure → `failed` on first attempt,
      no `Retry`; upstream-unavailable → `Retry` raised (AC6).

## Step 5 — JSON details (R4, M2)

- [ ] Mapper attaches `gateway_code` / `gateway_request_id` details (only
      when supplied) — covered by Step 2 fixtures.
- [ ] Confirm no service strips `details` before the envelope (they raise the
      mapped error as-is today).
- Rollback point: M1+M2 is a self-consistent deliverable (JSON-only).

## Step 6 — SSE details (R5, M3)

- [ ] `schemas/chat.py::ErrorEvent`: `details: dict[str, Any] | None = None`.
- [ ] All `ErrorEvent(...)` construction sites (chat stream, summarize,
      associations, writing, operation draft — grep for `ErrorEvent(`) pass
      `details=failure.details or None`.
- [ ] Check the shared SSE serializer's dump behavior for `None` fields;
      pre-Gateway streams stay byte-identical.
- [ ] Tests: per-stream terminal-error assertions — code + details present for
      a Gateway failure, `message` never contains the Gateway's text (AC2,
      AC5); embedding dim-mismatch fixture through the provider (AC3).

## Step 7 — Spec + docs (R7, AC7)

- [ ] `.trellis/spec/backend/error-handling.md`: Gateway mapping table,
      `Retry-After` convention (taxonomy row updated from "include
      Retry-After when provider gives one" to the implemented mechanism),
      SSE `details` contract, mapper call-site rule (services never map
      provider errors privately).
- [ ] README/Gateway docs: no runbook change needed (no env/schema).

## Final gate (Phase 2.2)

- [ ] Full suite green (ruff/mypy/pytest; db+es as available).
- [ ] Cross-check: both former `_as_app_error` copies deleted; grep proves no
      `except` in services maps `ModelHTTPError`/`openai.*` locally anymore
      (single mapper invariant).
- [ ] AC1–AC8 diffed against the issue checklist.
