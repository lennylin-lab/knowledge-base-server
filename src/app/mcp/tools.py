"""Wrap discovered MCP tools as pydantic-ai agent tools.

Wrapping path taken (per design verification): one `Tool` per MCP tool.
pydantic-ai 2.35 cannot take an external JSON schema at `Tool(...)` directly,
but its public `prepare` hook swaps the model-visible `ToolDefinition` per
request — so the wrapper stays a generic `(**kwargs)` passthrough while the
model sees the server's own schema verbatim. No signature codegen, and no
dispatcher fallback needed.

The wrapper owns the agent-side failure policy: an external tool error is
logged (`mcp_tool_failed`) and returned to the model as a concise error
string — the chat run never aborts because an external tool failed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import structlog
from pydantic_ai import RunContext
from pydantic_ai.tools import Tool, ToolDefinition

from app.mcp.manager import McpManager, McpToolInfo, _error_class

logger = structlog.get_logger(__name__)

_DESCRIPTION_LIMIT = 1024


@runtime_checkable
class McpCallObserver(Protocol):
    """Per-run sink for external tool-call counts.

    Structural: the QA agent's deps satisfy it by having this method, so
    `mcp/` counts calls without importing `agents/` (layering rule).
    """

    def record_external_tool_call(self) -> None: ...


def build_agent_tools(manager: McpManager, infos: Sequence[McpToolInfo]) -> list[Tool[Any]]:
    """One agent tool per discovered MCP tool, named `mcp_{server}_{tool}`.

    The server prefix namespaces tool names across servers; a still-duplicate
    name (e.g. server `a` tool `b_c` vs server `a_b` tool `c`) gets a numeric
    suffix rather than shadowing an earlier tool.
    """
    tools: list[Tool[Any]] = []
    used_names: set[str] = set()
    for info in infos:
        tools.append(
            _build_one(
                manager,
                info,
                _unique_name(f"mcp_{info.server}_{info.name}", used_names),
            )
        )
    return tools


def _unique_name(candidate: str, used: set[str]) -> str:
    """First non-taken variant of `candidate` (`name`, `name_2`, `name_3`, …)."""
    name = candidate
    suffix = 1
    while name in used:
        suffix += 1
        name = f"{candidate}_{suffix}"
    used.add(name)
    return name


def _build_one(manager: McpManager, info: McpToolInfo, name: str) -> Tool[Any]:
    server, tool = info.server, info.name
    description = (info.description or f"MCP tool '{tool}' on server '{server}'.")[
        :_DESCRIPTION_LIMIT
    ]

    async def wrapper(ctx: RunContext[Any], **kwargs: Any) -> str:
        # Per-run counting via the deps protocol (see McpCallObserver). The
        # first parameter is consumed by the framework; an MCP parameter
        # literally named like it would collide — accepted, documented edge.
        observer = ctx.deps
        if isinstance(observer, McpCallObserver):
            observer.record_external_tool_call()
        started = time.perf_counter()
        try:
            result = await manager.call_tool(server, tool, dict(kwargs))
        except Exception as exc:
            # Agent-side policy (design): MCPToolError or any transport
            # failure degrades to a string for the model — never an abort.
            error_class = _error_class(exc)
            logger.warning("mcp_tool_failed", tool=tool, server=server, error_class=error_class)
            return f"tool {name} failed: {error_class}"
        logger.info(
            "mcp_tool_called",
            tool=tool,
            server=server,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result.text or (
            json.dumps(result.structured) if result.structured is not None else ""
        )

    async def prepare(ctx: RunContext[Any], tool_def: ToolDefinition) -> ToolDefinition:
        # The model-visible definition is the server's own schema verbatim;
        # execution still flows through the wrapper's permissive signature.
        return ToolDefinition(
            name=tool_def.name,
            parameters_json_schema=info.input_schema,
            description=description,
        )

    return Tool(wrapper, name=name, description=description, takes_ctx=True, prepare=prepare)
