# History-aware query rewriting for multi-turn follow-ups

## Goal

In a multi-turn chat session, a follow-up question that relies on conversation
context (pronouns / anaphora / ellipsis, e.g. `那它的缺点呢?`) must retrieve
against a **self-contained** query that carries the referent, instead of the
raw anaphoric text. This is suggested-direction #1 of the multi-turn
hallucination-risks issue: it is the largest single win because it both
grounds follow-up answers and reduces the noise-recall pressure that weak
queries put on the retrieval gates.

## Background — diagnosis (source-verified)

`ChatService` already sends prior turns to the model as `message_history`
within `CHAT_HISTORY_CHAR_BUDGET` (`services/chat.py`), and the QA agent's
`search_knowledge` tool (`agents/qa.py`) forwards a query to the retriever.
Two source facts make anaphoric follow-ups fragile:

1. **The retrieval query is model-formed and history-blind at the tool.**
   `search_knowledge(ctx, query)` receives whatever string the model emits
   during the run; `ChatDeps` (`retriever`, `limit`, `collector`) carries no
   conversation context. The tool's docstring only tells the model to "use
   the most distinctive terms of the question", and `prompts/qa.md` rule 4
   ("Search deliberately") never instructs it to resolve anaphora into the
   search query. Although prior turns exist in the model's context window, in
   practice the model tends to search the verbatim follow-up, so an anaphoric
   question gives the retriever no referent.
2. **Weak/anaphoric queries amplify retrieval noise.** The issue links this to
   the rescue-tier noise problem (now addressed by archived tasks
   `09-10-irrelevant-query-noise-gates` and `09-10-vector-rescue-bm25-backstop`).
   A referent-less query is exactly the "weak query" that stresses the gates;
   resolving it upstream reduces that pressure regardless of the gate tuning.

`ChatDeps`' own docstring names itself "the natural seam for later multi-turn
state (history, session ids)", so the codebase already anticipates this work.

## Scope

In scope: a history-aware rewrite step for the **chat** path (`ChatService`)
that turns a follow-up question into a standalone retrieval-facing question,
gated to run only when session history exists (i.e. not on the first turn or
in stateless mode). A versioned rewrite prompt under `agents/prompts/`. A
Settings toggle so the behavior can be disabled without code changes.

## Requirements

### R1 — Standalone retrieval query for follow-ups

- When a turn has non-empty history, the query used for knowledge retrieval
  must be a self-contained reformulation of the user's question with pronouns
  / anaphora / ellipsis resolved from recent history.
- A question that is already self-contained must pass through unchanged (the
  rewrite is a no-op, not a paraphrase).
- The reformulation must preserve the user's original language (qa.md rule 5).

### R2 — First-turn / stateless behavior is byte-identical to today

- A first turn (no history) and the stateless service (no `session_factory`)
  must behave exactly as before: no rewrite call, no added latency, same
  prompt reaches the agent. This mirrors the existing "first turn behaves
  exactly like the stateless service" invariant in `ask`.

### R3 — Faithful persistence and history

- The persisted user message and the `message_history` rebuilt for later
  turns must remain the user's **original** question, not the rewritten form
  (history stays faithful to what the user typed; `to_message_history`
  contract unchanged).

### R4 — Operability & failure isolation

- A Settings flag (default on) disables rewriting entirely; when off, behavior
  is byte-identical to R2 for every turn.
- A rewrite-step failure (provider error, etc.) must not harden a session into
  errors: it degrades to the raw question (best-effort), and the run
  continues. It must never turn a would-be-successful answer into an error
  event on its own. (Open question Q3 confirms the degrade-vs-surface choice.)
- The rewrite step must not log question text (logging-guidelines: user data
  stays out of logs; lengths/flags only).

### R5 — No contract regressions

- SSE event order/vocabulary (`RunStartedEvent → SourcesEvent* →
  AnswerDeltaEvent* → DoneEvent`/terminal `ErrorEvent`), `sources` semantics,
  citation numbering, `tool_calls` accounting, and the error taxonomy are all
  unchanged.
- The rewrite is a QA/chat concern only; `WritingService` and the other agent
  services are out of scope and untouched.

## Non-goals

- Rolling summary beyond the history window (issue direction #2) — separate,
  larger task.
- Token-based budget and carrying prior-run sources forward (issue direction
  #3) — separate task.
- Re-tuning retrieval gates (handled by the archived 09-10 tasks); this task
  only reduces the input noise, it does not change the gates.
- Rewriting for the `search` API, `writing`, `summarize`, or `association`
  paths.

## Acceptance Criteria

- [ ] AC1: In a session where turn 1 establishes a topic and turn 2 is
      anaphoric (`那它的缺点呢?`), the string handed to the retriever on turn 2
      is a standalone query containing the resolved referent (asserted via the
      stub retriever's recorded calls, the existing `retriever.calls` pattern).
- [ ] AC2: A first turn (no history) and a stateless service perform **no**
      rewrite call and pass the raw question to the agent/retriever unchanged.
- [ ] AC3: The persisted user message and the next turn's `message_history`
      carry the original question text, not the rewritten form.
- [ ] AC4: With the Settings flag disabled, every turn behaves as AC2 (no
      rewrite), byte-identical to today.
- [ ] AC5: A scripted rewrite-step provider failure degrades to the raw query
      and the answer still streams to a `DoneEvent` (no terminal error solely
      from the rewrite step).
- [ ] AC6: Original language is preserved in the rewritten query (Chinese
      follow-up → Chinese standalone query).
- [ ] AC7: Full offline suite + `ruff` + `mypy` clean; no question text in logs.

## Open questions

- Q1 (integration point — resolved in design): the rewritten question is used
  as the **run prompt** passed to `agent.run_stream`, while the original is
  persisted and used for history. Alternative (rewrite inside the tool via
  history injected in `ChatDeps`) is documented and rejected in design.md.
- Q2 (rewrite trigger): run on every follow-up turn (history non-empty) and
  let the prompt no-op self-contained questions, vs. a cheap
  anaphora-heuristic gate to save an LLM call. Recommendation: every
  follow-up in v1 (simplest, most reliable); heuristic gating deferred.
- Q3 (failure policy): rewrite failure degrades to the raw question
  (best-effort) rather than surfacing an error — confirm this is desired over
  failing the turn.
- Q4 (history extent fed to the rewriter): reuse the already-assembled history
  window vs. a smaller recent-turns cap (e.g. last 2–3 turns) to bound rewrite
  latency/cost. Recommendation: a small recent-turns cap; see design.md.
