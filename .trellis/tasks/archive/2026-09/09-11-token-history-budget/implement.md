# Implementation plan

Swap the chat history budget from characters to tokens with an injected
counter, add a per-turn truncation guardrail, and migrate the Settings.
Conventions: `uv run <cmd>`; tokenizer lives in `llm/`; `select_history_window`
stays pure; docs in English; no user text in logs; tests stay offline.

## Stage A — Token counter (`llm/tokens.py`)

- [ ] A1. `pyproject.toml`: add `tiktoken` to `[project].dependencies`
      (already in the lock via `openai`); `uv sync` to confirm.
- [ ] A2. `llm/tokens.py::build_token_counter(model_name: str) -> Callable[[str], int]`:
      try `tiktoken.encoding_for_model(model_name)`; on `KeyError` use a
      default encoding (e.g. `o200k_base`/`cl100k_base`); wrap encoder
      construction so any failure (offline first-use, missing BPE) degrades to
      a deterministic char/CJK heuristic. Build the encoder once and close over
      it. The function must NEVER raise.
- [ ] A3. Keep the heuristic documented and self-contained so the fallback
      path is unit-testable without network.

## Stage B — Selection + guardrail (`services/chat.py`)

- [ ] B1. `select_history_window`: add keyword-only `measure: Callable[[str], int]`
      and `per_turn_cap: int`; replace `len(...)` cost with `measure(...)`.
      Preserve the newest-first, whole-turn, no-orphan-half-turn walk.
- [ ] B2. `_bound_turn(user, assistant, cap, measure)`: when a turn's measured
      cost exceeds `cap`, return detached bounded carriers (truncated content +
      visible marker) and the recomputed capped cost; NEVER mutate ORM rows.
      Trim by tokens (greedy/binary) to `cap - marker_cost`.
- [ ] B3. Wire the guardrail into the walk (bound oversized turns before the
      fit check) so older turns still receive the remaining budget.
- [ ] B4. `ChatService.__init__`: replace `history_char_budget` with
      `history_token_budget: int = 2000`; add `history_max_turn_fraction: float
      = 0.5` and `token_counter: Callable[[str], int] | None = None`. Set
      `self._token_counter = token_counter or build_token_counter(model.model_name)`.
- [ ] B5. `_prepare_turn`: call `select_history_window(rows,
      budget=self._history_token_budget, measure=self._token_counter,
      per_turn_cap=round(self._history_token_budget * self._history_max_turn_fraction))`.
- [ ] B6. (Optional) history log line: add `history_turns`, `history_tokens`,
      `truncated_turns` (numbers only, no content).

## Stage C — Config migration

- [ ] C1. `core/config.py`: remove `CHAT_HISTORY_CHAR_BUDGET`; add
      `CHAT_HISTORY_TOKEN_BUDGET: int = 2000` and
      `CHAT_HISTORY_MAX_TURN_FRACTION: float = 0.5` with comments (state the
      breaking change + the `>= 1.0` disable sentinel).
- [ ] C2. `.env.example`: replace `KB_CHAT_HISTORY_CHAR_BUDGET` with
      `KB_CHAT_HISTORY_TOKEN_BUDGET=2000`; add
      `KB_CHAT_HISTORY_MAX_TURN_FRACTION=0.5`.
- [ ] C3. `api/deps.py::build_chat_service`: pass `history_token_budget=` and
      `history_max_turn_fraction=`; omit `token_counter` (built from model).
- [ ] C4. Grep for any remaining `CHAR_BUDGET` / `history_char_budget`
      references (src + tests + docs) and update — leave none dangling (AC5).

## Stage D — Tests

- [ ] D1. `tests/test_chat_service.py`: update `_persisted_service` and any
      construction to the new `budget=`/counter params; where token behavior is
      asserted, inject a deterministic `token_counter` (e.g. word-count) so
      selection is exact and offline.
- [ ] D2. Selection (AC1): a session that fits the char budget but exceeds the
      token budget drops its oldest turn(s) under the injected measure.
- [ ] D3. Guardrail (AC2/AC3): an oversized turn appears truncated-with-marker
      in `message_history`, an older fitting turn survives, and the persisted
      rows keep FULL content (query the test DB).
- [ ] D4. Counter (AC4): `tests/test_tokens.py` — known model returns a
      tiktoken count; unknown model falls back to default encoding; forced
      tiktoken failure falls back to the heuristic; never raises.
- [ ] D5. Reuse/extend the existing budget tests
      (`test_history_budget_drops_oldest_complete_turns`,
      `test_history_read_limit_...`) to the token measure — keep their intent.

## Stage E — Regression gate

- [ ] E1. `uv run ruff check && uv run mypy && uv run pytest` (offline suite,
      `live_llm` excluded) all green, WITH NO network access (AC6). Confirm the
      search-gate drift-guard is unaffected (no search settings changed).

## Stage F — Manual E2E (optional, live)

- [ ] F1. A session with one long pasted-document turn plus several short
      turns: confirm the long turn arrives truncated-with-marker and earlier
      turns still reach the model (no silent single-turn collapse). Toggle
      `KB_CHAT_HISTORY_MAX_TURN_FRACTION=1.0` to confirm the guardrail disables.

## Validation commands

```bash
uv sync
uv run ruff check
uv run mypy
uv run pytest
```

## Rollback points

- `KB_CHAT_HISTORY_MAX_TURN_FRACTION>=1.0` disables the guardrail at runtime;
  revert the commit to restore the char budget. `extra="ignore"` means a
  leftover `KB_CHAT_HISTORY_CHAR_BUDGET` env var is harmless during rollout.

## Review gates

- After Stage B: confirm `_bound_turn` never mutates ORM rows (R2/AC3) and
  `select_history_window` remains a pure function (measure injected).
- After Stage C: grep-clean of the old setting name across the repo (AC5).
- After Stage D: confirm no test requires network (tiktoken heuristic/injected
  counter) — AC6.

## Dependency note

- Independent of, but complementary to, the shipped query-rewrite task. Feeds
  the next task (rolling summary, direction #2), which will replace this
  guardrail's truncation with summarized carry-forward of evicted turns.
