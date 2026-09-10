# Implementation plan

Add a persisted, incrementally-folded rolling conversation summary, injected as
leading history context and maintained best-effort after the answer. Builds on
the shipped token-window + guardrail and the injected-model pattern.
Conventions: `uv run <cmd>`; summary agent in `agents/`; agents never import
services; prompts are versioned files; docs in English; no user text in logs;
tests stay offline.

## Stage A — Schema (`models/chat.py` + migration)

- [ ] A1. `ChatSession`: add `rolling_summary: Mapped[str | None]` (Text,
      nullable) and `summarized_through_id: Mapped[uuid.UUID | None]` (UUID,
      nullable).
- [ ] A2. `alembic/versions/0007_chat_rolling_summary.py` (revises `0006`):
      `add_column` both (nullable, no backfill); `downgrade` drops both. Follow
      the 0006 file shape.
- [ ] A3. `uv run alembic upgrade head` then `downgrade -1` then `upgrade head`
      against the dev DB to confirm both directions.

## Stage B — Conversation-summary agent (`agents/`)

- [ ] B1. `agents/prompts/conversation_summary.md`: incremental-fold contract —
      inputs are the existing summary (may be empty) and the newly-evicted
      turns; output ONE updated summary that folds new into old, preserves
      entities/decisions/open threads, drops chit-chat, stays within the token
      bound, preserves the conversation language, outputs only the summary text.
- [ ] B2. `agents/conversation_summary.py`:
      `build_conversation_summary_agent(model) -> Agent[None, str]` (reuse
      `qa.load_prompt`; `deps_type=None`; no tools) and
      `render_fold_prompt(existing_summary, folded_turns)`. Import pydantic-ai +
      the loader only.

## Stage C — Repository (`repositories/chat.py`)

- [ ] C1. `ChatSessionRepository.update_rolling_summary(session_id, *, summary,
      through_id)` — one `update(...)` statement, caller owns the txn (sibling
      of `touch`).

## Stage D — Service wiring (`services/chat.py`)

- [ ] D1. `__init__`: add `summary_model: Model | None = None` and
      `summary_max_tokens: int = 400`; build
      `self._summary_agent = build_conversation_summary_agent(summary_model) if
      summary_model else None`; store the cap.
- [ ] D2. `_summary_prefix(summary) -> list[ModelMessage]`: labeled synthetic
      request/response pair carrying the summary as earlier-conversation
      context.
- [ ] D3. `_prepare_turn`: read `rolling_summary`/`summarized_through_id` (on
      the `get_by_id` row); when a summary is present, reserve
      `min(summary_tokens, summary_max_tokens)` from the budget passed to
      `select_history_window`, and prepend `_summary_prefix(...)` to the
      assembled history. Keep persisting the ORIGINAL user message.
- [ ] D4. `_maybe_update_rolling_summary(session_id)`: best-effort, NEVER
      raises; no LLM call inside an open txn. Read rows + summary/watermark
      (read txn, closed) → compute turns-to-fold (watermark < turn <
      window-oldest) → if none, return → `summary_agent.run(render_fold_prompt(
      old, folds))` (no txn) → `update_rolling_summary(...)` (write txn). Log
      `rolling_summary_update_failed` (error_class only) on failure.
- [ ] D5. `ask`: call `await self._maybe_update_rolling_summary(turn.session_id)`
      after `_persist_assistant_message`, wrapped so a failure cannot become a
      terminal error (the turn already succeeded) — before `DoneEvent`.

## Stage E — Config + wiring

- [ ] E1. `core/config.py`: `CHAT_ROLLING_SUMMARY_ENABLED: bool = True`,
      `CHAT_SUMMARY_MAX_TOKENS: int = 400` (comments).
- [ ] E2. `.env.example`: `KB_CHAT_ROLLING_SUMMARY_ENABLED=true`,
      `KB_CHAT_SUMMARY_MAX_TOKENS=400`.
- [ ] E3. `api/deps.py::build_chat_service`: pass
      `summary_model=model if settings.CHAT_ROLLING_SUMMARY_ENABLED else None`
      (reuse the single model instance) and
      `summary_max_tokens=settings.CHAT_SUMMARY_MAX_TOKENS`.

## Stage F — Test fakes

- [ ] F1. `tests/fakes.py`: `scripted_summary_model(outputs, *, prompts=None)` —
      `function`-based FunctionModel (mirror `scripted_summarize_model`) whose
      i-th `run` returns `outputs[i]`; support a scripted raise for AC5. (Reuse
      `scripted_summarize_model` if its shape already fits.)

## Stage G — Tests (`tests/test_chat_service.py`, DB-marked)

- [ ] G1. AC1: a session long enough to evict the oldest turn → that turn's
      content appears in the stored `rolling_summary`; the raw turn is not in
      the in-window history.
- [ ] G2. AC2: next turn's `histories` show the summary prefix ahead of the
      in-window turns, labeled.
- [ ] G3. AC3: incremental — already-folded turns are not re-summarized;
      `summary_model` is called only when new turns evicted (assert call count /
      watermark advance).
- [ ] G4. AC4: `summary_model=None` → no summary read/computed/injected;
      byte-identical to cliff behavior.
- [ ] G5. AC5: scripted summary failure → answer still reaches `DoneEvent`,
      summary/watermark unchanged, no `ErrorEvent`.
- [ ] G6. AC6: persisted `ChatMessage` rows unchanged by summarization (only
      session summary/watermark columns change).
- [ ] G7. AC7: `capture_logs` — no summary/question text logged.

## Stage H — Regression gate

- [ ] H1. `uv run ruff check && uv run mypy && uv run pytest` (offline suite)
      green; migration up/down verified (A3). Existing chat tests (no
      `summary_model`) pass unchanged.

## Stage I — Manual E2E (optional, live)

- [ ] I1. Long session: confirm early-turn facts remain answerable via the
      injected summary after those turns leave the window; toggle
      `KB_CHAT_ROLLING_SUMMARY_ENABLED=false` to confirm the cliff returns.

## Validation commands

```bash
uv run alembic upgrade head
uv run ruff check
uv run mypy
uv run pytest
```

## Rollback points

- `KB_CHAT_ROLLING_SUMMARY_ENABLED=false` (runtime) → cliff behavior, columns
  unused. Full revert: revert commit + `uv run alembic downgrade -1`.

## Review gates

- After Stage D: confirm no LLM call happens inside an open transaction and
  `_maybe_update_rolling_summary` can never raise into `ask` (error-handling
  streaming rule); confirm ORM rows are never rewritten (R3/AC6).
- After Stage G: confirm no summary/question text in logs (logging-guidelines)
  and the disabled path is byte-identical to today (AC4).

## Dependency note

- Depends on the shipped token-window + guardrail (`select_history_window`,
  `_bound_turn`) and reuses the `rewrite_model` injection pattern. Complements,
  does not replace, the guardrail: the guardrail bounds one oversized turn; this
  task carries older evicted turns forward as a summary. Carrying prior-run
  sources forward (issue direction #3, grounding half) remains a separate task.
