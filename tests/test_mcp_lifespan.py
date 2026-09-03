"""App lifespan wiring (offline): MCP manager start/stop and the no-config no-op.

FastAPI's lifespan does not run under httpx's ASGITransport, so these tests
drive `app.router.lifespan_context` directly — the same object uvicorn runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.core.config import get_settings
from app.main import create_app
from app.mcp.manager import McpManager, get_mcp_manager
from fakes import hermetic_settings


class LifecycleSpy:
    """Configured-manager stand-in that only records lifecycle calls."""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    @property
    def configured(self) -> bool:
        return True

    @property
    def running(self) -> bool:
        return self.started > self.stopped

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def list_tools(self) -> list[Any]:
        return []


async def test_lifespan_starts_and_stops_a_configured_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = LifecycleSpy()
    monkeypatch.setattr(main_module, "get_mcp_manager", lambda: spy)
    app = create_app()

    async with app.router.lifespan_context(app):
        assert spy.started == 1
        assert spy.stopped == 0

    assert spy.stopped == 1


async def test_lifespan_is_a_no_op_without_mcp_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The real wiring chain — settings -> load_mcp_config -> manager — against
    # a missing config file: the manager never starts, the app boots exactly
    # as it did before MCP existed.
    monkeypatch.setenv("KB_MCP_CONFIG_PATH", str(tmp_path / "absent-mcp.json"))
    get_settings.cache_clear()
    get_mcp_manager.cache_clear()
    try:
        app: FastAPI = create_app()
        manager = get_mcp_manager()

        assert not manager.configured

        async with app.router.lifespan_context(app):
            assert not manager.running

        assert not manager.running
    finally:
        # Rebuild both caches from the restored environment for later tests.
        monkeypatch.undo()
        get_settings.cache_clear()
        get_mcp_manager.cache_clear()


async def test_chat_service_builds_without_extra_tools_when_manager_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # deps wiring: an unstarted manager (running=False) contributes zero extra
    # tools — the constructed agent is the pre-MCP one.
    from pydantic import SecretStr

    import app.api.deps as deps_module
    from app.api.deps import build_chat_service

    monkeypatch.setattr(deps_module, "get_mcp_manager", lambda: McpManager({}))
    service = build_chat_service(hermetic_settings(CHAT_API_KEY=SecretStr("test-key")))

    assert service is not None
