"""Application factory; no business logic lives here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import close_arq_pool, close_cache
from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestIdMiddleware, configure_logging
from app.mcp.manager import get_mcp_manager


@asynccontextmanager
async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown for process-lifetime components.

    MCP manager: started/stopped when servers are configured (a no-op when
    not, so an MCP-less deployment is byte-identical to before). Shared ARQ
    pool: closed at shutdown when this process routed indexing through
    Redis — also a no-op in BackgroundTasks mode, where the pool never gets
    built. Shared cache client: closed when caching is enabled — a no-op in
    NullCache mode.
    """
    manager = get_mcp_manager()
    if manager.configured:
        await manager.start()
    try:
        yield
    finally:
        if manager.configured:
            await manager.stop()
        await close_arq_pool()
        await close_cache()


def create_app() -> FastAPI:
    """Build the app: settings -> logging -> middleware -> handlers -> routes."""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)

    app = FastAPI(title="knowledge-base-server", version="0.1.0", lifespan=app_lifespan)
    app.add_middleware(RequestIdMiddleware)
    if settings.CORS_ORIGINS:
        # Dev-only convenience: no origins configured = no CORS at all.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    register_exception_handlers(app)

    app.include_router(api_v1_router, prefix="/api/v1")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    return app


app = create_app()
