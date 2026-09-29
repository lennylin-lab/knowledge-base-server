# Parse Gateway error codes into distinguishable API error codes

Source issue: remote GitHub issue #5 ("Feature: 解析 Gateway 错误码并映射为可区分的 API error code").

## Problem

When calling knowledge-base-gateway via `KB_CHAT_BASE_URL` /
`KB_EMBEDDING_BASE_URL`, the server ignores the Gateway envelope's
`error.code` / `error.type`. Everything except HTTP 429 collapses into
`llm_provider_error` with the fixed message "LLM provider request failed", so
the Gateway's documented failure modes (key invalid/expired/revoked,
`model_not_allowed`, `capability_not_supported`, `upstream_rate_limited`
delivered as HTTP 503, `embedding_dim_mismatch`, …) are indistinguishable on
the API/SSE surface. Operators must read log `error_class` values, and the
Gateway `request_id` is not correlated anywhere user-visible.

Today's mapping lives in two duplicated `_as_app_error` copies
(`services/chat.py`, `services/agents.py` — the latter also imported privately
by `services/operation.py`) plus direct SDK catches in `llm/embeddings.py`.

## Goal

One Gateway error mapper in **`llm/`** parses
`{"error": {"code", "message", "request_id"}}` from provider failures and maps
each known Gateway `error.code` to a distinct `AppError` subclass (code +
HTTP status per the issue's table); unknown codes / no body / transport errors
fall back to today's behavior. JSON envelopes may carry
`gateway_code` / `gateway_request_id` in `details`; 429 responses transparently
forward `Retry-After`; the four SSE streams share the same mapper so terminal
`error` events match the JSON pre-stream errors. The Gateway's own `message`
and any upstream content never enter an API `message` field.

## Scope decision (user-approved 2026-09-29)

Single task covering all three backend phases of the issue (they are
cumulative layers of one contract, sharing the mapper):

- **Milestone 1 (Phase 1)**: mapper + structured logs (`gateway_code`,
  `gateway_request_id`) + new `code`s; message stays bucket-generic.
- **Milestone 2 (Phase 2)**: JSON `details` + `Retry-After` header on 429.
- **Milestone 3 (Phase 3, backend part)**: SSE `ErrorEvent` gains optional
  `details` (additive, non-breaking — clients that ignore it are unaffected);
  frontend UX lives in the separate frontend repo and is out of scope here.

## Requirements

### R1 — Mapper in `llm/`

- New module `llm/gateway_errors.py`: parse `ModelHTTPError.body` /
  SDK exception bodies for the Gateway envelope
  (`error.code`, `error.request_id`) and the `Retry-After` header
  (`ModelHTTPError.headers` is lowercased; pydantic-ai also exposes
  `retry_after`; SDK `APIStatusError` carries `response.headers`).
- One public `map_provider_error(exc) -> AppError` used by chat, summarize,
  associations, writing, draft (operation), and the embedding provider — the
  two private `_as_app_error` copies are deleted, and
  `services/operation.py`'s private cross-module import disappears with them.
- Unknown `error.code`, missing/unparseable body, non-Gateway bodies, and
  transport-layer failures fall back to today's behavior
  (`llm_provider_error` 502 / `rate_limited` 429 / generic `AppError` 500).

### R2 — New error classes (one per failure mode)

Per error-handling.md ("status lives on the exception class"), the mapping
table lands as new `AppError` subclasses in `core/exceptions.py`:

| Gateway `error.code` (HTTP) | New class | `code` | HTTP |
| --- | --- | --- | --- |
| `invalid_request`, `schema_validation_failed`, `invalid_tool_arguments` (400) | `GatewayInvalidRequestError` | `gateway_invalid_request` | 422 |
| `capability_not_supported` (400) | `CapabilityNotSupportedError` | `capability_not_supported` | 502 |
| `upstream_rejected_request` (400) | `GatewayUpstreamRejectedError` | `gateway_upstream_rejected` | 502 |
| `invalid_api_key`, `api_key_expired`, `api_key_revoked` (401) | `LLMGatewayAuthFailedError` | `llm_gateway_auth_failed` | 503 |
| `model_not_allowed` (403) | `ModelNotAllowedError` | `model_not_allowed` | 403 |
| `rate_limit_exceeded`, `quota_exceeded` (429) | existing `LLMRateLimitedError` | `rate_limited` | 429 (`details.reason` distinguishes quota) |
| `upstream_unavailable`, `no_route_available`, `limiter_unavailable` (503) | `UpstreamUnavailableError` | `upstream_unavailable` | 503 |
| `upstream_rate_limited` (HTTP 503, semantic 429) | existing `LLMRateLimitedError` | `rate_limited` | 429 |
| `upstream_timeout` (504) | `UpstreamTimeoutError` | `upstream_timeout` | 504 |
| `embedding_dim_mismatch` (500) | `EmbeddingDimMismatchError` | `embedding_dim_mismatch` | 502 |

Messages per class are generic; the Gateway's `error.message` and any upstream
content go to logs only.

### R3 — Retry-After (Milestone 2)

- The mapper reads `Retry-After` on 429-mapped failures (both the pydantic-ai
  header dict and the SDK httpx response) and threads it onto the raised
  error.
- The shared `AppError` handler emits a `Retry-After` response header when the
  exception carries one (envelope body unchanged).

### R4 — JSON details (Milestone 2)

- `details` may carry `gateway_code`, `gateway_request_id`, and for 429s
  `reason` (`"quota"` vs `"rate_limit"`) — never prompt content, API keys, or
  the Gateway's `message`.

### R5 — SSE terminal errors (Milestone 3)

- `ErrorEvent` (the one shared terminal event) gains an optional
  additive `details` field carrying the same safe keys, so all four streams
  (chat, summary, associations, writing — and the draft stream via the shared
  mapper) emit terminal error codes identical to the JSON pre-stream errors.
- Backward compatible: clients ignoring `details` see only the existing
  `code`/`message` pair.

### R6 — ARQ worker classification

- `is_transient_index_error` aligns with the mapped codes: `rate_limited`,
  `upstream_unavailable`, `upstream_timeout` (+ existing
  `SearchIndexError`/`OSError`) stay retry-worthy; the new permanent classes
  (auth-failed, `model_not_allowed`, capability, invalid-request,
  dim-mismatch, upstream-rejected) settle `failed` immediately without
  relying on the `openai.AuthenticationError` cause chain.

### R7 — Logging

- Every mapped failure logs a structured event with `gateway_code` and
  `gateway_request_id` (Milestone 1) — error class + these ids only; Gateway
  message text and upstream content never logged.

## Non-goals

- No forwarding of the server's inbound `X-Request-ID` to the Gateway
  (separate future issue, gateway docs §9).
- No change to the vector-retrieval degradation strategy (embedding failure
  mid-search still degrades to BM25-only).
- No frontend changes (separate repo); SSE `details` is additive so the
  current client keeps working unchanged.
- Unchanged paths: unconfigured `CHAT_API_KEY` → 503 `chat_unavailable`;
  MCP soft-fail; `chat_unavailable` gating order.

## Acceptance Criteria

- [ ] AC1: Fixture-based unit tests — every Gateway `error.code` row of the
      mapping table (as `ModelHTTPError` body fixtures) maps to the expected
      `AppError.code` AND HTTP status; unknown code / no body / transport
      error fall back to `llm_provider_error` (or 429 for SDK rate-limit
      shape).
- [ ] AC2: chat / summarize / associations / writing terminal SSE `error`
      events carry the same `code` as the JSON envelope for the same failure
      (one shared mapper; tests pin at least chat + one document-agent
      stream per new code class).
- [ ] AC3: `embedding_dim_mismatch` through the Gateway maps to the dedicated
      code, not generic `llm_provider_error` (embedding provider path).
- [ ] AC4: 429-mapped JSON responses carry `Retry-After` when the Gateway
      supplied it, and omit it when not; `details.reason` distinguishes
      `quota_exceeded`.
- [ ] AC5: No Gateway `message` or upstream content in any API `message`
      field or SSE `message` (tests assert the gateway text is absent).
- [ ] AC6: ARQ worker: auth-failed / model-not-allowed / dim-mismatch shaped
      failures settle `failed` on the first attempt (no `Retry` raised);
      upstream-unavailable / timeout / rate-limit still retry.
- [ ] AC7: Spec updated: `.trellis/spec/backend/error-handling.md` gains the
      Gateway mapping table, the `Retry-After` convention, and the SSE
      `details` contract.
- [ ] AC8: Full gates green (ruff, mypy, offline pytest; db/es suites where
      services are reachable).
