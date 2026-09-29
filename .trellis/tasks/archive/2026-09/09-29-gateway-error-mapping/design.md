# Design: one Gateway error mapper, distinct AppError codes

## Scope

- `core/exceptions.py` — 8 new `AppError` subclasses + `Retry-After` header
  plumbing in the shared handler.
- `llm/gateway_errors.py` (new) — envelope parsing + the one
  `map_provider_error`.
- `llm/errors.py` — permanent/transient helpers stay; `llm/` remains the only
  place that knows SDK/pydantic-ai exception shapes (error-handling.md
  layering rule: services may import SDK *types* for mapping, but the mapper
  centralizes even that).
- `services/chat.py`, `services/agents.py` — delete both `_as_app_error`
  copies, call the shared mapper; `services/operation.py` — drop its private
  cross-module import of `agents._as_app_error`.
- `llm/embeddings.py` — route SDK catches through the mapper.
- `rag/worker.py` — `is_transient_index_error` aligned with mapped classes.
- `schemas/chat.py::ErrorEvent` — optional additive `details`.
- `.trellis/spec/backend/error-handling.md` — mapping table + conventions
  (Phase 3.3).

No Settings keys, no DB changes, no new endpoints. Wire changes are additive
(new codes, optional details, one new response header).

## 1. New exception classes (`core/exceptions.py`)

Follow the established rule — one subclass per failure mode, status on the
class, generic message per class:

```python
class GatewayInvalidRequestError(AppError):   # 422 gateway_invalid_request
class CapabilityNotSupportedError(AppError):  # 502 capability_not_supported
class GatewayUpstreamRejectedError(AppError): # 502 gateway_upstream_rejected
class LLMGatewayAuthFailedError(AppError):    # 503 llm_gateway_auth_failed
class ModelNotAllowedError(AppError):         # 403 model_not_allowed
class UpstreamUnavailableError(AppError):     # 503 upstream_unavailable
class UpstreamTimeoutError(AppError):         # 504 upstream_timeout
class EmbeddingDimMismatchError(AppError):    # 502 embedding_dim_mismatch
```

`rate_limit_exceeded` / `quota_exceeded` / `upstream_rate_limited` (semantically
429) reuse the existing `LLMRateLimitedError` — same failure mode (caller
should back off), same status; quota vs rate-limit is data (`details.reason`),
not a different exception class.

Deliberately NOT subclasses of `LLMProviderError`: the ARQ worker's transient
branch keys on `LLMProviderError`; making the new 422/502/503/504/403 modes
direct `AppError` subclasses keeps `is_transient_index_error` explicit and
prevents "new class silently inherits retry-worthiness".

### Retry-After plumbing

`AppError` gains an optional transport attribute (data, not class state —
only 429s ever carry it):

```python
class AppError(Exception):
    retry_after_seconds: float | None = None  # set post-construction by the mapper
```

`error_response(..., headers: dict[str, str] | None = None)` grows an optional
parameter; `app_error_handler` adds
`{"Retry-After": str(int(math.ceil(exc.retry_after_seconds)))}` when set.
Header value uses seconds (RFC 9110 delay-seconds); pydantic-ai's
`ModelHTTPError.retry_after` already parses both delay-seconds and HTTP-date
forms into seconds, so the mapper stores seconds only.

## 2. Mapper (`llm/gateway_errors.py`)

```python
def map_provider_error(exc: Exception) -> AppError
```

Branch order (single public entry; services call this directly):

1. `AppError` → return as-is (idempotent; keeps current re-raise semantics).
2. `ModelHTTPError` (pydantic-ai paths: chat, summarize, associations,
   writing, draft, rewrite) → `_map_http(status, body, headers)`.
3. `openai.RateLimitError` / `openai.APIStatusError` (SDK path:
   `llm/embeddings.py`; `APIStatusError` carries `response` + `body`) →
   same `_map_http` with `exc.response.headers`.
4. `ModelAPIError` (connection/timeout wrapper) and remaining `openai.APIError`
   → `LLMProviderError("LLM provider request failed")`.
5. Fallback → `AppError("Internal server error")` (today's tail).

`_map_http`:

- Parse the Gateway envelope: `body` is the parsed JSON dict (pydantic-ai) or
  SDK-parsed object; read `body["error"]["code"]` and
  `body["error"]["request_id"]` tolerantly (missing/malformed → unknown
  fallback). The `error.type` field is not needed for the mapping (codes are
  unique across types) — noted in case future codes collide.
- Look up `gateway_code` in the module-level mapping table
  (`_GATEWAY_CODE_MAP: dict[str, Callable[[str | None], AppError]]`-shaped or
  plain `(class, message)` table). Unknown/absent code → status-based legacy
  fallback: 429 → `LLMRateLimitedError`, everything else →
  `LLMProviderError` (byte-identical to today's `_as_app_error`).
- Attach `details={"gateway_code": ..., "gateway_request_id": ...}` only when
  the Gateway actually supplied them (empty dict otherwise — envelopes keep
  showing `{}` for non-Gateway providers).
- 429-mapped failures: parse `Retry-After` from the headers
  (`ModelHTTPError.retry_after` property, or httpx response header on the SDK
  path; tolerate a missing/unparseable header → no attribute set). When the
  gateway code is `quota_exceeded`, `details["reason"] = "quota"`.
- `upstream_rate_limited` maps to `LLMRateLimitedError` DESPITE arriving on
  HTTP 503 — the caller should back off and retry, which is the 429 semantic
  (issue decision). Its Retry-After, if the Gateway sends one on that 503, is
  honored the same way.
- Log once per mapping: `logger.warning("gateway_error_mapped",
  gateway_code=..., gateway_request_id=..., status_code=...)` — error-class +
  ids only; the Gateway `message` and upstream content are NEVER logged or
  returned (R7/AC5). The mapping table's messages are the only text that can
  reach a client.

## 3. Call-site consolidation

- `services/chat.py::_as_app_error` and `services/agents.py::_as_app_error`
  are deleted; both call sites import
  `map_provider_error` from `app.llm.gateway_errors` (aliased locally where
  the short name reads better). The two copies are byte-identical today, so
  no behavior change beyond the new mapping.
- `services/operation.py` currently does
  `from app.services.agents import _as_app_error` — replaced by the shared
  import (removes a private cross-module dependency; draft stream inherits
  the new mapping for free).
- `llm/embeddings.py`: the `openai.RateLimitError` / `openai.APIError`
  except-branches call the mapper; its bespoke "mismatched number of vectors"
  / local dimension-check errors stay as-is (those are OUR contract checks,
  not Gateway failures).

## 4. SSE terminal errors (Milestone 3)

`ErrorEvent` (schemas/chat.py, the one shared error dialect):

```python
class ErrorEvent(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None   # additive; gateway_code/request_id/reason
```

- Streams that already hold the mapped `AppError` (chat's
  `ErrorEvent(code=failure.code, message=failure.message)` sites in
  chat/agents/operation) additionally pass
  `details=failure.details or None` — `None` (not `{}`) keeps pre-Gateway
  payloads byte-identical.
- Serialization: the shared SSE serializer emits the model dump; an omitted
  `None` field (pydantic `model_dump(exclude_none=True)` semantics already
  used there — verify at implementation; if the serializer dumps all fields,
  gate on `None`) keeps old clients unaffected either way since an extra JSON
  key is additive.
- AC2 consistency is structural: both surfaces read `failure.code` from the
  SAME mapped `AppError` instance, so no per-stream mapping can drift.

## 5. ARQ worker (`rag/worker.py` + `llm/errors.py`)

`is_transient_index_error` becomes an explicit allowlist of retry-worthy
mapped failures:

```python
_TRANSIENT_INDEX_ERRORS = (LLMRateLimitedError, UpstreamUnavailableError,
                           UpstreamTimeoutError, SearchIndexError)
if isinstance(exc, (*_TRANSIENT_INDEX_ERRORS, OSError)): return True
return isinstance(exc, LLMProviderError) and not is_permanent_provider_error(exc)
```

Everything else (the new permanent classes, arbitrary bugs) settles `failed`
immediately — no dependence on the `openai.AuthenticationError` cause chain
(the old `is_permanent_provider_error` stays for legacy
`LLMProviderError`-with-SDK-cause paths, e.g. non-Gateway deployments).
`upstream_rate_limited` → `LLMRateLimitedError` → transient, correct (a
gateway-side upstream throttle clears on its own).

## 6. Tests

- `tests/test_gateway_errors.py` (new): per-row fixtures of the mapping table
  (ModelHTTPError with realistic `body` dicts, including
  `request_id` present/absent, unknown code, non-dict body, missing `error`
  key) → assert `type(err)`, `.code`, `.status_code`, `.details`;
  Retry-After parsing (seconds + absent); quota `details.reason`;
  idempotency on AppError input; SDK-path fixtures (`openai.RateLimitError`
  with a stubbed httpx response) for the embedding route.
- `tests/test_chat_service.py` / `test_summarize_service.py` /
  `test_association_service.py` / `test_writing_service.py` (or a focused
  shared-mapper test): scripted provider failures with Gateway bodies →
  terminal `error` event code == JSON envelope code; details present; gateway
  message text absent from `message` (AC2/AC5).
- `tests/test_embeddings.py`: `embedding_dim_mismatch` body →
  `EmbeddingDimMismatchError` (AC3); unknown SDK error → generic.
- `tests/test_arq_worker.py` (or the worker's existing test file): auth-shape
  Gateway failure settles `failed` with no `Retry`; upstream-unavailable
  still raises `Retry` (AC6).
- `tests/test_error_envelope.py`: `Retry-After` header present on a 429
  AppError carrying the attribute, absent otherwise (AC4).

## Tradeoffs & rejected alternatives

- **Instance-level `code` overrides** on one `LLMGatewayError` class —
  rejected: violates "status lives on the exception class" (error-handling.md
  Rules) and would smear the taxonomy the issue wants to establish.
- **Mapping in each service** (keep two `_as_app_error` copies, extend both) —
  rejected: the copies already drifted into a layering wart
  (`operation.py` importing a private from `agents.py`); one mapper in `llm/`
  is the issue's own target and the layering rule's home for SDK shapes.
- **Retry-After only on the SDK path** — rejected: pydantic-ai exposes
  lowercased `headers` (and a parsed `retry_after`), so the chat/agent paths
  get the same treatment for free.
- **`ErrorEvent.details` as always-present dict** — rejected: `None` default
  keeps pre-Gateway SSE payloads unchanged (byte-identical streams for
  non-Gateway deployments).
- **Parsing `error.type`** — not needed; codes are unique across the three
  gateway doc versions. The parser keeps the whole `error` dict in hand so a
  future type-disambiguation is a local change.

## Compatibility & rollback

- Additive wire surface: new codes (previously `llm_provider_error`),
  optional `details`, optional `Retry-After` header. A client keying on the
  old codes sees new, more specific ones — the issue accepts this (it IS the
  feature); the flutter repo only needs work for UX on `details` (out of
  scope).
- Unknown/absent bodies fall back to today's exact behavior, so non-Gateway
  OpenAI-compatible providers are unaffected.
- Rollback = revert commit; no data, schema, or Settings involvement.

## Milestones (delivery order inside one task)

1. M1 = mapper + classes + logging + service call-site consolidation + worker
   alignment (tests AC1/AC2-code/AC6).
2. M2 = `details` in JSON envelopes + `Retry-After` (AC4; AC1 details
   assertions).
3. M3 = `ErrorEvent.details` (AC2 full, AC5, AC3 assertions already possible).
