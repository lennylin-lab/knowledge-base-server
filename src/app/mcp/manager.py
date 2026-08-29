"""MCP server connections: config parsing, lifecycle, discovery, tool calls.

Transport opening is an injected async-context-manager factory so tests run
against in-process servers instead of subprocesses or network. The default
opener dispatches on the config's transport: stdio subprocess (via
`StdioServerParameters`) or streamable HTTP (via the URL form of the SDK's
high-level client).

Note on the SDK shape (mcp 2.x): the design sketch named
`mcp.client.streamablehttp.streamablehttp_client`; the installed SDK exposes
`mcp.client.streamable_http` and a high-level `Client` that enters the
transport AND performs the handshake in `__aenter__` (`ClientSession` no
longer initializes itself). The manager therefore holds `Client` objects —
one injection point instead of hand-wired streams.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

import structlog
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)

from app.core.config import get_settings
from app.core.exceptions import MCPToolError, ValidationError

logger = structlog.get_logger(__name__)

# Provider-layer style constants (same convention as `llm/`): one place for
# the timeouts the MCP client uses, not per-call literals.
_CONNECT_TIMEOUT_S = 10.0
_CALL_TIMEOUT_S = 30.0

# Transport label for `mcp_server_started` logs; derived from the config shape.
_TRANSPORT_STDIO = "stdio"
_TRANSPORT_HTTP = "http"


class McpServerConfig(BaseModel):
    """One `mcpServers` entry: a stdio command or a streamable-HTTP url.

    `env` values are secrets — they are passed to the subprocess but never
    logged (logging-guidelines).
    """

    model_config = ConfigDict(extra="forbid")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None

    @property
    def transport(self) -> str:
        """Transport label for logging; exactly one field is set (validated)."""
        return _TRANSPORT_STDIO if self.command is not None else _TRANSPORT_HTTP

    @model_validator(mode="after")
    def _exactly_one_transport(self) -> Self:
        if (self.command is None) == (self.url is None):
            raise ValueError(
                "each mcpServers entry must set exactly one of "
                "'command' (stdio) or 'url' (streamable HTTP), not both and not neither"
            )
        return self


class McpConfigFile(BaseModel):
    """Root of a Claude-Desktop-style config file; unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(alias="mcpServers", default_factory=dict)


@dataclass(frozen=True)
class McpToolInfo:
    """One discovered external tool, snapshotted at connect time."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpToolResult:
    """Normalized tool result: concatenated text blocks plus structured data."""

    text: str
    structured: dict[str, Any] | None


def load_mcp_config(path: Path) -> dict[str, McpServerConfig]:
    """Parse the MCP config file; missing file is an empty config, not an error.

    Malformed JSON or schema violations raise the domain `ValidationError`
    loudly at startup (config errors are never deferred to request time).
    Pydantic's raw error payloads echo input values — which would leak `env`
    secrets into logs — so details carry only locations, messages, and types.
    """
    if not path.exists():
        logger.info("mcp_config_missing", path=str(path))
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        parsed = McpConfigFile.model_validate(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"MCP config file is not valid JSON: {path}",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except PydanticValidationError as exc:
        raise ValidationError(
            f"MCP config file is invalid: {path}",
            details={"path": str(path), "errors": _scrubbed_errors(exc)},
        ) from exc
    return dict(parsed.mcp_servers)


def _scrubbed_errors(exc: PydanticValidationError) -> list[dict[str, Any]]:
    """Pydantic errors without `input`/`ctx` — `env` values must not leak."""
    return [
        {
            "loc": ".".join(str(item) for item in error["loc"]),
            "msg": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]


ConnectionOpener = Callable[[McpServerConfig], AbstractAsyncContextManager[Client]]
"""Opens one live client connection; injectable so tests avoid subprocess/network."""


def _default_opener(config: McpServerConfig) -> AbstractAsyncContextManager[Client]:
    """Production opener: stdio subprocess or streamable HTTP, per config."""
    if config.command is not None:
        params = StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=dict(config.env) or None,
        )
        return Client(params, read_timeout_seconds=_CALL_TIMEOUT_S)
    # Exactly one of command/url is set (model validator), so url is safe here.
    return Client(config.url or "", read_timeout_seconds=_CALL_TIMEOUT_S)


@dataclass
class _ServerConnection:
    """One live server: the stack owning its transport plus its tool snapshot."""

    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    client: Client | None = None
    tools: list[McpToolInfo] = field(default_factory=list)


class McpManager:
    """Owns every configured MCP server connection for the process lifetime.

    One server failing to connect never blocks the others: the failure is
    logged (`mcp_server_failed`) and that server is simply absent from the
    tool snapshot.
    """

    def __init__(
        self,
        servers: dict[str, McpServerConfig],
        open_connection: ConnectionOpener = _default_opener,
    ) -> None:
        self._servers = dict(servers)
        self._open_connection = open_connection
        self._connections: dict[str, _ServerConnection] = {}
        self._started = False

    @property
    def configured(self) -> bool:
        """Whether any server is configured; drives the app-lifespan no-op."""
        return bool(self._servers)

    @property
    def running(self) -> bool:
        """True once `start()` has run (even if every server failed to connect)."""
        return self._started

    async def start(self) -> None:
        """Connect and discover tools for each configured server, sequentially.

        Connect + discovery share one timeout so a hung subprocess cannot pin
        startup forever; per-server failures are contained by the loop.
        """
        if self._started:
            return
        self._started = True
        for name, config in self._servers.items():
            try:
                connection = await self._connect_one(name, config)
            except Exception as exc:
                # Containment is the contract: warn with the class only and
                # keep going — the healthy servers' tools still register.
                logger.warning("mcp_server_failed", server=name, error_class=_error_class(exc))
                continue
            self._connections[name] = connection
            logger.info(
                "mcp_server_started",
                server=name,
                transport=config.transport,
                tool_count=len(connection.tools),
            )

    async def stop(self) -> None:
        """Close every live connection; shutdown must complete even if closes fail."""
        for name, connection in self._connections.items():
            try:
                await connection.stack.aclose()
            except Exception as exc:
                logger.warning("mcp_server_stop_failed", server=name, error_class=_error_class(exc))
        self._connections.clear()
        self._started = False

    def list_tools(self) -> list[McpToolInfo]:
        """Aggregated tool snapshot across live servers; empty before `start`."""
        return [info for connection in self._connections.values() for info in connection.tools]

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> McpToolResult:
        """Call one tool on a connected server, normalizing the MCP result.

        Unknown servers, timeouts, and `isError` results raise `MCPToolError`
        with the server and tool in `details`; transport-level exceptions
        propagate for the caller (the agent-side wrapper) to degrade on.
        """
        connection = self._connections.get(server)
        if connection is None or connection.client is None:
            raise MCPToolError(
                f"MCP server '{server}' is not connected",
                details={"server": server, "tool": tool},
            )
        try:
            async with asyncio.timeout(_CALL_TIMEOUT_S):
                result = await connection.client.call_tool(tool, dict(arguments))
        except TimeoutError as exc:
            raise MCPToolError(
                f"MCP tool '{server}.{tool}' timed out",
                details={"server": server, "tool": tool},
            ) from exc
        if result.is_error:
            raise MCPToolError(
                f"MCP tool '{server}.{tool}' reported an error",
                details={
                    "server": server,
                    "tool": tool,
                    "message": _result_text(result) or "no error detail provided",
                },
            )
        return _normalize(result)

    async def _connect_one(self, name: str, config: McpServerConfig) -> _ServerConnection:
        """Open one connection and snapshot its tools, owning the transport stack."""
        connection = _ServerConnection()
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_S):
                connection.client = await connection.stack.enter_async_context(
                    self._open_connection(config)
                )
                connection.tools = [
                    _tool_info(name, tool) for tool in await _list_all_tools(connection.client)
                ]
        except BaseException:
            # Close the half-open connection before propagating so a failed
            # start leaves no orphaned transport behind.
            await connection.stack.aclose()
            raise
        return connection


async def _list_all_tools(client: Client) -> list[Tool]:
    """All tool pages (a server may paginate `tools/list`)."""
    tools: list[Tool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if cursor is None:
            return tools


def _error_class(exc: BaseException) -> str:
    """Error class for logs, unwrapping single-member anyio exception groups.

    The SDK's task groups wrap transport failures (spawn errors, closed
    streams) in `ExceptionGroup`, which would make every failure log read
    identically; the single inner exception is the informative one.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return type(exc).__name__


def _tool_info(server: str, tool: Tool) -> McpToolInfo:
    return McpToolInfo(
        server=server,
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.input_schema),
    )


def _normalize(result: CallToolResult) -> McpToolResult:
    """MCP content blocks to `(text, structured)`; structured passes through."""
    structured = result.structured_content if isinstance(result.structured_content, dict) else None
    return McpToolResult(text=_result_text(result), structured=structured)


def _result_text(result: CallToolResult) -> str:
    """Concatenated text blocks; non-text content (images, audio) is skipped."""
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


@lru_cache(maxsize=1)
def get_mcp_manager() -> McpManager:
    """Process-lifetime manager wired from Settings + the configured mcp.json.

    Restart-to-reload by design: the snapshot (and the chat service's agent
    tools) is taken once at startup; hot reload was explicitly rejected.
    """
    settings = get_settings()
    return McpManager(load_mcp_config(Path(settings.MCP_CONFIG_PATH)))
