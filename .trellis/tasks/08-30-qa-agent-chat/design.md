# Design: QAAgent + SSE Chat

## Request lifecycle

```
POST /api/v1/chat {question, limit?}
  └─ endpoint (thin) → ChatService.ask(question, limit)
       ├─ key check: settings.OPENAI_API_KEY empty ⇒ ChatUnavailableError (503)
       ├─ bind run_id contextvar (uuid4)
       ├─ build ChatDeps(retriever, sources_collector, limit) — per request
       ├─ agent.run_stream(question, deps=deps)   # pydantic-ai
       │    └─ tool: search_knowledge(ctx: RunContext[ChatDeps], query: str)
       │         → Retriever.retrieve(query, limit=ctx.deps.limit)
       │         → collector.append(results); returns numbered context blocks
       └─ yield SSE events as the run progresses
```

SSE events (event name + JSON data):

| event | data | when |
|---|---|---|
| `run_started` | `{run_id, mode}` (mode = retriever mode: hybrid/bm25) | immediately |
| `sources` | `{items: [SearchHit…]}` | after each retrieval tool call returns |
| `answer_delta` | `{text}` | per streamed text part |
| `done` | `{run_id, outcome, tool_calls, latency_ms}` | normal end |
| `error` | `{code, message}` | terminal; stream closes after |

Event names/messages reuse existing shapes: `SearchHit` is
`schemas/search.py`'s; error data matches the envelope's
`{"code", "message"}` pair (SSE cannot change the status code after 200 —
error-handling spec's streaming rule).

## Agent construction (`agents/qa.py`)

```python
@dataclass(slots=True)
class ChatDeps:
    retriever: Retriever
    limit: int
    collector: list[list[SearchHit]]   # per-run retrieval results

qa_agent = Agent(          # module-level, reusable — state flows via deps
    model_factory,         # injected: () -> Model, from llm/models.py
    deps_type=ChatDeps,
    instructions=load_prompt("qa.md"),
    tools=[search_knowledge],
)

async def search_knowledge(ctx: RunContext[ChatDeps], query: str) -> str:
    outcome = await ctx.deps.retriever.retrieve(query, limit=ctx.deps.limit)
    ctx.deps.collector.append(outcome.items)
    # numbered context blocks: [1] title\n<chunk text>… — the prompt binds
    # bracket citations to these numbers; tool returns the blocks verbatim
```

- Model injection: the agent module takes the model (or a factory) as a
  parameter of a builder function (`build_qa_agent(model) -> Agent`), so
  tests pass `FunctionModel` and production passes the Settings-built
  model. No Settings import inside `agents/` beyond types — model
  construction stays in `llm/models.py` (layering: agents may import
  `llm/`).
- Prompt loading: `agents/prompts/qa.md` read once at module import via
  `importlib.resources` (or `Path(__file__)`); wheel packaging needs the
  file included — add hatch `force-include`/package-data in pyproject if
  the default excludes it.
- Instructions content: grounded-answering contract per PRD req 2.

## Model factory (`llm/models.py`)

```python
def get_chat_model(settings: Settings) -> Model:
    return OpenAIChatModel(
        settings.CHAT_MODEL,
        base_url=settings.OPENAI_BASE_URL,
        api_key=settings.OPENAI_API_KEY.get_secret_value(),
    )
```

- Dependency check first: if `pydantic_ai.models.openai` is not importable
  with the current `pydantic-ai-slim` install, switch the dep to
  `pydantic-ai-slim[openai]` (uv add) — the `openai` package itself is
  already a direct dependency.
- This module is the ONLY place constructing chat models; the embeddings
  provider stays in `llm/embeddings.py`.

## Service (`services/chat.py`)

```python
class ChatService:
    def __init__(self, retriever: Retriever, model: Model): ...
    async def ask(self, question: str, *, limit: int = 8) -> AsyncIterator[ChatEvent]
```

- Key check happens in `api/deps.py` construction (build service only
  when key present; otherwise the dependency raises `ChatUnavailableError`,
  new AppError subclass 503 `chat_unavailable` — clean envelope, no
  half-open stream).
- `ask` is an async generator: bind `run_id` (structlog contextvars +
  returned in events), emit `run_started`, stream the agent
  (`async with agent.run_stream(question, deps=deps) as result:` iterate
  `result.stream()` for deltas), flush `sources` events when the
  collector grows (poll after each part or wrap collector with a
  callback — prefer a small `_SourceBuffer` with an `on_append` callback
  set by the service; keeps the agent decoupled), finally `done`.
- `agent_run_started` (agent="qa", question_length — never the text) /
  `agent_run_finished` (outcome, tool_calls, latency_ms, usage tokens if
  available) info events per logging spec.
- Failures: any exception inside the generator ⇒ log once, yield
  `error` event (code from AppError if it is one, else `internal_error`),
  return. The endpoint's EventSourceResponse just forwards; no
  exception may escape `ask` after the first event was yielded.

## Endpoint + deps

- `api/deps.py`: `get_chat_service` — builds `OpenAIChatModel` via
  `llm/models.py` ONLY when key non-empty (else raise
  `ChatUnavailableError`), retriever exactly as the search endpoint
  builds it (shared `get_shared_es_client`, provider-None degradation for
  the retrieval leg — chat works with bm25-only retrieval, that is fine).
- `api/v1/endpoints/chat.py`: `POST /chat`, `ChatRequest`
  (`question: str min_length=1`, `limit: int = Field(8, ge=1, le=20)`),
  returns `EventSourceResponse(service.ask(...))` (sse-starlette). Router
  stays thin — it does not iterate the stream itself.

## Schemas (`schemas/chat.py`)

`ChatRequest` (above), `ChatEvent` variants as typed models serialized by
the endpoint (event name = discriminator). `SourceItems` reuses
`SearchHit` from `schemas/search.py`.

## Error handling

| Failure | Behavior |
|---|---|
| No API key | `ChatUnavailableError` (503 envelope) raised in deps before stream starts |
| Provider failure mid-stream | terminal `error` SSE event, stream closes (spec rule) |
| Retriever ES failure during tool call | `SearchIndexError` propagates out of the tool ⇒ caught by `ask` ⇒ `error` event (code `search_index_error`) — do NOT silently answer ungrounded |
| Retriever vector-leg failure | existing degradation (bm25) — agent keeps working |
| Bad request body | FastAPI 422 envelope (standard) |

## Tests

| Suite | World | Cases |
|---|---|---|
| `test_chat_service.py` | offline, FunctionModel + scripted retriever stub | event order run_started→sources→deltas→done; sources after tool call; mid-stream provider failure ⇒ terminal error then close; run_id bound in logs (capture_logs) |
| `test_chat_api.py` | offline (service with FunctionModel via dependency override) | SSE content-type; event sequence parsed; 422 empty question / bad limit; no-key app ⇒ 503 envelope |
| `test_qa_agent.py` | db+es (seeded via `seed_indexed` corpus fixture) | scripted FunctionModel actually calls `search_knowledge` → real retriever returns seeded hits → collector/sources reflect corpus; agent answers citing [1] |
| live smoke | `live_llm` | real provider one-shot question (manual/CI-opt-in) |

FunctionModel scripting: tool-call part first (`search_knowledge` with a
query), then text parts — pydantic-ai's `FunctionModel` supports both.
Reuse `tests/corpus.py` seeding; add a scripted-model helper to
`tests/fakes.py`.

## Tradeoffs / Rejected

- **Retrieve-then-generate (no tool)** (rejected): the spec's agent
  architecture is tool-based (`tools from rag/retriever.py`); the tool
  pattern also survives the MCP extension unchanged. Cost: one extra
  LLM turn for the tool call.
- **Structured output type for citations** (rejected for v1): validated
  structured output does not stream; inline bracket citations + the
  `sources` event give the same UX while keeping token streaming.
- **Multi-turn memory** (rejected, user decision): stateless first;
  `ChatDeps` is the natural seam to add history later.
- **Per-request Agent construction** (rejected): pydantic-ai agents are
  designed for reuse; per-request state belongs in `deps` (framework
  idiom), which also matches the singleton-model wiring in `llm/models.py`.
- **Chat persistence/audit** (rejected): later with multi-turn.

## Rollback

Single commit, no schema change. Revert removes the endpoint/agent; the
retrieval layer is untouched.
