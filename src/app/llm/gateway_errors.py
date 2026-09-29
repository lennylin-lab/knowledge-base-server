"""Gateway error mapper: provider failures -> the AppError taxonomy.

The ONE place that knows provider error shapes (pydantic-ai's `ModelHTTPError`
on the agent paths, the openai SDK's exceptions on the embedding path) and the
ONE place that parses the knowledge-base-gateway envelope
`{"error": {"code", "message", "request_id"}}` (issue #5). Every documented
gateway `error.code` maps to a distinct `AppError` subclass; unknown codes,
missing/malformed bodies, and transport failures fall back to the legacy
status-based mapping (429 -> rate limited, else provider error). The
gateway's own `message` and any upstream content never reach a client or the
logs: the mapped classes' generic messages are the only text that can.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import openai
import structlog
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from app.core.exceptions import (
    AppError,
    CapabilityNotSupportedError,
    EmbeddingDimMismatchError,
    GatewayInvalidRequestError,
    GatewayUpstreamRejectedError,
    LLMGatewayAuthFailedError,
    LLMProviderError,
    LLMRateLimitedError,
    ModelNotAllowedError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

logger = structlog.get_logger(__name__)

_RATE_LIMITED_MESSAGE = "LLM provider rate limit exceeded"
_PROVIDER_FAILED_MESSAGE = "LLM provider request failed"

# One row per documented gateway `error.code` (gateway docs — 17 codes): the
# mapped class carries the HTTP status, the message is the bucket-generic
# text a client may see, and `reason` is the only 429 detail worth
# distinguishing (quota vs plain rate limit). The gateway's own message and
# upstream content appear nowhere in this table.
_GATEWAY_CODE_MAP: dict[str, tuple[type[AppError], str, str | None]] = {
    "invalid_request": (
        GatewayInvalidRequestError,
        "LLM gateway rejected the request as invalid",
        None,
    ),
    "schema_validation_failed": (
        GatewayInvalidRequestError,
        "LLM gateway rejected the request as invalid",
        None,
    ),
    "invalid_tool_arguments": (
        GatewayInvalidRequestError,
        "LLM gateway rejected the request as invalid",
        None,
    ),
    "capability_not_supported": (
        CapabilityNotSupportedError,
        "LLM gateway does not support the requested capability",
        None,
    ),
    "upstream_rejected_request": (
        GatewayUpstreamRejectedError,
        "The LLM gateway's upstream rejected the request",
        None,
    ),
    "invalid_api_key": (LLMGatewayAuthFailedError, "LLM gateway authentication failed", None),
    "api_key_expired": (LLMGatewayAuthFailedError, "LLM gateway authentication failed", None),
    "api_key_revoked": (LLMGatewayAuthFailedError, "LLM gateway authentication failed", None),
    "model_not_allowed": (
        ModelNotAllowedError,
        "The requested model is not allowed for this API key",
        None,
    ),
    "rate_limit_exceeded": (LLMRateLimitedError, _RATE_LIMITED_MESSAGE, None),
    "quota_exceeded": (LLMRateLimitedError, _RATE_LIMITED_MESSAGE, "quota"),
    "upstream_unavailable": (
        UpstreamUnavailableError,
        "LLM provider is temporarily unavailable",
        None,
    ),
    "no_route_available": (
        UpstreamUnavailableError,
        "LLM provider is temporarily unavailable",
        None,
    ),
    "limiter_unavailable": (
        UpstreamUnavailableError,
        "LLM provider is temporarily unavailable",
        None,
    ),
    # Delivered on HTTP 503 but semantically 429: the caller should back off
    # and retry, so it maps to the rate-limited class (issue decision).
    "upstream_rate_limited": (LLMRateLimitedError, _RATE_LIMITED_MESSAGE, None),
    "upstream_timeout": (UpstreamTimeoutError, "LLM provider request timed out", None),
    "embedding_dim_mismatch": (
        EmbeddingDimMismatchError,
        "Embedding vector dimension does not match the configured width",
        None,
    ),
}


def map_provider_error(exc: Exception) -> AppError:
    """Map any provider-shaped failure onto the error taxonomy.

    Branch order: `AppError` passes through unchanged (idempotent — services
    re-raise already-mapped failures); pydantic-ai's `ModelHTTPError` carries
    the gateway envelope plus a parsed `retry_after`; the openai SDK's status
    errors carry the same via their httpx response; `ModelAPIError` /
    remaining `openai.APIError` are transport-level provider failures; and
    anything else is the generic internal error. Details live in logs and
    (for gateway failures) in the raised error's safe `details` dict.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, ModelHTTPError):
        # ModelHTTPError subclasses ModelAPIError, so this must come first.
        return _map_http_failure(exc.status_code, exc.body, exc.retry_after)
    if isinstance(exc, openai.APIStatusError):
        # RateLimitError is an APIStatusError subclass; both carry `response`.
        return _map_http_failure(
            exc.status_code, exc.body, _parse_retry_after(exc.response.headers.get("retry-after"))
        )
    if isinstance(exc, (ModelAPIError, openai.APIError)):
        return LLMProviderError(_PROVIDER_FAILED_MESSAGE)
    return AppError("Internal server error")


def _map_http_failure(status_code: int, body: object, retry_after: float | None) -> AppError:
    """Map one HTTP-shaped provider failure; logs the mapped ids once.

    A known gateway code produces its dedicated class with the safe
    `gateway_code` / `gateway_request_id` (and 429 `reason`) details; the
    `Retry-After` seconds ride along only on 429-mapped failures. Unknown or
    absent codes fall back to the legacy status-based mapping — byte-identical
    to the pre-gateway behavior, with no details attached.
    """
    gateway_code, request_id = _parse_gateway_envelope(body)
    log = logger.bind(
        gateway_code=gateway_code,
        gateway_request_id=request_id,
        status_code=status_code,
    )
    row = _GATEWAY_CODE_MAP.get(gateway_code) if gateway_code is not None else None
    if row is None:
        # Legacy fallback: byte-identical to the pre-gateway status-based
        # mapping (unknown codes, non-gateway bodies, missing envelopes) —
        # no details attached. A plain provider 429 lands here too.
        failure: AppError = (
            LLMRateLimitedError(_RATE_LIMITED_MESSAGE)
            if status_code == 429
            else LLMProviderError(_PROVIDER_FAILED_MESSAGE)
        )
    else:
        error_class, message, reason = row
        details: dict[str, Any] = {"gateway_code": gateway_code}
        if request_id is not None:
            details["gateway_request_id"] = request_id
        if reason is not None:
            details["reason"] = reason
        failure = error_class(message, details=details)
    if failure.status_code == 429 and retry_after is not None:
        # 429 semantics only — the fallback 429 and a mapped
        # upstream_rate_limited (delivered on 503) honor it alike.
        failure.retry_after_seconds = retry_after
    log.warning("gateway_error_mapped")
    return failure


def _parse_gateway_envelope(body: object) -> tuple[str | None, str | None]:
    """Tolerant read of the gateway envelope: (`error.code`, `error.request_id`).

    Anything unexpected — non-dict body, missing/foreign `error` object,
    non-string fields — degrades to `(None, None)`: the legacy status-based
    fallback owns the failure then.
    """
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    request_id = error.get("request_id")
    return (
        code if isinstance(code, str) else None,
        request_id if isinstance(request_id, str) else None,
    )


def _parse_retry_after(raw: str | None) -> float | None:
    """`Retry-After` header value -> seconds (delay-seconds or HTTP-date).

    Absent or unparseable -> `None` (no header is emitted then). Mirrors
    pydantic-ai's `ModelHTTPError.retry_after` parsing for the SDK path,
    where only the raw httpx header is available.
    """
    if raw is None:
        return None
    try:
        seconds = int(raw)
        return float(seconds) if seconds >= 0 else None
    except ValueError:
        pass
    try:
        retry_time = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_time is None:
        return None
    if retry_time.tzinfo is None:
        retry_time = retry_time.replace(tzinfo=UTC)
    return max(0.0, (retry_time - datetime.now(UTC)).total_seconds())
