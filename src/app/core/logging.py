"""structlog configuration and request-scoped logging middleware."""

from __future__ import annotations

import logging
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def configure_logging(log_level: str, log_format: str) -> None:
    """Configure structlog once at app startup; JSON in prod, console in dev."""
    level = logging.getLevelName(log_level.upper())
    if not isinstance(level, int):  # unknown level name -> fall back to INFO
        level = logging.INFO

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer()
            if log_format == "console"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


class RequestIdMiddleware:
    """Pure ASGI middleware binding `request_id`/`path` to structlog contextvars.

    Every log line emitted inside the request carries them automatically, and
    the id is echoed back as the `X-Request-ID` response header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, path=scope["path"])

        # Expose the id on scope state so the outermost 500 handler (which
        # Starlette runs outside this middleware) can still log with context.
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        response_status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=response_status,
                duration_ms=duration_ms,
            )
            structlog.contextvars.clear_contextvars()
