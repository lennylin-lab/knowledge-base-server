# MCP Extension Mechanism + QAAgent Integration

## Goal

The knowledge base's extension mechanism: connect external MCP (Model
Context Protocol) servers — Context7 (library docs) and Web Search as the
configured examples — discover their tools, and register those tools into
the QAAgent so chat answers can draw on external knowledge beyond the KB.

Two user decisions shape scope: (1) build the mechanism AND wire it into
QA this task; (2) server configuration lives in an `mcp.json` file whose
path comes from Settings.

## Requirements

1. **Config** (`mcp.json`, path via `KB_MCP_CONFIG_PATH`, default
   `mcp.json` at repo root): Claude-Desktop-style shape —
   `{"mcpServers": {"<name>": {"command", "args", "env"} | {"url"}}}`;
   stdio (`command`) and HTTP (`url`) transports. Committed
   `mcp.json.example` with Context7 + a web-search entry;
   `mcp.json` itself is gitignored (may hold real keys in `env`).
   Missing file ⇒ empty config (info log); malformed file ⇒ loud
   startup validation error.
2. **Manager** (`mcp/manager.py`): per-server connect via the official
   `mcp` SDK client (stdio subprocess / streamable HTTP), tool discovery
   after connect (`list_tools`), `call_tool(server, tool, arguments)`
   passing through structured results. One server failing to connect
   must not block the others (warn `mcp_server_failed`, continue).
   `start()`/`stop()` lifecycle; transport opening is injectable so
   tests run in-memory sessions instead of subprocesses/network.
3. **Tool wrapping** (`mcp/tools.py`): each discovered MCP tool becomes
   an agent-registerable function — name `mcp_{server}_{tool}`
   (collision-safe), description from the server's tool description,
   arguments per the MCP input schema (verify what pydantic-ai's Tool
   API supports for external schemas; documented fallback: a single
   `mcp_tool(server, tool, arguments)` dispatcher). A failing tool call
   logs `mcp_tool_failed` and returns a concise error string to the
   model — the agent degrades gracefully; it must NOT abort the chat
   run.
4. **App lifecycle** (`main.py`): FastAPI lifespan starts the manager at
   startup (if config non-empty) and stops it at shutdown. No MCP config
   ⇒ zero behavior change anywhere (existing suite stays green
   unchanged).
5. **QA integration**: `build_qa_agent` accepts extra tools; the chat
   wiring passes the manager's wrapped tools when available. Prompt
   (`agents/prompts/qa.md`) gains rules for external tools: they
   complement `search_knowledge`; KB citations stay bracket-numbered,
   external-tool findings are labeled as external (no fake KB source
   entries); prefer KB when both suffice.
6. **Logging**: `mcp_server_started` (server, transport, tool_count) /
   `mcp_server_failed` (server, error_class) at startup;
   `mcp_tool_called` (tool, server, latency_ms) / `mcp_tool_failed`
   (tool, error_class) per call — never full tool payloads at `info`.
7. **Tests**: config parsing units (offline); manager lifecycle +
   discovery + call over an in-memory fake MCP server (FastMCP via
   memory streams — no subprocess, no network); tool wrapping units;
   chat integration with FunctionModel scripting an MCP tool call
   through a fake manager; optional real-server smoke under a
   `live_mcp` marker (deselected by default, like `live_llm`).

## Out of Scope

- MCP *server* implementation (this repo is a client only)
- OAuth/authenticated MCP servers, tool allowlists/permissions UI
- Registering MCP tools into the other three agents (don't exist yet)
- MCP tool results persisted into the KB or cited as first-class sources
- Hot-reload of mcp.json (restart to apply — documented)

## Acceptance Criteria

- [ ] `mcp.json.example` committed with Context7 + web-search entries;
      `mcp.json` gitignored; missing file ⇒ empty manager, app boots
      identically to today
- [ ] Valid config with one stdio + one HTTP entry parses; malformed
      JSON/schema ⇒ startup `ValidationError` with a clear message
- [ ] Manager over an in-memory fake server: start → list_tools sees the
      fake's tools → call_tool round-trips arguments and result (tested)
- [ ] One unreachable server among two: the healthy one's tools still
      register; `mcp_server_failed` warning logged (tested with
      injected transports)
- [ ] Wrapped tool: name/description mapping correct; a raising tool
      call returns an error string to the model and the chat run
      continues to a normal `done` (FunctionModel test)
- [ ] Chat integration: FunctionModel scripts a call to an MCP tool
      (fake manager), the answer streams afterwards; `tool_calls`
      counts include it
- [ ] No mcp.json: full existing suite green with zero MCP-related
      changes in behavior (regression check)
- [ ] Offline: default `uv run pytest` runs no subprocess and no
      network; live smoke only under `live_mcp`
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
