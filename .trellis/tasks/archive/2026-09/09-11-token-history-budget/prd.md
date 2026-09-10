# Token-based history budget + long-document guardrail

## Goal

Bound the multi-turn conversation history sent to the model by **tokens**, not
characters, and prevent a single oversized turn (e.g. a long pasted document)
from silently evicting all other history. This is the budget half of the
multi-turn hallucination-risks issue's direction #3, and it directly removes
mechanism #4 ("char-based budget … one long turn … silently evicts all other
history, degrading the session to single-turn without the model knowing").

## Background — diagnosis (source-verified)

`services/chat.py::select_history_window(messages, *, budget)` measures each
turn's cost as `len(user.content) + len(assistant.content)` (characters) and
walks newest-first, stopping at the first turn that does not fit `budget`
(`CHAT_HISTORY_CHAR_BUDGET`, default 8000; `.env.example`
`KB_CHAT_HISTORY_CHAR_BUDGET`). Two consequences:

1. **Characters are a poor proxy for the real (token) cost.** For mixed
   CJK/English content the char→token ratio varies widely (English ~4 chars/
   token, CJK closer to 1–1.5), so an 8000-char budget maps to very different
   token loads per session. The config comment already flags this: "chars, not
   tokens, are the MVP proxy (PRD out-of-scope: token-accurate budgeting)".
   The `input_tokens` the run already logs is the real quantity to bound.
2. **One oversized turn starves the rest.** Because the walk is newest-first
   and a turn is all-or-nothing, a single turn whose content alone exceeds the
   budget makes the window empty (or, if it is the newest and barely fits,
   leaves no room for any older turn). The session silently degrades to
   single-turn and nothing signals the loss.

`tiktoken` (0.14.0) is already resolved in `uv.lock` (via `openai`), so an
OpenAI-family tokenizer is available; it is not yet a direct dependency.

## Scope

In scope: token-based measurement for the chat history window, a per-turn
guardrail so an oversized turn cannot evict the rest of history, the Settings
change from a char budget to a token budget, and a token-counting helper in
`llm/` with an offline-safe fallback.

## Requirements

### R1 — Token-based history budget

- The history window must be selected by a **token** budget: each turn's cost
  is the token count of its user + assistant content, summed newest-first
  until the budget is exhausted (same whole-turn, no-orphan-half-turn walk as
  today, just a token measure).
- Token counting must be derived from the configured chat model where possible
  and must be **offline-safe and deterministic in tests** (no network at
  import/first-use in the test suite).

### R2 — Long-document guardrail (no silent single-turn collapse)

- A single turn whose token cost exceeds a per-turn cap must not silently evict
  all other history. The turn is admitted in a **bounded** form (its history
  copy is truncated to the cap with a visible truncation marker) so older turns
  can still fit the remaining budget.
- Truncation applies ONLY to the copy assembled into `message_history`; the
  persisted `ChatMessage` rows and `to_message_history`'s faithfulness for
  normal turns are unchanged (persistence always stores full content).
- The marker makes the truncation observable to the model (it sees that
  content was elided), replacing today's silent degradation.

### R3 — Config migration

- Replace `CHAT_HISTORY_CHAR_BUDGET` with `CHAT_HISTORY_TOKEN_BUDGET`
  (default `2000`) and add `CHAT_HISTORY_MAX_TURN_FRACTION` (default `0.5`,
  `>= 1.0` disables the guardrail). Update `.env.example` and all references.
  This is an intentional breaking config change (single-user MVP); document it.
  `tiktoken` is promoted to a direct dependency.

### R4 — No contract regressions

- SSE event order/vocabulary, `sources` semantics, citation numbering,
  `tool_calls` accounting, the error taxonomy, `HISTORY_READ_LIMIT` behavior,
  the first-turn/stateless invariant, and the query-rewrite step (already
  shipped) are all unchanged.
- `select_history_window` stays a pure, unit-testable function (the measure is
  injected, not imported inside it), preserving its current test style.

## Non-goals

- Rolling summary of evicted turns (issue direction #2) — the guardrail
  truncates an oversized single turn but does NOT summarize dropped history;
  that is the next task.
- Carrying the previous run's retrieved sources forward (issue #2 grounding /
  the other half of direction #3) — separate task; needs source persistence.
- Token-accurate accounting of the current question, tool results, or the
  system prompt — this task bounds conversation **history** only (per-run input
  is already observable via logged `input_tokens`).
- Any change to retrieval, agents other than the chat QA path, or the DB
  schema.

## Acceptance Criteria

- [ ] AC1: `select_history_window` selects turns by token count via an injected
      measure; a mixed CJK/English session that fit under the char budget but
      exceeds the token budget drops its oldest turn(s) accordingly. Unit-tested
      with a deterministic fake measure.
- [ ] AC2: A turn whose content exceeds the per-turn cap is included in
      `message_history` truncated-with-marker, and at least one older turn that
      fits the remaining budget is still present (no silent single-turn
      collapse). Unit-tested.
- [ ] AC3: Persistence is unaffected — the stored user/assistant rows keep full
      content; only the assembled history copy is truncated. Verified against
      the disposable test DB.
- [ ] AC4: Production token counting uses the configured chat model's encoding
      when available and falls back to a deterministic heuristic when the
      encoding is unknown or unavailable (offline), never raising. Unit-tested
      via the fallback path.
- [ ] AC5: Config migration complete — `CHAT_HISTORY_CHAR_BUDGET` replaced,
      `.env.example` updated, no dangling references (grep-clean); the new
      settings flow through `deps.py`.
- [ ] AC6: Full offline suite + `ruff` + `mypy` clean, with **no network
      access** required by the tests; no user text in logs.

## Decisions (open questions resolved — all recommendations accepted)

- D1 (guardrail policy): **truncate-with-marker** — the oversized turn's history
  copy is truncated to the per-turn cap and marked; not dropped, not
  kept-whole. (Was Q1.)
- D2 (per-turn cap): **a fraction of the budget**,
  `per_turn_cap = round(budget * CHAT_HISTORY_MAX_TURN_FRACTION)`, default
  fraction `0.5`; `>= 1.0` disables the guardrail. (Was Q2.)
- D3 (tokenizer / offline): **tiktoken** via `encoding_for_model` + fallback
  encoding, wrapped so unknown-model/offline never raises (heuristic fallback);
  `tiktoken` promoted to a **direct dependency**. (Was Q3.)
- D4 (default token budget): **`CHAT_HISTORY_TOKEN_BUDGET = 2000`** (≈ the old
  8000 chars for mixed CJK/English; an operator knob, not a contract). (Was Q4.)
- D5 (config migration): breaking rename of `CHAT_HISTORY_CHAR_BUDGET` →
  `CHAT_HISTORY_TOKEN_BUDGET` **accepted**; `extra="ignore"` keeps a leftover
  old env var harmless during rollout.
