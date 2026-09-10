# Implementation plan

Add a best-effort, history-aware query-rewrite step to the chat path. Rewrite
logic in `agents/`, orchestration in `services/chat.py`, toggled by Settings.
Conventions: `uv run <cmd>`; agents never import services; prompts are
versioned files; docs in English; no user text in logs.

## Stage A — Rewrite agent + prompt (`agents/`)

- [ ] A1. `agents/prompts/rewrite.md`: standalone-question reformulation
      contract — resolve pronouns/anaphora/ellipsis from the conversation
      history, return the question UNCHANGED if already self-contained,
      preserve the original language, output ONLY the query text (no quotes,
      no explanation).
- [ ] A2. `agents/rewrite.py`: `build_rewrite_agent(model: Model) -> Agent[None, str]`
      using `instructions=load_prompt("rewrite.md")` (import `load_prompt`
      from `agents/qa.py` — no new loader). `deps_type` is `None`; no tools.
      Module imports pydantic-ai + the prompt loader only (no `services/`).

## Stage B — Service orchestration (`services/chat.py`)

- [ ] B1. `ChatService.__init__`: add keyword-only `rewrite_model: Model | None = None`
      and `rewrite_history_turns: int = 3`. Build
      `self._rewrite_agent = build_rewrite_agent(rewrite_model) if rewrite_model else None`;
      store the turn cap. Existing params/behavior unchanged.
- [ ] B2. `_rewrite_query(self, question, history) -> str`: return `question`
      when `_rewrite_agent is None` or `history` is empty; else slice the last
      `2 * rewrite_history_turns` messages (full window when `<= 0`), run
      `await self._rewrite_agent.run(question, message_history=recent)`, return
      `result.output.strip() or question`. Wrap in `try/except Exception`:
      log `query_rewrite_failed` (error_class only, NO text) and return
      `question`. The method must never raise.
- [ ] B3. `ask`: after `yield RunStartedEvent`, compute
      `retrieval_question = await self._rewrite_query(question, turn.history if turn else [])`
      and pass it as the first arg of `run_stream(...)`. Keep
      `message_history=turn.history if turn and turn.history else None`
      unchanged. Do NOT change what `_prepare_turn` persists (original
      question) or `to_message_history`.
- [ ] B4. Observability: emit flags/lengths only (`applied`, `changed`,
      `original_length`, `rewritten_length`) — never question or rewritten
      text. Fold into an existing log line or add one `query_rewrite` event.

## Stage C — Config + wiring

- [ ] C1. `core/config.py`: `CHAT_QUERY_REWRITE_ENABLED: bool = True` and
      `CHAT_REWRITE_HISTORY_TURNS: int = 3`, with comments (chars-not-tokens
      style). Keep near the other `CHAT_*` settings.
- [ ] C2. `.env.example`: `KB_CHAT_QUERY_REWRITE_ENABLED=true` and
      `KB_CHAT_REWRITE_HISTORY_TURNS=3`.
- [ ] C3. `api/deps.py::build_chat_service`: reuse the single
      `get_chat_model(settings)` instance; pass
      `rewrite_model=model if settings.CHAT_QUERY_REWRITE_ENABLED else None`
      and `rewrite_history_turns=settings.CHAT_REWRITE_HISTORY_TURNS`.

## Stage D — Test fakes

- [ ] D1. `tests/fakes.py`: `scripted_rewrite_model(outputs, *, prompts=None,
      histories=None)` — `function`-based FunctionModel (mirror
      `scripted_summarize_model`): i-th `run` returns `outputs[i]`; record
      `histories`/`prompts` for assertions; support a scripted raise for AC5.

## Stage E — Tests (`tests/test_chat_service.py`)

- [ ] E1. AC1/AC6: DB test — turn 1 topic, turn 2 anaphoric Chinese question;
      assert the retriever's turn-2 call query == scripted standalone (Chinese)
      query, not the raw anaphoric text; assert the rewriter saw prior turns.
- [ ] E2. AC2: first turn (history empty) → rewrite `run` never invoked,
      retriever sees raw question; stateless service (no factory) → no rewrite.
- [ ] E3. AC3: persisted user message == original; next turn's
      `message_history` user prompt == original (via `_user_prompts`).
- [ ] E4. AC4: `rewrite_model=None` → every turn passes the raw question.
- [ ] E5. AC5: rewrite fake raises (openai error) → retriever sees raw query,
      stream reaches `DoneEvent`, no `ErrorEvent`.
- [ ] E6. AC7: `capture_logs` — no question/rewritten text logged.
- [ ] E7. Confirm existing chat tests (no `rewrite_model`) still pass
      unchanged (regression / stateless invariant).

## Stage F — Regression gate

- [ ] F1. `uv run ruff check && uv run mypy && uv run pytest` (offline suite,
      `live_llm` excluded) all green. Confirm the search-gate drift-guard test
      is unaffected by the new `CHAT_*` settings.

## Stage G — Manual E2E (optional, real corpus + live LLM)

- [ ] G1. Two-turn session: turn 1 asks about a topic, turn 2 anaphoric
      (`那它的缺点呢?`); confirm turn 2 retrieves on the resolved topic and the
      answer is grounded/cited. Toggle `KB_CHAT_QUERY_REWRITE_ENABLED=false`
      and confirm the anaphoric turn degrades as before.

## Validation commands

```bash
uv run ruff check
uv run mypy
uv run pytest
```

## Rollback points

- Runtime: `KB_CHAT_QUERY_REWRITE_ENABLED=false` (no deploy) → today's
  behavior. Or revert the commit. No migration, no reindex.

## Review gates

- After Stage B: re-read `error-handling.md` streaming rule — confirm
  `_rewrite_query` can never raise into `ask` after the first event.
- After Stage E: confirm no user text in logs (logging-guidelines) and that
  R3 (original question persisted / in history) holds.

## Dependency note

- Composes with the archived retrieval-noise tasks
  (`09-10-irrelevant-query-noise-gates`, `09-10-vector-rescue-bm25-backstop`):
  this task reduces the noise *input* (referent-less queries); the gates still
  filter residual noise. Issue direction #4 (re-evaluate residual noise on
  follow-ups) is a follow-up, not part of this task.
