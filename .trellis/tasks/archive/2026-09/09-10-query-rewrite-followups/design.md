# Design: History-aware query rewriting for multi-turn follow-ups

Adds a best-effort, pre-run rewrite step to `ChatService` that turns a
follow-up question into a self-contained retrieval-facing question, using the
session's recent history. Rewrite logic lives in `agents/` (a small,
tool-free rewrite agent + versioned prompt); the service orchestrates it. No
retrieval-gate changes, no schema changes, no new SSE events.

## Where the rewrite plugs in (data flow)

Current chat flow (`services/chat.py::ChatService.ask`):

```
_prepare_turn (persist original question, assemble history)
  → yield RunStartedEvent
  → agent.run_stream(question, message_history=history)   # model forms search query
      → search_knowledge(query) → retriever.retrieve(query)
```

New flow (single added edge, bracketed):

```
_prepare_turn (persist ORIGINAL question, assemble history)
  → yield RunStartedEvent
  → [ retrieval_question = _rewrite_query(question, history)   # NEW, best-effort ]
  → agent.run_stream(retrieval_question, message_history=history)
      → search_knowledge(query) → retriever.retrieve(query)
```

The rewritten string is used **only** as the run prompt (`run_stream`'s first
arg). The original question is what `_prepare_turn` already persisted and what
`to_message_history` rebuilds for later turns, so R3 (faithful history) holds
without touching persistence or `to_message_history`.

## Chosen integration: rewrite the run prompt (Q1)

Rationale for using the rewritten question as the run prompt rather than
resolving inside the tool:

- **Deterministic.** The model's own `search_knowledge` query is history-blind
  today (issue #3); making the *prompt itself* self-contained means both the
  model's search query and its answer reasoning start from resolved text,
  instead of relying on the QA model to voluntarily resolve anaphora.
- **One call per turn, not per search.** A single rewrite runs before the
  stream; a tool-side rewrite would fire once per `search_knowledge` call and
  add an LLM round-trip inside each tool invocation.
- **Minimal blast radius.** `ChatDeps`, `search_knowledge`, `format_context_blocks`,
  citation numbering, and the `sources`/`tool_calls` accounting are all
  untouched — the change is confined to `ask` plus a new agent module.
- **Testable in isolation** with the existing FunctionModel + StubRetriever
  harness (see Testing).

### Rejected alternative — resolve inside `search_knowledge` via `ChatDeps`

Inject recent history into `ChatDeps` and rewrite the model's query inside the
tool. Rejected: fires per search call (N LLM round-trips), pushes an LLM
dependency into a retrieval tool, complicates multi-search numbering, and
still leaves the answer-generation prompt anaphoric. `ChatDeps`' "natural
seam" docstring is honored instead by the service-level history the rewrite
consumes.

## The rewrite agent (`agents/rewrite.py` + `prompts/rewrite.md`)

A tool-free Pydantic AI agent, constructed like the other agents (model
injected, prompt from a versioned file), returning a plain string:

```python
# agents/rewrite.py
def build_rewrite_agent(model: Model) -> Agent[None, str]:
    return Agent(model, instructions=load_prompt("rewrite.md"))
```

- `deps_type` is `None` (no tools, no retriever) — the agent only reformulates
  text. It reuses `agents/qa.py::load_prompt` (shared prompt loader) — no new
  loader (code-reuse guide).
- Run non-streaming: `result = await agent.run(question, message_history=recent)`;
  `result.output` is the standalone question. Prior turns arrive as
  `message_history` (same mechanism the QA agent uses), so the model sees the
  conversation without any manual text-flattening.
- Layering: `agents/rewrite.py` imports only pydantic-ai + the shared prompt
  loader; it never imports `services/` (spec convention #6). ✔

### Prompt contract (`prompts/rewrite.md`)

Instructs the model to: read the conversation history above; rewrite the
user's latest question into a single, self-contained search query with
pronouns/anaphora/ellipsis resolved; **return the question unchanged if it is
already self-contained**; preserve the original language (qa.md rule 5);
output only the query text — no quotes, no explanation, no preamble.

## Service wiring (`services/chat.py`)

`ChatService.__init__` gains two optional, keyword-only params (backward
compatible — stateless/pre-existing construction is unchanged):

```python
def __init__(self, retriever, model, *, mode, extra_tools=(),
             session_factory=None, history_char_budget=8000,
             rewrite_model: Model | None = None,        # NEW: None disables rewrite
             rewrite_history_turns: int = 3):            # NEW: recent-turn cap (<=0 = full window)
    ...
    self._rewrite_agent = build_rewrite_agent(rewrite_model) if rewrite_model else None
    self._rewrite_history_turns = rewrite_history_turns
```

`rewrite_model` as the toggle (rather than a bool) means "no model → no
rewrite" and folds R4 (Settings disable) into construction: `deps.py` passes a
model only when `CHAT_QUERY_REWRITE_ENABLED` is true.

### Why a separate `rewrite_model` param (not reuse `self._model`)

Production passes the **same** `get_chat_model(settings)` instance as both
`model` and `rewrite_model` (one SDK client, no extra cost). The param exists
for **testability**: `scripted_chat_model` is stream-only (`stream_function`),
but the rewrite uses `Agent.run`, which FunctionModel serves via a
non-streaming `function` (the summarize path's constraint). Injecting the
rewrite model separately lets tests script the rewrite call with a
`function`-based fake without touching the stream-only QA fake.

### `_rewrite_query` — best-effort, never raises (R4, AC5)

```python
async def _rewrite_query(self, question: str, history: list[ModelMessage]) -> str:
    if self._rewrite_agent is None or not history:
        return question                      # disabled, first turn, or stateless (R2, AC2, AC4)
    recent = history if self._rewrite_history_turns <= 0 \
        else history[-2 * self._rewrite_history_turns:]   # cap: N turns = 2N messages (Q4)
    try:
        result = await self._rewrite_agent.run(question, message_history=recent)
        rewritten = result.output.strip()
        return rewritten or question         # empty output → raw
    except Exception:
        logger.warning("query_rewrite_failed", error_class=...)   # no question text
        return question                      # degrade to raw (best-effort)
```

Called in `ask` **after** `yield RunStartedEvent` and before `run_stream`;
because it can never raise, it does not interact with the streaming
"terminal-error-after-first-event" rule (error-handling spec) — a rewrite
failure simply degrades to the raw query and the answer still streams
(AC5). Placement keeps `RunStartedEvent` timing unchanged (R2 for the
event contract).

`ask` then opens the stream with the resolved prompt:

```python
retrieval_question = await self._rewrite_query(question, turn.history if turn else [])
async with self._agent.run_stream(
    retrieval_question,
    deps=deps,
    message_history=turn.history if turn and turn.history else None,
) as result:
```

Note the `message_history` guard is unchanged; the rewrite only affects the
first positional prompt.

### Observability

One new log line `query_rewrite` (or fold into `agent_run_started`) carrying
**flags and lengths only** — `applied` (bool), `changed` (bool),
`original_length`, `rewritten_length` — never the question or the rewritten
text (logging-guidelines: user data stays out of logs). `input_tokens` for the
rewrite call is available from `result.usage` and may be added to the run's
token accounting if desired (optional; not required by AC).

## Configuration (`core/config.py`, `.env.example`)

| Field | Default | Meaning |
|-------|---------|---------|
| `CHAT_QUERY_REWRITE_ENABLED: bool` | `True` | `deps.py` passes a `rewrite_model` only when true; false → byte-identical to today (R4/AC4) |
| `CHAT_REWRITE_HISTORY_TURNS: int` | `3` | Recent turns fed to the rewriter; `<=0` uses the full assembled window (Q4 latency/cost bound) |

These are `CHAT_*` settings and are unrelated to the retriever
`DEFAULT_*`↔`SEARCH_*` drift-guard (that guard covers search-gate constants
only), so no drift-guard change is expected — to be confirmed by running it.

## Wiring (`api/deps.py::build_chat_service`)

```python
model = get_chat_model(settings)
return ChatService(
    _build_retriever(settings, provider),
    model,
    mode="hybrid" if provider is not None else "bm25",
    extra_tools=extra_tools,
    session_factory=SessionFactory,
    history_char_budget=settings.CHAT_HISTORY_CHAR_BUDGET,
    rewrite_model=model if settings.CHAT_QUERY_REWRITE_ENABLED else None,   # same instance
    rewrite_history_turns=settings.CHAT_REWRITE_HISTORY_TURNS,
)
```

Reusing the single `model` instance keeps one SDK client per process (the
existing chat-model lifetime). No change to `get_chat_service`'s cache or the
no-key `ChatUnavailableError` gate.

## Testing strategy (offline, `tests/test_chat_service.py` + `fakes.py`)

New fake: `scripted_rewrite_model(outputs, *, prompts=None, histories=None)` —
a `function`-based FunctionModel (mirrors `scripted_summarize_model`) whose
i-th `run` returns `outputs[i]`; `histories` records the message list so tests
can assert the rewriter saw prior turns.

| AC | Test |
|----|------|
| AC1/AC6 | DB test: turn 1 establishes topic, turn 2 anaphoric Chinese question; assert `StubRetriever.calls` shows turn 2's query == the scripted standalone (Chinese) query, not the raw `那它的缺点呢?` |
| AC2 | First turn: `rewrite_model` provided but history empty → rewrite fake's `run` never called; retriever sees raw question. Stateless (no factory): no rewrite. |
| AC3 | After the anaphoric turn, assert persisted user message == original raw text and next turn's `message_history` user prompt == original (existing `_user_prompts` helper). |
| AC4 | `rewrite_model=None` → every turn passes the raw question (no rewrite call). |
| AC5 | Rewrite fake scripted to raise (openai error) → retriever sees raw query, stream still reaches `DoneEvent`, no `ErrorEvent`. |
| AC7 | `capture_logs`: no question/rewritten text in logs; `ruff`/`mypy`/`pytest` green. |

Existing chat tests that construct `ChatService` without `rewrite_model` stay
valid unchanged (new params default to off) — regression safety for the whole
suite and the stateless invariant test.

## Compatibility / rollback

- Backward compatible: both new params default to off; omitting them (all
  current call sites/tests) reproduces today's behavior exactly. No migration,
  no reindex, no schema change.
- Rollback: set `KB_CHAT_QUERY_REWRITE_ENABLED=false` (runtime, no deploy), or
  revert the commit.

## Files touched (expected)

| File | Change |
|------|--------|
| `agents/rewrite.py` | NEW: `build_rewrite_agent(model) -> Agent[None, str]` (reuses `qa.load_prompt`) |
| `agents/prompts/rewrite.md` | NEW: standalone-question reformulation contract (language-preserving, no-op if self-contained) |
| `services/chat.py` | `__init__` gains `rewrite_model` / `rewrite_history_turns`; new `_rewrite_query`; `ask` uses resolved prompt for `run_stream` |
| `core/config.py` | `CHAT_QUERY_REWRITE_ENABLED`, `CHAT_REWRITE_HISTORY_TURNS` |
| `.env.example` | document both new `KB_CHAT_*` vars |
| `api/deps.py` | pass `rewrite_model` (same model instance, gated by flag) + turn cap |
| `tests/fakes.py` | `scripted_rewrite_model` (function-based) |
| `tests/test_chat_service.py` | AC1–AC7 coverage |

## Tradeoffs

- **Added latency on follow-up turns.** One extra non-streaming LLM call
  before the answer stream starts (only when history exists and rewrite is
  enabled). Bounded by `CHAT_REWRITE_HISTORY_TURNS`; can be gated later by an
  anaphora heuristic (Q2, deferred) to skip obviously self-contained
  questions. First turns and stateless mode pay nothing (R2).
- **Rewrite model quality.** A poor rewrite could over-specify; the prompt's
  "return unchanged if already self-contained" rule and the best-effort raw
  fallback bound the downside, and retrieval gates (archived 09-10 tasks) still
  filter noise.
