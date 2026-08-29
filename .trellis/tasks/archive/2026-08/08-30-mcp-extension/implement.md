# Implement: MCP Extension + QA Integration

Ordered checklist. Gates after each group; single commit at the end
(no schema change).

## M1 — Config + manager

- [ ] `Settings.MCP_CONFIG_PATH` (default "mcp.json")
- [ ] `mcp/manager.py`: `McpServerConfig` + `load_mcp_config`;
      `McpManager` with injectable `ConnectionOpener`, start/stop,
      list_tools, call_tool, MCPToolError mapping, timeout constants
- [ ] `mcp.json.example` (context7 stdio + websearch http);
      `.gitignore` += `mcp.json`
- [ ] `tests/test_mcp_config.py`, `tests/test_mcp_manager.py`
      (FastMCP in-memory opener — verify the SDK's memory-session helper
      name in the installed mcp version)
- [ ] Validation: ruff + format + mypy; targeted pytest

## M2 — Tool wrapping + app lifecycle

- [ ] `mcp/tools.py`: `build_agent_tools` (per-tool Tools if pydantic-ai
      takes external schemas, else documented dispatcher fallback);
      fail→error-string policy + mcp_tool_called/failed logs
- [ ] `main.py` lifespan: manager.start/stop (no-op when unconfigured)
- [ ] `tests/test_mcp_tools.py`
- [ ] Validation: ruff + format + mypy; app boots with and without
      mcp.json present

## M3 — QA integration + tests

- [ ] `agents/qa.py`: `build_qa_agent(model, extra_tools=())`
- [ ] `agents/prompts/qa.md`: external-tool rules (external ≠ bracket
      citations; prefer KB)
- [ ] `api/deps.py` chat wiring: tools from manager snapshot
- [ ] `tests/test_chat_service.py` extensions (MCP call → done; failure
      → still done); optional `live_mcp` smoke (marker registered,
      deselected)
- [ ] Validation: full `uv run pytest` (compose up — MCP suites are
      offline); offline spot check; full gates

## Review gates

- trellis-check dispatch after M3: five spec files, PRD acceptance sweep,
  layering (mcp imports core only; no subprocess/network in default
  tests), gates re-run.

## Rollback

- Single revert; no migration; chat behavior unchanged when unconfigured.
