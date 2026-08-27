"""Domain exception hierarchy and the shared error-envelope handlers."""

from __future__ import annotations

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


# --- LLM / agent stack (see error-handling.md taxonomy) ---


class LLMProviderError(AppError):
    status_code = 502
    code = "llm_provider_error"


class LLMRateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class MCPToolError(AppError):
    status_code = 502
    code = "mcp_tool_failed"


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
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
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.warning("app_error", code=exc.code, path=request.url.path, detail=exc.message)
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


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
