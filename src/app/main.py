"""Application factory; no business logic lives here."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging


def create_app() -> FastAPI:
    """Build the app: settings -> logging -> middleware -> handlers -> routes."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    app = FastAPI(title="knowledge-base-server", version="0.1.0")
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    app.include_router(api_v1_router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    return app


app = create_app()
