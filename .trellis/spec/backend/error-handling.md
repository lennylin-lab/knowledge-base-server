# Error Handling

> How errors are caught, logged, and returned.

---

## Overview

- **Domain exceptions** subclass `AppError` (`core/exceptions.py`); each maps
  to exactly one HTTP status via a shared handler.
- Services raise domain exceptions; **routers never raise HTTPException
  directly for domain failures**. `HTTPException` is allowed only for
  transport/auth concerns in `api/`.
- Every error response uses one envelope shape.

---

## Exception Hierarchy

```python
# core/exceptions.py (canonical shape)
class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_failed"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


# --- LLM / agent stack (see error taxonomy below) ---

class LLMProviderError(AppError):
    status_code = 502
    code = "llm_provider_error"


class LLMRateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class MCPToolError(AppError):
    status_code = 502
    code = "mcp_tool_failed"
```

Add a new subclass when a new failure mode appears — never overload an
existing one with a different status/code.

## Registration & Response Envelope

Registered once in `create_app()`; the handler produces the envelope:

```python
# main.py (canonical shape)
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log.warning("app_error", code=exc.code, path=request.url.path, detail=exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )
```

Response shape (all errors, all status codes):

```json
{
  "error": {
    "code": "not_found",
    "message": "Document abc123 not found",
    "details": {}
  }
}
```

FastAPI's built-in 422 request-validation errors are reshaped to the same
envelope via a `RequestValidationError` handler.

## Usage by Layer

| Layer | On failure |
|-------|-----------|
| Router | Lets exceptions propagate; maps nothing. Returns 2xx responses only. |
| Service | Raises `NotFoundError` / `ConflictError` / etc. Never catches broad `Exception` to swallow. |
| Repository | Raises SQLAlchemy errors as-is; translates to domain exceptions **only** for unique-violation → `ConflictError`. |
| Agent (`agents/`) | Provider failures (5xx/connect/timeout from `openai` SDK) surface as-is to the service, which wraps them in `LLMProviderError`. |
| `llm/` | Normalizes provider SDK exceptions; retries idempotent calls (configurable in Settings) before giving up. |
| `mcp/` | A failing external tool raises `MCPToolError` **with the tool name in `details`**; the agent decides to continue degraded or abort. |

## Error Taxonomy: LLM / MCP / streaming

| Failure | Exception | HTTP | Notes |
|---------|-----------|------|-------|
| Provider 5xx / connection / timeout after retries | `LLMProviderError` | 502 | message generic; provider + model go to logs |
| Provider rate limit | `LLMRateLimitedError` | 429 | include `Retry-After` when provider gives one |
| Context window exceeded | `ValidationError` | 422 | chunking/retrieval bug — fix there, don't truncate silently |
| External MCP tool failure | `MCPToolError` | 502 | `details: {"tool": "web_search"}`; agent may retry or degrade |
| Mid-stream failure (SSE already 200) | — | — | emit a final SSE `error` event, then close the stream; never leave it hanging |

Streaming rule: once the SSE response started (status 200 sent), the error
envelope can no longer be delivered as a status code. The chat service wraps
the agent run; on failure it pushes
`event: error\ndata: {"code": "...", "message": "..."}\n\n` and completes the
stream. The client contract (frontend repo) treats `error` event as terminal.

Background pipeline (indexing/embedding) never raises to users: failures are
caught at the job boundary, logged with full context, and the document's
`index_status` flips to `failed` — user-facing APIs expose that status.

Canonical service pattern:

```python
async def get_document(self, doc_id: UUID) -> DocumentRead:
    doc = await self._repo.get_by_id(doc_id)
    if doc is None:
        raise NotFoundError(f"Document {doc_id} not found")
    return DocumentRead.model_validate(doc)
```

## Rules

- **Never** `except Exception: pass` (or log-and-continue) anywhere in `src/`.
- Catch the narrowest exception type possible; unknown exceptions fall
  through to the 500 handler, which logs the traceback.
- 500 responses leak no internals: message is a generic one; details go to
  logs only.
- Services never import `fastapi.HTTPException` or set status codes — the
  status lives on the exception class in `core/exceptions.py`.
- Background jobs wrap their body in try/except, log with full context, and
  re-raise (or mark the job failed) — never silently drop.
