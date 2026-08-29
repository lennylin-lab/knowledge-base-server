"""MCP manager lifecycle (offline): in-process servers, no subprocess/network.

The injected opener hands the SDK's high-level `Client` an `MCPServer` running
in-process — the test pattern the SDK itself documents — so `start`/`stop`,
discovery, and `call_tool` are exercised against the real client/server
protocol stack without spawning anything.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from structlog.testing import capture_logs

import app.mcp.manager as manager_module
from app.core.exceptions import MCPToolError
from app.mcp.manager import McpManager, McpServerConfig

ConfigDict = dict[str, McpServerConfig]


def _server(name: str, *, with_boom: bool = True, with_slow: bool = False) -> MCPServer[Any]:
    """An in-process server with one real tool (plus failure modes on demand)."""
    server: MCPServer[Any] = MCPServer(name)

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    if with_boom:

        @server.tool()
        def boom() -> str:
            """Always fails."""
            raise RuntimeError("kaboom")

    if with_slow:

        @server.tool()
        def slow() -> str:
            """Sleeps past any sane call timeout."""
            import time

            time.sleep(0.5)
            return "finally"

    return server


def _memory_opener(
    servers: dict[str, Any], *, fail_keys: set[str] = frozenset()
) -> manager_module.ConnectionOpener:
    """Opener dispatching on the config's own transport key (command or url)."""

    def opener(config: McpServerConfig) -> AbstractAsyncContextManager[Client]:
        key = config.command or config.url or ""
        if key in fail_keys:
            raise ConnectionRefusedError(f"no server at {key}")
        return Client(servers[key])

    return opener


def _configs(**entries: str) -> ConfigDict:
    """Configs keyed by name; each value is a stdio command or an http url."""
    return {
        name: (
            McpServerConfig(command=transport)
            if transport.startswith("cmd:")
            else McpServerConfig(url=transport)
        )
        for name, transport in entries.items()
    }


async def _started_manager(
    servers: dict[str, Any], configs: ConfigDict, **opener_kwargs: Any
) -> McpManager:
    manager = McpManager(configs, open_connection=_memory_opener(servers, **opener_kwargs))
    await manager.start()
    return manager


async def test_start_discovers_tools_across_servers() -> None:
    servers = {"cmd:alpha": _server("alpha"), "https://beta/mcp": _server("beta")}
    manager = await _started_manager(servers, _configs(alpha="cmd:alpha", beta="https://beta/mcp"))
    try:
        tools = manager.list_tools()
        assert [(tool.server, tool.name) for tool in tools] == [
            ("alpha", "add"),
            ("alpha", "boom"),
            ("beta", "add"),
            ("beta", "boom"),
        ]
        add = tools[0]
        assert add.description == "Add two integers."
        assert add.input_schema["properties"].keys() == {"a", "b"}
        assert add.input_schema["required"] == ["a", "b"]
    finally:
        await manager.stop()


async def test_start_logs_server_started_with_transport_and_tool_count() -> None:
    servers = {"cmd:alpha": _server("alpha")}
    manager = McpManager(_configs(alpha="cmd:alpha"), open_connection=_memory_opener(servers))
    with capture_logs() as logs:
        await manager.start()
    try:
        started = next(entry for entry in logs if entry["event"] == "mcp_server_started")
        assert started["server"] == "alpha"
        assert started["transport"] == "stdio"
        assert started["tool_count"] == 2
    finally:
        await manager.stop()


async def test_http_transport_is_logged_as_http() -> None:
    servers = {"https://beta/mcp": _server("beta")}
    manager = McpManager(_configs(beta="https://beta/mcp"), open_connection=_memory_opener(servers))
    with capture_logs() as logs:
        await manager.start()
    try:
        started = next(entry for entry in logs if entry["event"] == "mcp_server_started")
        assert started["transport"] == "http"
    finally:
        await manager.stop()


async def test_call_tool_round_trips_arguments_and_result() -> None:
    manager = await _started_manager({"cmd:alpha": _server("alpha")}, _configs(alpha="cmd:alpha"))
    try:
        result = await manager.call_tool("alpha", "add", {"a": 2, "b": 3})

        assert result.text == "5"
        assert result.structured == {"result": 5}
    finally:
        await manager.stop()


async def test_one_dead_server_does_not_block_the_healthy_one() -> None:
    servers = {"cmd:alpha": _server("alpha"), "https://beta/mcp": _server("beta")}
    manager = McpManager(
        _configs(alpha="cmd:alpha", beta="https://beta/mcp"),
        open_connection=_memory_opener(servers, fail_keys={"cmd:alpha"}),
    )
    with capture_logs() as logs:
        await manager.start()
    try:
        # Only the healthy server's tools registered.
        assert [tool.server for tool in manager.list_tools()] == ["beta", "beta"]
        failed = next(entry for entry in logs if entry["event"] == "mcp_server_failed")
        assert failed["server"] == "alpha"
        assert failed["error_class"] == "ConnectionRefusedError"
        # The healthy server remains fully callable.
        result = await manager.call_tool("beta", "add", {"a": 1, "b": 1})
        assert result.text == "2"
    finally:
        await manager.stop()


async def test_stop_closes_connections_and_resets_state() -> None:
    manager = await _started_manager({"cmd:alpha": _server("alpha")}, _configs(alpha="cmd:alpha"))
    await manager.stop()

    assert not manager.running
    assert manager.list_tools() == []
    with pytest.raises(MCPToolError):
        await manager.call_tool("alpha", "add", {"a": 1, "b": 1})


async def test_call_tool_on_unknown_server_raises() -> None:
    manager = await _started_manager({"cmd:alpha": _server("alpha")}, _configs(alpha="cmd:alpha"))
    try:
        with pytest.raises(MCPToolError) as excinfo:
            await manager.call_tool("ghost", "add", {"a": 1, "b": 1})

        assert excinfo.value.details["server"] == "ghost"
        assert excinfo.value.details["tool"] == "add"
    finally:
        await manager.stop()


async def test_tool_side_error_raises_with_message_detail() -> None:
    manager = await _started_manager({"cmd:alpha": _server("alpha")}, _configs(alpha="cmd:alpha"))
    try:
        with pytest.raises(MCPToolError) as excinfo:
            await manager.call_tool("alpha", "boom", {})

        details = excinfo.value.details
        assert details["server"] == "alpha"
        assert details["tool"] == "boom"
        assert details["message"]  # the server's own error text, for logs only
    finally:
        await manager.stop()


async def test_unconfigured_manager_is_a_no_op() -> None:
    manager = McpManager({})

    assert not manager.configured
    assert not manager.running

    await manager.start()
    try:
        assert manager.running
        assert manager.list_tools() == []
    finally:
        await manager.stop()

    assert not manager.running


async def test_call_tool_timeout_maps_to_mcp_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shrink the module constant so the timeout fires in milliseconds, not
    # the production 30s; the manager reads it at call time.
    monkeypatch.setattr(manager_module, "_CALL_TIMEOUT_S", 0.05)
    manager = await _started_manager(
        {"cmd:alpha": _server("alpha", with_boom=False, with_slow=True)},
        _configs(alpha="cmd:alpha"),
    )
    try:
        with pytest.raises(MCPToolError) as excinfo:
            await manager.call_tool("alpha", "slow", {})

        assert "timed out" in excinfo.value.message
    finally:
        await manager.stop()


async def test_lifespan_style_start_stop_cycle_leaves_no_tools() -> None:
    servers = {"cmd:alpha": _server("alpha")}
    manager = McpManager(_configs(alpha="cmd:alpha"), open_connection=_memory_opener(servers))

    async with asyncio.timeout(5):
        await manager.start()
        assert manager.list_tools()
        await manager.stop()

    assert manager.list_tools() == []


def test_error_class_unwraps_single_member_exception_groups() -> None:
    # anyio task groups wrap transport failures in ExceptionGroup; the log
    # must name the informative inner class, not the wrapper.
    single = ExceptionGroup("transport", [RuntimeError("spawn failed")])
    multi = ExceptionGroup("transport", [ValueError("a"), KeyError("b")])

    assert manager_module._error_class(single) == "RuntimeError"
    assert manager_module._error_class(ValueError("plain")) == "ValueError"
    # Multi-member groups keep the wrapper name — no arbitrary inner pick.
    assert manager_module._error_class(multi) == "ExceptionGroup"
