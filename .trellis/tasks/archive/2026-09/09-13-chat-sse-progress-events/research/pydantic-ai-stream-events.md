# Research: pydantic-ai streaming events for tool-call progress

## Source

Inspected installed `pydantic-ai` (pyproject: `pydantic-ai-slim[openai]>=1.0`)
via `uv run python3` against `.venv` site-packages.

## Findings

### Current usage

`ChatService.ask` and `WritingService.suggest` call:

```python
async with agent.run_stream(...) as result:
    async for delta in result.stream_text(delta=True, debounce_by=None):
        ...
```

`stream_text` yields **text only** — tool calls and thinking parts are
filtered out.

### Available API

`Agent.run_stream` accepts:

```python
event_stream_handler: EventStreamHandler[AgentDepsT] | None = None
```

Where `EventStreamHandler = Callable[
    [RunContext[AgentDepsT], AsyncIterable[AgentStreamEvent]],
    Awaitable[None],
]`.

Relevant `AgentStreamEvent` variants:

| Event | When |
|-------|------|
| `FunctionToolCallEvent` | Tool about to execute (`part.tool_name`, `part.args`, `tool_call_id`) |
| `FunctionToolResultEvent` | Tool completed (`part.tool_name`, `content`, `tool_call_id`) |
| `PartStartEvent` / `PartDeltaEvent` | Part-level streaming (includes `ThinkingPart` — out of scope) |

`stream_text` and `event_stream_handler` can be used **together** on the
same `run_stream` context — handler receives tool lifecycle events while
`stream_text` continues to yield answer text deltas.

### Implication for design

No need to replace `stream_text` with full part streaming. Bridge pattern:
handler pushes to a pending queue; main loop drains queue each iteration
(same idiom as `SourceCollector` → `SourcesEvent`).
