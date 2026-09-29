"""Domain exception hierarchy and the shared error-envelope handlers."""

from __future__ import annotations

import math
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from app.core.logging import REQUEST_ID_HEADER

logger = structlog.get_logger(__name__)


class AppError(Exception):
    """Base for every domain error; maps to exactly one HTTP status + code."""

    status_code: int = 500
    code: str = "internal_error"
    # Optional transport hint in SECONDS, set post-construction (e.g. by the
    # gateway error mapper on 429-mapped failures only — data, never class
    # state). Rendered as a `Retry-After` response header; never part of the
    # envelope body.
    retry_after_seconds: float | None = None

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
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


class AuthenticationError(AppError):
    """Request authentication failed or is not configured (HTTP 401).

    The response message is deliberately generic — the verification failure
    reason (issuer/audience/signature/expiry/JWKS) goes to logs only, never
    into the envelope, so probes learn nothing about why a token failed.
    """

    status_code = 401
    code = "unauthorized"


# --- LLM / agent stack (see error-handling.md taxonomy) ---


class LLMProviderError(AppError):
    status_code = 502
    code = "llm_provider_error"


class LLMRateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


# --- Gateway error mapping (raised by llm/gateway_errors.py, issue #5) ---
# One subclass per documented gateway `error.code`; status lives on the class.
# They extend AppError DIRECTLY (not LLMProviderError): the ARQ worker's
# transient branch keys on LLMProviderError, so these never silently inherit
# retry-worthiness. Messages are bucket-generic — the gateway's own `message`
# and any upstream content go to logs only.


class GatewayInvalidRequestError(AppError):
    """The gateway rejected the request itself (`invalid_request`,
    `schema_validation_failed`, `invalid_tool_arguments`) — a request-shape
    problem a retry cannot fix."""

    status_code = 422
    code = "gateway_invalid_request"


class CapabilityNotSupportedError(AppError):
    """The gateway's catalog does not support the requested capability
    (`capability_not_supported`, e.g. tool passthrough on a non-tool model)."""

    status_code = 502
    code = "capability_not_supported"


class GatewayUpstreamRejectedError(AppError):
    """The gateway's upstream provider rejected the request
    (`upstream_rejected_request`)."""

    status_code = 502
    code = "gateway_upstream_rejected"


class LLMGatewayAuthFailedError(AppError):
    """The gateway credentials are invalid, expired, or revoked
    (`invalid_api_key`, `api_key_expired`, `api_key_revoked`) — a deployment
    problem a retry cannot fix."""

    status_code = 503
    code = "llm_gateway_auth_failed"


class ModelNotAllowedError(AppError):
    """The API key may not use the requested model (`model_not_allowed`)."""

    status_code = 403
    code = "model_not_allowed"


class UpstreamUnavailableError(AppError):
    """The gateway has no healthy upstream right now (`upstream_unavailable`,
    `no_route_available`, `limiter_unavailable`) — transient."""

    status_code = 503
    code = "upstream_unavailable"


class UpstreamTimeoutError(AppError):
    """The gateway's upstream call timed out (`upstream_timeout`)."""

    status_code = 504
    code = "upstream_timeout"


class EmbeddingDimMismatchError(AppError):
    """The gateway's embedding width does not match the configured dimension
    (`embedding_dim_mismatch`) — configuration drift a retry cannot fix."""

    status_code = 502
    code = "embedding_dim_mismatch"


class MCPToolError(AppError):
    status_code = 502
    code = "mcp_tool_failed"


class SearchIndexError(AppError):
    status_code = 502
    code = "search_index_error"


class ChatUnavailableError(AppError):
    """Chat cannot run at all (e.g. no provider API key) — unlike search there
    is no non-LLM fallback, so it fails fast with a clean envelope."""

    status_code = 503
    code = "chat_unavailable"


class TenantUnavailableError(AppError):
    """The request's tenant scope cannot be resolved (e.g. the configured
    default tenant is missing because migrations have not run) — a deployment
    problem, not a caller error, so it fails fast with a clean envelope."""

    status_code = 503
    code = "tenant_unavailable"


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the one envelope shape used by every error response."""
    # jsonable_encoder keeps the envelope renderable when details carry
    # non-JSON types (UUID, datetime, ...) — otherwise the error handler
    # itself would crash and mask the real error with a 500.
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": jsonable_encoder(details or {}),
            }
        },
        headers=headers,
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("app_error", code=exc.code, path=request.url.path, detail=exc.message)
    headers: dict[str, str] | None = None
    if exc.retry_after_seconds is not None:
        # RFC 9110 delay-seconds: whole seconds, rounded up. Body unchanged.
        headers = {"Retry-After": str(math.ceil(exc.retry_after_seconds))}
    return error_response(exc.status_code, exc.code, exc.message, exc.details, headers=headers)


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return error_response(
        422,
        "validation_failed",
        "Request validation failed",
        {"errors": jsonable_encoder(exc.errors())},
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code_by_status = {
        404: "not_found",
        405: "method_not_allowed",
    }
    code = code_by_status.get(exc.status_code, f"http_{exc.status_code}")
    message: str
    details: dict[str, Any]
    if isinstance(exc.detail, dict):
        message, details = "Request failed", exc.detail
    elif isinstance(exc.detail, str):
        message, details = exc.detail, {}
    else:
        message, details = "Request failed", {}
    return error_response(exc.status_code, code, message, details)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    log = logger.bind(
        request_id=request_id,
        path=request.url.path,
    )
    log.exception("unhandled_exception")
    # Generic message only: internals stay in logs, never in the response.
    response = error_response(500, "internal_error", "Internal server error")
    # This handler runs in Starlette's ServerErrorMiddleware, *outside* the
    # request-id middleware, so the header echo must happen here — the
    # middleware's send-wrapper is never called on this path.
    if request_id is not None:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def register_exception_handlers(app: FastAPI) -> None:
    """Install all handlers so every error leaves the app in one envelope shape."""
    # Starlette dispatches each handler with the exact class it is registered
    # for, so the handlers' narrower parameter types are safe at runtime; the
    # cast only bridges starlette's contravariant `ExceptionHandler` alias.
    app.add_exception_handler(AppError, cast(ExceptionHandler, app_error_handler))
    app.add_exception_handler(
        RequestValidationError, cast(ExceptionHandler, request_validation_error_handler)
    )
    app.add_exception_handler(
        StarletteHTTPException, cast(ExceptionHandler, http_exception_handler)
    )
    app.add_exception_handler(Exception, cast(ExceptionHandler, unhandled_exception_handler))
