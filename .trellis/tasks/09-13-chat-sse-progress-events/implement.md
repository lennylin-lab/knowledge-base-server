# Implementation plan — Chat SSE progress events

## Checklist

### 1. Schema & wire mapping

- [ ] Add `StatusPhase`, `StatusEvent`, `ToolCallStartedEvent`,
      `ToolCallFinishedEvent`, `QueryRewrittenEvent` to `schemas/chat.py`.
- [ ] Extend `ChatStreamEvent` union.
- [ ] Register names in `api/v1/endpoints/chat.py::_EVENT_NAMES`.
- [ ] Export new types from any `__init__` re-exports if present.

### 2. Stream bridge helper

- [ ] Add `services/stream_bridge.py` (or equivalent) with:
      - `RunEventBridge` dataclass
      - `on_agent_event` handler mapping pydantic-ai events → typed SSE events
      - `_parse_tool_args(part)` — JSON-decode `part.args` safely
      - `_is_mcp_failure(content)` — prefix check for MCP degrade strings
      - inject `limit` into `search_knowledge` started args from `RunContext.deps`
- [ ] Keep helper free of FastAPI imports; deps typed as `ChatDeps | WritingDeps`.

### 3. ChatService integration

- [ ] Refactor `_rewrite_query` → `_resolve_rewrite` returning `RewriteOutcome`.
- [ ] In `ask`:
      - emit `status(rewriting_query)` when rewrite will run
      - emit `query_rewritten` from outcome when present
      - wire `event_stream_handler=bridge.on_agent_event`
      - drain `bridge.pending_tool_events` alongside sources drain
      - emit `status(generating)` once before first `answer_delta`
- [ ] Preserve carried-sources prelude order (AC5).
- [ ] Preserve error-path: terminal `error`, no escape after first event.

### 4. WritingService integration

- [ ] Mirror bridge + drain loop in `WritingService.suggest`.
- [ ] No rewrite events.

### 5. Tests

- [ ] `tests/test_chat_service.py`:
      - rewrite + tool-call + status order on follow-up turn
      - first turn: no rewrite events; tool-call events when retrieval runs
      - rewrite degrade: no `query_rewritten`, still `done`
      - carried sources: batch before progress events
- [ ] `tests/test_chat_api.py`: wire-level event names + JSON shapes
- [ ] `tests/test_writing_api.py` (or service test): tool-call events
- [ ] MCP soft-failure case: `tool_call_finished.status == failed`, `done` terminal
- [ ] Update `tests/fakes.py` if handler timing needs scripting hooks

### 6. Validation commands

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest tests/test_chat_service.py tests/test_chat_api.py tests/test_writing_api.py -q
uv run pytest -q
```

## Risky files

| File | Why |
|------|-----|
| `services/chat.py` | Core streaming loop; citation order regression |
| `services/agents.py` | Writing mirror |
| `schemas/chat.py` | Union type exhaustiveness |
| `tests/test_chat_service.py` | Order assertions are the safety net |

## Rollback point

After step 1 only: schema compiles but unused — safe to revert as one commit.
After step 3: run full test suite before touching writing.

## Pre-start gates (this task)

- [x] `prd.md` complete with acceptance criteria
- [x] `design.md` complete
- [x] `implement.md` complete
- [x] `implement.jsonl` / `check.jsonl` curated
- [ ] User approves planning summary → then `task.py start`
