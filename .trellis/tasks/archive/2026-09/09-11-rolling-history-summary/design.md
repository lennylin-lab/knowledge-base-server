# Design: Rolling conversation summary beyond the history window

Adds a per-session rolling summary that folds evicted turns incrementally,
injects it as leading context in `message_history`, and maintains it
best-effort off the answer critical path. Builds directly on the shipped
token-window + guardrail (`select_history_window`, `_bound_turn`) and reuses
the injected-model testability pattern from the shipped query-rewrite step.

## Current shape (post token-budget task)

```
_prepare_turn:
  rows = list_recent_for_session(session_id, limit=HISTORY_READ_LIMIT)   # newest-first
  window = select_history_window(rows, budget, measure, per_turn_cap)     # in-window turns
  history = to_message_history(window)                                    # oldest-first
  persist user message; commit
  return _ChatTurn(session_id, history)
```

Turns in `rows` but not in `window` are dropped with no trace (the cliff).

## Data model (R3) — `chat_sessions` + migration 0007

Two nullable columns on `ChatSession`:

| Column | Type | Meaning |
|--------|------|---------|
| `rolling_summary` | `Text`, nullable | compressed summary of all folded (evicted) turns; `NULL`/empty = nothing folded yet |
| `summarized_through_id` | `UUID`, nullable | id (uuid7, time-ordered) of the newest message already folded into `rolling_summary`; the incremental watermark (Q3) |

Migration `0007_chat_rolling_summary` (revises `0006`): `add_column` both,
nullable, no backfill (R3); `downgrade` drops both. Follows the 0006 pattern.

## Fold boundary (R1) — which turns get summarized

At maintenance time the messages are read newest-first (≤ `HISTORY_READ_LIMIT`).
Given the current `window` (in-window turns) and `summarized_through_id`
(watermark), the **turns to fold** are the complete turns that are:

- older than the retained window (they scrolled out), AND
- newer than the watermark (not yet summarized).

These are exactly the turns between the watermark and the window's oldest
retained turn. Folding them advances the watermark to the newest folded
message id. Turns that scrolled past the 200-message read before folding are
not summarized (Q4 MVP bound — documented).

## Conversation-summary agent (`agents/conversation_summary.py` + prompt)

The document summarizer (`agents/summarize.py`) is title/tags/section-shaped
and unsuitable. Add a dedicated, tool-free agent:

```python
def build_conversation_summary_agent(model: Model) -> Agent[None, str]:
    return Agent(model, instructions=load_prompt("conversation_summary.md"))
```

- Reuses `agents/qa.py::load_prompt`; `deps_type=None`; no tools; imports only
  pydantic-ai + the prompt loader (layering convention #6). ✔
- Run non-streaming: `result = await agent.run(fold_prompt)`, output is the new
  summary text.
- `prompts/conversation_summary.md` contract: given the existing summary (may be
  empty) and the newly-evicted turns, produce ONE updated summary that folds
  the new turns into the old, preserves entities/decisions/open threads, drops
  chit-chat, stays within the token bound, and preserves the conversation's
  language. Output only the summary text.

## Summary maintenance (R4) — persist-time, best-effort (Q1)

Maintenance runs **after** the answer streamed and the assistant message was
persisted, as a separate best-effort step that never raises into the stream:

```
ask(...):
  ... stream answer ...
  await _persist_assistant_message(turn, ...)   # existing, inside the try
  await _maybe_update_rolling_summary(turn.session_id)   # NEW, best-effort, own guard
  ... yield DoneEvent
```

`_maybe_update_rolling_summary` (never raises; no LLM call inside an open txn):

```python
if self._summary_agent is None:          # disabled / stateless
    return
try:
    # 1. READ txn: rows, current summary + watermark; close txn.
    # 2. Compute turns-to-fold (watermark < turn < window-oldest).
    #    If none: return (nothing evicted since last fold).
    # 3. LLM: new_summary = summary_agent.run(render_fold_prompt(old_summary, folds))
    #    (NO open transaction here).
    # 4. WRITE txn: update rolling_summary=new_summary,
    #    summarized_through_id=<newest folded id>; commit.
except Exception:
    logger.warning("rolling_summary_update_failed", error_class=...)   # no text
    # watermark unchanged → folding retries next turn (R4/AC5)
```

Rationale (Q1): persist-time keeps the answer's time-to-first-event unchanged
and tolerates a one-turn lag (the summary used on turn N reflects folds through
turn N-1) — acceptable for "gradual degradation". Read-time folding (immediate
but adds prelude latency and risks an LLM call near the read txn) is the
documented alternative.

## Summary injection into history (R2) — `_prepare_turn`

When `rolling_summary` is non-empty, prepend it to the assembled history:

```
history = _summary_prefix(rolling_summary) + to_message_history(window)
```

Framing (Q2): a labeled synthetic turn rather than a `SystemPromptPart` (which
could collide with the agent's own instructions):

```python
def _summary_prefix(summary: str) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"[Summary of earlier conversation]\n{summary}")]),
        ModelResponse(parts=[TextPart(content="Understood — I'll use this summary as context.")]),
    ]
```

The pair keeps request/response alternation clean and makes the summary's role
explicit to the model. Exact wording is an implementation detail.

Budget reservation (Q5/R2): the summary is bounded to `CHAT_SUMMARY_MAX_TOKENS`
and that many tokens are **reserved** — `select_history_window` is called with
`budget = history_token_budget - reserved_summary_tokens` so the summary is
never evicted by turn growth (older context stays represented). If the summary
alone approaches the budget, turns get little room (degenerate but correct: the
summary is the memory). Reserve only when a summary is present.

## Service + config wiring

`ChatService.__init__` (mirrors the shipped `rewrite_model` pattern):

```python
summary_model: Model | None = None,       # None disables rolling summary
summary_max_tokens: int = 400,
...
self._summary_agent = build_conversation_summary_agent(summary_model) if summary_model else None
self._summary_max_tokens = summary_max_tokens
```

`core/config.py` / `.env.example`:

| Field | Default | Meaning |
|-------|---------|---------|
| `CHAT_ROLLING_SUMMARY_ENABLED: bool` | `True` | `deps.py` passes a `summary_model` only when true; false → today's cliff eviction (R5/AC4) |
| `CHAT_SUMMARY_MAX_TOKENS: int` | `400` | bound on the stored/injected summary and the reserved budget slice |

`deps.py::build_chat_service`: reuse the single `get_chat_model(settings)`
instance as `summary_model` when enabled; pass `summary_max_tokens`.

## Repository additions (`repositories/chat.py`)

- `ChatSessionRepository.update_rolling_summary(session_id, *, summary, through_id)`
  — single `update(...)` statement (caller owns the txn), sibling of `touch`.
- Read of `rolling_summary` / `summarized_through_id` comes with the existing
  `get_by_id` (columns are on the row) — no new read method needed for the
  prelude; maintenance reads the same row plus `list_recent_for_session`.

## Data flow (new edges bracketed)

```
prelude: get_by_id (summary+watermark) + list_recent_for_session
         → select_history_window(budget - [reserved])
         → [ _summary_prefix ] + to_message_history → run
post-answer: persist assistant → [ _maybe_update_rolling_summary:
             read → fold turns via summary agent → update summary+watermark ]
```

## Edge cases

| Case | Behavior |
|------|----------|
| First turn / no eviction yet | No summary present; identical to today |
| Summary present, no new evictions | Injected as context; maintenance no-ops (no folds) |
| New turns evicted this turn | Folded post-answer; visible on the next turn |
| Summary disabled | No column read, no model call, no injection (cliff) |
| Maintenance LLM fails | Watermark unchanged; retried next turn; turn still succeeds |
| Stateless service (no factory) | No summary at all (no DB) |
| Turns scrolled past HISTORY_READ_LIMIT unfolded | Not summarized (MVP bound, Q4) |

## Compatibility / rollback

- Additive schema (two nullable columns); existing sessions unaffected until
  new turns evict. Migration `downgrade` drops both columns.
- Rollback: `KB_CHAT_ROLLING_SUMMARY_ENABLED=false` (runtime) → cliff behavior;
  the columns stay but are unused. Or revert the commit + `alembic downgrade`.

## Files touched (expected)

| File | Change |
|------|--------|
| `models/chat.py` | `ChatSession.rolling_summary`, `summarized_through_id` |
| `alembic/versions/0007_chat_rolling_summary.py` | NEW migration (add/drop columns) |
| `repositories/chat.py` | `update_rolling_summary(...)` |
| `agents/conversation_summary.py` | NEW: `build_conversation_summary_agent` + `render_fold_prompt` |
| `agents/prompts/conversation_summary.md` | NEW: incremental fold contract |
| `services/chat.py` | `_summary_prefix`, `_maybe_update_rolling_summary`, budget reservation; `__init__`/`_prepare_turn`/`ask` wiring |
| `core/config.py`, `.env.example` | `CHAT_ROLLING_SUMMARY_ENABLED`, `CHAT_SUMMARY_MAX_TOKENS` |
| `api/deps.py` | pass `summary_model` (gated) + `summary_max_tokens` |
| `tests/fakes.py` | `scripted_summary_model` (function-based) if not covered by an existing fake |
| `tests/test_chat_service.py` | AC1–AC7 coverage |

## Tradeoffs / rejected

- **Read-time folding** (immediate, no lag): adds latency before the first event
  and risks an LLM call near the read txn. Rejected for persist-time
  best-effort (Q1).
- **Recompute the whole summary from all out-of-window turns each turn**: simple
  but O(history) per turn and non-incremental — rejected for the watermark fold
  (R1/AC3).
- **`SystemPromptPart` for the summary**: risks colliding with the agent's
  instructions; a labeled synthetic turn is safer (Q2).
- **No budget reservation** (summary counts as an ordinary entry): turn growth
  could evict the summary itself, defeating the feature — rejected (Q5).
