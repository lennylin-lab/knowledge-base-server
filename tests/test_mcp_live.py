"""Live MCP smoke (opt-in only): connect to the servers in a real mcp.json.

Run manually with `uv run pytest -m live_mcp`; excluded from the default
suite exactly like `live_llm`. Skips (rather than fails) when no mcp.json is
configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.mcp.manager import McpManager, load_mcp_config

pytestmark = pytest.mark.live_mcp


async def test_real_mcp_servers_connect_and_list_tools() -> None:
    servers = load_mcp_config(Path(get_settings().MCP_CONFIG_PATH))
    if not servers:
        pytest.skip("no mcp.json configured")

    manager = McpManager(servers)
    await manager.start()
    try:
        assert manager.running
        # Tool counts differ per server; the smoke value is that every
        # configured server connected (failures would have logged
        # `mcp_server_failed` and dropped out of the snapshot).
        tools = manager.list_tools()
        assert [tool.server for tool in tools]
    finally:
        await manager.stop()
