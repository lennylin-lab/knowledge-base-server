# Rolling conversation summary beyond the history window

## Goal

Replace the cliff-edge eviction of old turns with a **rolling summary**: turns
that fall outside the token history window are folded, incrementally, into a
per-session summary that is injected as leading context on every subsequent
turn. Older context then degrades **gradually** (a compressed memory) instead
of vanishing silently. This is direction #2 of the multi-turn
hallucination-risks issue and directly addresses mechanism #1 ("cliff-edge
memory loss … nothing signals the loss … answers may reference dropped turns
or contradict them").

## Background — diagnosis (source-verified)

`services/chat.py::select_history_window` now selects the newest complete turns
that fit a **token** budget (`CHAT_HISTORY_TOKEN_BUDGET`, default 2000) with a
per-turn guardrail (`_bound_turn`, from the shipped token-budget task). Turns
older than the window are simply not returned — `to_message_history` rebuilds
only the in-window turns, and `_prepare_turn` hands that to the agent. There is
no record of dropped turns and no signal to the model that anything is missing.

Persistence: `chat_sessions` (`models/chat.py`) has no summary column; messages
are stored whole and read newest-first (`list_recent_for_session`, bounded by
`HISTORY_READ_LIMIT = 200`). The existing summarize agent
(`agents/summarize.py`) is **document-oriented** (title/tags/section prompts),
not a conversation summarizer, so a dedicated conversation-summary agent is
needed. The shipped `rewrite_model` injection pattern gives a clean, testable
template for an injected, toggle-able summary model.

## Scope

In scope: a persisted, incrementally-updated per-session rolling summary of
evicted turns; injection of that summary as leading history context; a
dedicated conversation-summary agent + versioned prompt; the schema, config,
and wiring to support it; best-effort maintenance that never breaks a turn.

## Requirements

### R1 — Rolling summary of evicted turns

- Turns that fall outside the token window must be folded into a per-session
  summary that persists across turns. The fold is **incremental**: only turns
  not already summarized are added to the existing summary (a watermark tracks
  how far summarization has progressed), so cost does not grow with session
  length per turn.
- The summary is bounded (a max token size); folding new turns re-compresses to
  stay within the bound.

### R2 — Summary injected as leading context

- When a session has a non-empty rolling summary, it is injected ahead of the
  in-window turns in `message_history`, clearly labeled as a summary of earlier
  conversation (not presented as a verbatim user/assistant turn).
- The injected summary counts against the history budget in a bounded, reserved
  way so it cannot itself be evicted by turn growth (older context stays
  represented — the point of the feature).

### R3 — Persistence & faithfulness

- The rolling summary and its watermark are stored on the session
  (`chat_sessions`), added by an Alembic migration. Existing sessions default
  to an empty summary (no backfill; they simply have no history beyond the
  window until new turns evict).
- Stored `ChatMessage` rows are never rewritten; the summary is derived,
  additional state. `to_message_history`'s per-turn faithfulness is unchanged.

### R4 — Best-effort, off the answer critical path

- Summary maintenance (the extra LLM call) must not block the answer stream and
  must never turn a successful turn into an error: it runs as a best-effort
  step around its own transaction, wrapped so it can never raise into the
  stream. A maintenance failure simply defers folding to a later turn (the
  watermark did not advance).
- No LLM call is made while a DB transaction is held open.

### R5 — Operability & failure isolation

- A Settings flag (default on) disables rolling summary entirely; when off,
  behavior is byte-identical to today's cliff eviction (no summary column read,
  no summary model call).
- Summary text is user-derived content and must never be logged (lengths/counts
  only).

### R6 — No contract regressions

- SSE event order/vocabulary, `sources` semantics, citation numbering,
  `tool_calls` accounting, the error taxonomy, the token-budget selection and
  guardrail, the query-rewrite step, `HISTORY_READ_LIMIT`, and the
  first-turn/stateless invariant are all unchanged.
- Rolling summary is a chat-path concern only; other agent services are
  untouched.

## Non-goals

- Carrying the previous run's retrieved **sources** forward (issue #2 grounding
  half of direction #3) — separate task; needs source persistence.
- Summarizing turns beyond `HISTORY_READ_LIMIT` that were never read — the MVP
  folds turns within the bounded read as they evict (see Open questions).
- Semantic dedup / entity extraction beyond what the summary prompt does.
- Any change to retrieval or to non-chat agents.

## Acceptance Criteria

- [ ] AC1: In a session long enough that the oldest turn falls outside the
      token window, that turn's content is represented in the session's stored
      rolling summary (folded), and the raw turn is no longer in the in-window
      history. Verified against the disposable test DB with an injected
      deterministic summary model.
- [ ] AC2: On the next turn, the rolling summary is injected ahead of the
      in-window turns in the `message_history` the model receives, labeled as an
      earlier-conversation summary (asserted via the scripted model's recorded
      `histories`).
- [ ] AC3: Folding is incremental — a turn already summarized is not
      re-summarized on later turns (watermark advances; the summary model is
      called only when new turns have evicted). Unit/DB-tested.
- [ ] AC4: With the Settings flag disabled, no summary is read, computed, or
      injected; behavior matches today's cliff eviction byte-for-byte.
- [ ] AC5: A scripted summary-maintenance failure does not affect the answer
      stream (turn still reaches `DoneEvent`); the watermark/summary are left
      unchanged and folding retries next turn.
- [ ] AC6: Persisted `ChatMessage` rows are unchanged by summarization; only the
      session's summary/watermark columns change.
- [ ] AC7: Full offline suite + `ruff` + `mypy` clean; migration up/down works;
      no summary/question text in logs.

## Open questions

- Q1 (maintenance timing): fold at persist-time (post-answer, off the critical
  path, one-turn lag) vs. read-time (immediate, adds latency before the first
  event). Design recommends **persist-time, best-effort**.
- Q2 (summary framing in history): a leading `SystemPromptPart` vs. a
  clearly-labeled synthetic user/assistant pair. Design recommends a labeled
  synthetic turn to avoid colliding with the agent's own instructions.
- Q3 (watermark representation): last-summarized message id (uuid7,
  time-ordered) vs. a `(created_at, id)` cursor. Design recommends the message
  id.
- Q4 (`HISTORY_READ_LIMIT` interaction): accept that turns which scroll past the
  200-message read before being folded are not summarized (MVP bound), vs. a
  separate unbounded read for summarization. Recommendation: accept the MVP
  bound; document it.
- Q5 (budget reservation for the summary): reserve a fixed slice
  (`CHAT_SUMMARY_MAX_TOKENS`) of the budget for the injected summary vs. let it
  count as an ordinary leading entry. Design recommends a reserved slice so the
  summary is never evicted by turn growth.
