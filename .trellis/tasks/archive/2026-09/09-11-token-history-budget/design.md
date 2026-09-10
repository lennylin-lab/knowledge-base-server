# Design: Token-based history budget + long-document guardrail

Converts `select_history_window`'s cost measure from characters to tokens via
an **injected** counter, adds a per-turn truncation guardrail so a single
oversized turn cannot silently collapse the session to single-turn, and swaps
the char-budget Settings for token-budget Settings. No schema change, no
retrieval change, no change to the shipped query-rewrite step.

## Current shape (`services/chat.py`)

```
select_history_window(messages, *, budget):        # char budget
    for user, assistant in _complete_turns_newest_first(messages):
        cost = len(user.content) + len(assistant.content)   # chars
        if cost > remaining: break
        window += [assistant, user]; remaining -= cost
    window.reverse(); return window

_prepare_turn: ... select_history_window(rows, budget=self._history_char_budget)
               ... to_message_history(window)
```

`ChatService` stores `self._history_char_budget` (default 8000) from
`CHAT_HISTORY_CHAR_BUDGET`, wired in `deps.py::build_chat_service`.

## Change 1 — injected token measure (R1, R4)

Keep `select_history_window` pure and offline-testable by injecting the cost
function instead of importing a tokenizer inside it:

```python
def select_history_window(
    messages: Sequence[ChatMessage], *,
    budget: int,
    measure: Callable[[str], int],          # NEW: text -> token count
    per_turn_cap: int,                       # NEW: see Change 2
) -> list[ChatMessage]:
```

Cost becomes `measure(user.content) + measure(assistant.content)`. Tests pass a
deterministic `measure` (e.g. word-count, or `len`), so the selection logic is
verified without any tokenizer or network (AC1, AC6). Production injects the
tiktoken-backed counter (Change 3).

## Change 2 — long-document guardrail (R2, AC2/AC3)

`per_turn_cap` bounds any single turn's contribution. When a turn's measured
cost exceeds the cap, its **history copy** is truncated to fit the cap and
marked, so the turn is still represented but cannot consume the whole budget:

```
for user, assistant in _complete_turns_newest_first(messages):
    u_cost, a_cost = measure(user.content), measure(assistant.content)
    turn_cost = u_cost + a_cost
    if turn_cost > per_turn_cap:
        # bound this turn's history copy (truncate longer side to the cap),
        # append a marker, recompute its (capped) cost
        user_c, assistant_c, turn_cost = _bound_turn(user, assistant, per_turn_cap, measure)
    else:
        user_c, assistant_c = user, assistant
    if turn_cost > remaining:
        break
    window += [assistant_c, user_c]; remaining -= turn_cost
```

- `_bound_turn` returns **new, detached** `ChatMessage`-shaped carriers (or
  bounded `(role, content)` inputs to `to_message_history`) with truncated
  content plus a visible marker (e.g. `\n\n…[truncated: long message elided]`).
  It never mutates the ORM rows (R2/AC3: persistence untouched).
- Truncation is by tokens using the same injected `measure` for consistency
  (binary/greedy trim to `cap - marker_cost`); an exact token slice is not
  required — being at or under the cap is what matters.
- Because the cap makes an oversized turn finite, older turns still get the
  remaining budget → no silent single-turn collapse (AC2). The marker makes the
  elision visible to the model, replacing today's silent degradation.

Guardrail policy (Q1): truncate-with-marker chosen over "drop the turn" (loses
the exchange entirely) and "keep whole if newest" (reintroduces the starvation
cliff). It is the cheapest graceful degradation short of rolling summary
(explicitly the next task, non-goal here).

Per-turn cap value (Q2): a **fraction of the budget** —
`per_turn_cap = round(budget * CHAT_HISTORY_MAX_TURN_FRACTION)` — so the two
knobs scale together; default fraction e.g. `0.5` (no single turn may eat more
than half the window, guaranteeing room for ≥1 other turn). A `>= 1.0` fraction
disables the guardrail (turns are never bounded) for operators who want the old
all-or-nothing behavior.

## Change 3 — token counter in `llm/` (R1, R3, AC4)

New `llm/tokens.py` builds an offline-safe counter from a model name:

```python
def build_token_counter(model_name: str) -> Callable[[str], int]:
    """text -> token count for `model_name`; never raises, offline-safe.

    Tries tiktoken.encoding_for_model(model_name); on KeyError (unknown /
    non-OpenAI model) falls back to a default encoding; if tiktoken itself is
    unavailable or its BPE data cannot be loaded (e.g. offline first use),
    falls back to a deterministic char/CJK heuristic. The chosen encoder is
    built once and closed over (per-process, like the model/client)."""
```

- Placing token counting in `llm/` matches convention #7 ("`llm/` is the only
  layer touching provider SDKs / tokenizers; everything below stays
  provider-agnostic"). `services/` receives a plain `Callable[[str], int]`.
- **Offline safety (Q3, AC6):** tiktoken downloads BPE files on first use; the
  builder wraps encoder construction in try/except and degrades to a heuristic
  counter (`len(ascii)/4 + cjk_chars`, or similar) so neither production nor the
  test suite ever needs the network. The heuristic is also what unit tests for
  the fallback path assert against deterministically.
- Add `tiktoken` as a **direct** dependency in `pyproject.toml` (already in the
  lock via `openai`) rather than relying on the transitive edge.

## Change 4 — service + config wiring (R3)

`ChatService.__init__`:

```python
history_token_budget: int = 2000,                 # replaces history_char_budget
history_max_turn_fraction: float = 0.5,           # NEW guardrail knob
token_counter: Callable[[str], int] | None = None, # NEW; None -> build from model
```

- `self._token_counter = token_counter or build_token_counter(model.model_name)`.
  The injectable param mirrors the shipped `rewrite_model` testability pattern:
  tests pass a deterministic counter, production lets the service build the
  tiktoken one from the chat model.
- `_prepare_turn` calls
  `select_history_window(rows, budget=self._history_token_budget,
  measure=self._token_counter, per_turn_cap=round(budget * fraction))`.

`core/config.py` / `.env.example`:

| Old | New | Default | Meaning |
|-----|-----|---------|---------|
| `CHAT_HISTORY_CHAR_BUDGET=8000` | `CHAT_HISTORY_TOKEN_BUDGET` | `2000` | history token budget (≈ old 8000 chars for mixed content; Q4) |
| — | `CHAT_HISTORY_MAX_TURN_FRACTION` | `0.5` | per-turn cap as a fraction of the budget; `>= 1.0` disables the guardrail |

`deps.py::build_chat_service` passes the two new settings; production omits
`token_counter` so the service builds it from `CHAT_MODEL`.

Default budget rationale (Q4): 8000 chars of typical mixed CJK/English maps to
roughly 2000–4000 tokens; `2000` is chosen as a slightly conservative,
round default that keeps per-run input modest. Operators tune via env. (The
exact number is an operator knob, not a contract.)

## Data flow (unchanged shape)

`list_recent_for_session (≤ HISTORY_READ_LIMIT) → select_history_window
(token measure + per-turn cap) → to_message_history → run_stream
message_history`. The only changes are the cost function and the per-turn
bounding; `HISTORY_READ_LIMIT`, ordering, and the no-orphan-half-turn rule are
unchanged.

## Observability

Optional: extend the existing history/log line with `history_turns`,
`history_tokens` (the summed measured cost), and `truncated_turns` counts —
numbers only, never content (logging-guidelines). No new required log.

## Edge cases

| Case | Behavior |
|------|----------|
| Turn within cap | Included whole (as today, token-measured) |
| Single turn > cap | Truncated-with-marker to the cap; older turns still fill remaining budget |
| Newest turn > cap and budget small | Newest included truncated; may be the only turn (but never silently — marker present) |
| `fraction >= 1.0` | Guardrail off — turns never bounded (old all-or-nothing) |
| Unknown / non-OpenAI `CHAT_MODEL` | Fallback encoding; still deterministic |
| tiktoken unavailable / offline | Heuristic counter; never raises |
| First turn / stateless | No history assembled (unchanged) |

## Compatibility / rollback

- Breaking config: `CHAT_HISTORY_CHAR_BUDGET` is removed. Deployments must set
  `KB_CHAT_HISTORY_TOKEN_BUDGET` (documented in `.env.example`); single-user
  MVP makes this acceptable. `extra="ignore"` in `Settings` means a leftover
  `KB_CHAT_HISTORY_CHAR_BUDGET` is silently ignored (no crash), so rollout is
  safe.
- Rollback: set `KB_CHAT_HISTORY_MAX_TURN_FRACTION>=1.0` to disable the
  guardrail; revert the commit to restore the char budget. No migration, no
  reindex.

## Files touched (expected)

| File | Change |
|------|--------|
| `llm/tokens.py` | NEW: `build_token_counter(model_name)` (tiktoken + offline heuristic fallback) |
| `services/chat.py` | `select_history_window` gains `measure` + `per_turn_cap`; `_bound_turn` helper; `__init__` swaps to token budget + fraction + injectable counter; `_prepare_turn` wiring |
| `core/config.py` | replace `CHAT_HISTORY_CHAR_BUDGET` with `CHAT_HISTORY_TOKEN_BUDGET`; add `CHAT_HISTORY_MAX_TURN_FRACTION` |
| `.env.example` | replace the char budget var; add the fraction var |
| `api/deps.py` | pass the two new settings; omit `token_counter` |
| `pyproject.toml` | add `tiktoken` as a direct dependency |
| `tests/test_chat_service.py` | token-measure selection, guardrail truncation, persistence-intact, config wiring |
| `tests/test_tokens.py` (or existing llm tests) | counter: known model, unknown-model fallback, offline heuristic |

## Tradeoffs / rejected

- **Reinterpret `CHAT_HISTORY_CHAR_BUDGET` as tokens** (keep the name): rejected
  — the name would lie and silently change every deployment's effective window.
- **Count tokens inside `select_history_window`** (import tiktoken there):
  rejected — breaks the pure-function test style and pulls a tokenizer into the
  selection logic; injection keeps both clean.
- **Absolute per-turn token cap** instead of a fraction: viable, but a fraction
  scales with the budget and needs no separate calibration (Q2).
- **Summarize the oversized turn** instead of truncating: that is rolling
  summary (direction #2) — deliberately the next task, out of scope here.
