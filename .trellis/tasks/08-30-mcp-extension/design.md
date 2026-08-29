# Design: MCP Extension + QA Integration

## Config

`Settings.MCP_CONFIG_PATH: str = "mcp.json"` (env `KB_MCP_CONFIG_PATH`).

```json
{
  "mcpServers": {
    "context7": { "command": "npx", "args": ["-y", "@upstash/context7-mcp"] },
    "websearch": { "url": "https://mcp.example/search/mcp" }
  }
}
```

- `McpServerConfig(BaseModel)`: `command: str | None`, `args: list[str] = []`,
  `env: dict[str, str] = {}`, `url: str | None`; model validator requires
  exactly one of (`command`, `url`). `env` values are secrets — never
  logged (log key names only, or nothing).
- `load_mcp_config(path: Path) -> dict[str, McpServerConfig]`:
  missing file → `{}` (info `mcp_config_missing`); unreadable/invalid
  JSON/schema → raise `ValidationError` (config errors are loud, at
  startup, not per-request).
- Committed: `mcp.json.example`; `.gitignore` gains `mcp.json`.

## Manager (`mcp/manager.py`)

```python
class McpManager:
    def __init__(self, servers: dict[str, McpServerConfig],
                 open_connection: ConnectionOpener = _default_opener): ...
    async def start(self) -> None          # connect each server sequentially
    async def stop(self) -> None           # graceful: close all, swallow+log close errors
    def list_tools(self) -> list[McpToolInfo]      # aggregated snapshot (post-start)
    async def call_tool(self, server: str, tool: str, arguments: dict) -> McpToolResult
    @property
    def running(self) -> bool
```

- Transport opening is an injected async-contextmanager factory
  `ConnectionOpener = Callable[[McpServerConfig], AbstractAsyncContextManager[ClientSession]]`;
  the default dispatches stdio (`mcp.client.stdio.stdio_client` with
  `StdioServerParameters`) vs HTTP (`mcp.client.streamablehttp.
  streamablehttp_client`). The normalization (streams → `ClientSession`)
  lives in one private helper per transport inside the default opener —
  tests inject their own opener holding a FastMCP in-memory session.
- Per-server state: `None` (not started) | live session | failed. A
  connect/list failure logs `mcp_server_failed` (server, error_class)
  and marks that server failed; `start()` continues with the rest.
- `call_tool` on a failed/unknown server raises `MCPToolError`
  (`details={"server", "tool"}`); a tool-side error raises
  `MCPToolError` with `isError`/message detail. Result normalization:
  MCP content blocks → `McpToolResult(text: str, structured: dict | None)`
  (text block concatenated; pass structured through per spec).
- Timeouts: constants in this module (`_CALL_TIMEOUT_S = 30`,
  `_CONNECT_TIMEOUT_S = 10`) — same convention as `llm/` provider
  constants (quality-guidelines rule).

Singleton + app lifecycle (mirrors `get_shared_es_client` pattern):

```python
# mcp/manager.py
@lru_cache
def get_mcp_manager() -> McpManager: ...   # built from Settings + config file

# main.py — create_app gains a lifespan:
async with AsyncExitStack() as stack:      # pattern sketch; actual shape may differ
    await manager.start()                  # if servers configured
    yield
    await manager.stop()
```

`main.py` owns startup/shutdown (it already owns app composition);
`mcp/` imports stay `core/`-only (spec layering: mcp must not import
api/services/agents/models — manager is pure infrastructure).

## Tool wrapping (`mcp/tools.py`)

```python
@dataclass(frozen=True)
class McpToolInfo:
    server: str; name: str; description: str; input_schema: dict

def build_agent_tools(manager: McpManager, infos: list[McpToolInfo]) -> list[Tool]:
```

- Preferred: one pydantic-ai `Tool` per MCP tool. Verify what the
  installed pydantic-ai supports for externally-provided JSON schemas
  (`ToolDefinition`/`parameters_json_schema` or equivalent). If the
  framework only derives schemas from function signatures, use the
  documented fallback: **one dispatcher tool**
  `mcp_tool(server: str, tool: str, arguments: dict[str, Any]) -> str`
  with the available servers/tools enumerated in its docstring. Models
  handle dispatcher-style tools well; do not hand-roll schema-to-
  signature codegen. Note which path was taken as a deviation/decision.
- Names are prefixed `mcp_{server}_{tool}` (namespace collisions across
  servers); description = MCP tool description (truncated to a sane
  bound, e.g. 1024 chars).
- The wrapper catches `MCPToolError` (and transport exceptions) itself:
  logs `mcp_tool_failed` (tool, server, error_class) and returns
  `"tool <name> failed: <error_class>"` to the model — **the chat run
  never aborts because an external tool failed**. `MCPToolError` still
  exists for direct (non-agent) callers; the wrapper is the agent-side
  policy.
- `mcp_tool_called` info event (tool, server, latency_ms) on success.

## QA integration

- `build_qa_agent(model, extra_tools: Sequence[Tool] = ())` — append
  after `search_knowledge`. Default empty ⇒ byte-identical agent
  behavior (regression-safe).
- `api/deps.py` `build_chat_service`: `tools = build_agent_tools(manager,
  manager.list_tools())` when `manager.running and manager.list_tools()`
  non-empty; pass into `build_qa_agent`. Manager is process-lifetime —
  the cached chat service captures the startup tool snapshot; restart
  applies config changes (documented, no hot reload).
- Prompt (`agents/prompts/qa.md`) additions: external MCP tools
  complement `search_knowledge`; bracket-number citations are KB sources
  ONLY; label external-tool findings as external (no fake `[n]`); prefer
  KB content when both answer the question.
- `sources` SSE event stays KB-retrieval-only (by design); `tool_calls`
  in `done`/logs counts MCP calls too (already generic).

## Error handling & logging

| Case | Behavior |
|---|---|
| mcp.json malformed | startup `ValidationError` (app refuses to boot) |
| Server unreachable at start | warn `mcp_server_failed`, server skipped, others fine |
| Tool call fails during chat | warn `mcp_tool_failed`; error string to model; run continues |
| `call_tool` direct misuse (unknown server/tool) | `MCPToolError` 502 (envelope) if ever exposed via HTTP |
| stop() close error | log, swallow — shutdown must complete |

## Tests

| Suite | World | Cases |
|---|---|---|
| `test_mcp_config.py` | offline | valid stdio+http parse; exactly-one-transport validation; missing file → {}; malformed JSON → ValidationError; example file itself parses |
| `test_mcp_manager.py` | offline, injected opener w/ FastMCP memory session | start/list_tools discovery; call_tool round-trip; one-dead-server isolation; stop() closes; call on unknown server → MCPToolError |
| `test_mcp_tools.py` | offline, fake manager | name/description mapping; raising call → error string + `mcp_tool_failed` log, no exception |
| `test_chat_service.py` (extend) | offline | FunctionModel scripts MCP-tool call then answer → normal done; tool failure mid-run still reaches done (no terminal error) |
| live smoke | `live_mcp` marker | real `mcp.json` server list_tools (manual/opt-in) |

- `live_mcp` marker registered in pyproject, deselected by default
  (mirror `live_llm`); the fake-server suites carry no marker.
- Fake opener: FastMCP server instance + `create_connected_server_and_client_session` (or the SDK's current memory-stream helper — verify exact helper name in the installed version) wrapped in the injectable opener shape.

## Tradeoffs / Rejected

- **Eager connect at startup** (chosen) vs lazy per-first-call: startup
  gives one loud failure point and a stable tool snapshot; lazy hides
  breakage into the first chat. Restart-to-reload is acceptable MVP.
- **Module-level manager singleton** (chosen) vs app.state + Request
  deps: consistent with `get_shared_es_client`; lifespan owns its
  lifecycle explicitly, so the singleton is not hidden global mutation.
- **Per-tool Tool objects** (preferred) vs single dispatcher: per-tool is
  nicer for the model; dispatcher is the robust fallback if pydantic-ai
  can't take external schemas. Either way the wrapper policy (fail→error
  string) is identical.
- **Hot reload** (rejected): config watch adds lifecycle complexity for
  a single-user MVP.
- **MCP results as citations** (rejected): external sources are not KB
  documents; faking them into `sources` would corrupt the citation
  contract.

## Rollback

Single commit, no schema change. Revert restores chat exactly (extra_tools
defaults empty); `mcp/` additions are additive; `main.py` lifespan
shrinks back. `mcp.json` is untracked user config.
