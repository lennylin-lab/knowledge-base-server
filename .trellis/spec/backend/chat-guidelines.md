# Chat Path Guidelines

> Conversation-history and chat-input contracts for `knowledge-base-server`:
> session history assembly, budgeting, and preprocessing that shapes what the
> QA model sees. The retrieval-input side of the chat path (query rewriting)
> is documented in [Search Guidelines](./search-guidelines.md) — the history
> window feeds the rewrite, which feeds the retriever.

---

## Scenario: Token-based history budget + long-turn guardrail

### 1. Scope / Trigger

- Trigger: cross-layer contract change — a **breaking** Settings rename
  (`CHAT_HISTORY_CHAR_BUDGET` → `CHAT_HISTORY_TOKEN_BUDGET`), a new direct
  dependency (`tiktoken`), a new `llm/` module, and a signature change to
  `select_history_window` (2026-09-11, task `09-11-token-history-budget`).
  Any change to history assembly or tokenization must re-verify this
  scenario. The planned rolling-summary task (issue direction #2) will build
  directly on this contract.

### 2. Signatures

- `llm/tokens.py::build_token_counter(model_name: str) -> Callable[[str], int]`
  — text → token count; **never raises** (convention #7: tokenizers live in
  `llm/` only; `services/` receives a plain callable).
- `services/chat.py::select_history_window(messages, *, budget, measure,
  per_turn_cap) -> list[ChatMessage]` — **pure**; the cost measure is
  injected, never imported inside.
- `ChatService(..., *, history_token_budget: int = 2000,
  history_max_turn_fraction: float = 0.5,
  token_counter: Callable[[str], int] | None = None)` — `None` → the service
  builds the tiktoken counter from the chat model (injectable for tests, same
  pattern as `rewrite_model`).

### 3. Contracts

- Selection walk unchanged in shape: whole turns, newest-first, no orphan
  half-turns, `HISTORY_READ_LIMIT` unchanged — only the cost measure changed
  (chars → tokens: `measure(user.content) + measure(assistant.content)`).
- Guardrail: `per_turn_cap = round(budget * history_max_turn_fraction)`. A
  turn costing more than the cap has its **history copy** truncated to the
  cap with the visible `TRUNCATION_MARKER`, so the elision is observable to
  the model and older turns still fit the remaining budget (no silent
  single-turn collapse). `fraction >= 1.0` → `_per_turn_cap()` returns the
  `0` sentinel = guardrail off (old all-or-nothing behavior).
- **Persistence faithfulness**: `_bound_turn` returns fresh, detached copies
  and passes the original row through unmodified when it fits — truncated
  content exists ONLY in the assembled `message_history`; persisted
  `ChatMessage` rows always keep full content. (Load-bearing: a mutation of
  an ORM row would flush on the session's next commit.)
- Counter fallback chain (never raises): `tiktoken.encoding_for_model`
  (`KeyError` on unknown names is a pure registry lookup — no I/O) → default
  encoding (`o200k_base`) → any load failure (cold-cache BPE fetch, offline)
  → deterministic char/CJK heuristic. `encode` failures fall back at count
  time too; `disallowed_special=()` so pasted special-token text counts as
  content.
- **Lazy counter construction**: the encoder is built on the FIRST history
  assembly (`_token_measure()`), not in `__init__` — tiktoken's cold-cache
  `get_encoding` performs an un-timed network fetch, which would make every
  stateless test with a fake model name hit the network. Memoized per
  service thereafter (`build_chat_service` is `lru_cache(1)`, so once per
  process in production). Worst case: a one-time blocking fetch that
  degrades permanently to the heuristic — the never-raises contract holds.
- Settings (breaking rename; `extra="ignore"` makes a leftover
  `KB_CHAT_HISTORY_CHAR_BUDGET` var silently ignored during rollout):
  `CHAT_HISTORY_TOKEN_BUDGET=2000` (≈ the old 8000 chars for mixed
  CJK/English; operator knob), `CHAT_HISTORY_MAX_TURN_FRACTION=0.5`.
- Log event `history_selected` carries numbers only — `session_id`,
  `history_turns`, `history_tokens`, `truncated_messages` — never content
  (logging-guidelines).

### 4. Validation & Error Matrix

- tiktoken cold-cache fetch fails (offline) → caught at build AND count time
  → heuristic counter; tests must never depend on this fetch (see §6).
- Unknown / non-OpenAI `CHAT_MODEL` → `KeyError` → default encoding, still
  deterministic.
- Bounded turn still exceeds the remaining budget → it is still DROPPED
  (bounding bounds cost; it does not guarantee admission — the walk's fit
  check runs after bounding).
- Trimming an oversized turn: content allowance is `cap - 2·marker` per side
  (sequential trims), so a both-sides-bounded turn measures exactly ≤ cap in
  one deterministic pass.
- Mutating ORM rows in `_bound_turn` → forbidden (would flush persisted
  truncation).

### 5. Good/Base/Bad Cases

- Good: one long pasted-document turn + several short turns → the long turn
  arrives truncated-with-marker, older turns still reach the model.
- Base: every turn within cap → identical to the old walk, token-measured.
- Bad: keeping the `CHAR_BUDGET` name with token semantics (the name would
  lie and silently change every deployment's window — rejected); importing
  tiktoken inside `select_history_window` (breaks purity + offline tests);
  truncating persisted rows; eager counter construction in `__init__`.

### 6. Tests Required

- `tests/test_history_window.py`: fake injected measures (`len`, word-count,
  CJK-aware); char-fit-but-token-over drops the oldest turn (AC1);
  oversized-turn truncated-with-marker + older turn survives (AC2); no ORM
  mutation + persisted rows keep full content (AC3, DB-verified); cap-0
  disables; bounded-still-over-budget drops.
- `tests/test_tokens.py`: all tiktoken interactions monkeypatched — the
  suite NEVER loads a real encoder (a real load can't be made
  offline-deterministic); unknown-model fallback; forced failure →
  heuristic; never raises.
- `tests/test_chat_service.py`: service-level AC1–AC3 with injected
  counters; Settings-defaults pin (2000 / 0.5). Rule: every history-
  assembling `ChatService` in tests injects a `token_counter`.
- **No-network proof** (AC6): full suite re-run under a socket-blocking
  plugin (loopback allowed) with an empty `TIKTOKEN_CACHE_DIR` → zero
  outbound connection attempts.

### 7. Wrong vs Correct

#### Wrong

```python
# Eager + eager-priced: __init__ builds the encoder (network on cache miss),
# and the window silently starves on one oversized turn.
self._encoder = build_token_counter(model.model_name)   # in __init__
window = select_history_window(rows, budget=self._budget,
                               measure=lambda s: len(s))  # chars ≠ tokens
```

#### Correct

```python
# Lazy, memoized, injected measure; oversized turns bounded with a marker.
def _token_measure(self) -> Callable[[str], int]:
    if self._token_counter is None:
        self._token_counter = build_token_counter(self._model_name)  # never raises
    return self._token_counter

window = select_history_window(
    rows, budget=self._history_token_budget,
    measure=self._token_measure(), per_turn_cap=self._per_turn_cap(),
)
```

---

## Scenario: Rolling conversation summary folds evicted turns

### 1. Scope / Trigger

- Trigger: cross-layer contract change — schema addition on `chat_sessions`
  (migration `0007`: `rolling_summary`, `summarized_through_id`), a new
  repository method, a new toolless agent, and post-answer service
  orchestration (2026-09-11, task `09-11-rolling-history-summary`). Any
  change to history assembly, fold logic, or summary injection must
  re-verify this scenario.

### 2. Signatures

- `agents/conversation_summary.py::build_conversation_summary_agent(model)
  -> Agent[None, str]` and `render_fold_prompt(turn_text_pairs, *,
  max_tokens)` — toolless, imports pydantic-ai + `qa.load_prompt` ONLY.
  `agents/` cannot import `models/`, so prompt rendering takes plain
  `(user_text, assistant_text)` pairs, never ORM rows.
- `ChatSessionRepository.update_rolling_summary(session_id, *, summary,
  through_id)` — one `update(ChatSession)` statement, caller owns the txn.
- `services/chat.py::select_turns_to_fold(...)` — pure;
  `summary_prefix(summary)` — the labeled synthetic request/response pair.
- `ChatService(..., *, summary_model: Model | None = None,
  summary_max_tokens: int = 400)` — `None` disables (same injected-model
  pattern as `rewrite_model`).

### 3. Contracts

- Watermark semantics: `summarized_through_id` is compared by uuid7 ID
  ORDERING (`assistant.id > watermark`), never by membership — a later
  budget widening that re-admits a folded turn into the window must NOT
  re-fold it (the watermark never moves backward).
- Fold boundary mirrors the prelude's reservation: a turn is folded as soon
  as the next prelude (`budget - min(measure(summary), summary_max_tokens)`)
  would stop showing it. The one-turn transient while the fold itself
  changes the reservation is accepted and documented in
  `select_turns_to_fold`.
- Fold input is bounded through the `_bound_turn` guardrail copies before
  rendering (a 50k-token pasted document cannot become a 50k-token fold
  prompt); persisted rows stay whole.
- Empty model output is a FAILED fold (`rolling_summary_update_failed`,
  `error_class="EmptySummary"`, watermark held) — an empty summary would
  erase the memory it was meant to extend.
- Maintenance timing (persist-time, off the answer critical path): runs
  after `ask`'s streaming try/except and before `DoneEvent`; `latency_ms`
  is fixed BEFORE the call so `DoneEvent` keeps meaning answer latency.
  Phasing: read txn CLOSED → `agent.run` (no open txn) → write txn. The
  body catches `Exception` only and never raises into `ask`
  (`BaseException` propagates).
- Injection: labeled synthetic pair (`SUMMARY_PREFIX_LABEL` + acknowledgment)
  prepended to the in-window history; never persisted.
- Budget reservation: `_turn_budget = max(budget -
  min(measure(summary), summary_max_tokens), 0)`, applied only when a
  summary is present — the summary cannot be evicted by turn growth.
- Settings: `CHAT_ROLLING_SUMMARY_ENABLED=True` (gates `summary_model`
  injection in deps), `CHAT_SUMMARY_MAX_TOKENS=400`.
- Logs: `rolling_summary_injected` (summary_length, reserved_tokens; only
  when present) and `rolling_summary_update_failed` (error_class only).
  NEVER summary or question text.
- Disabled (`summary_model=None`) / stateless: no column read, no model
  call, no injection, no new log events — byte-identical to cliff eviction.

### 4. Validation & Error Matrix

- Maintenance LLM failure → warning with error_class, watermark and summary
  unchanged → folding retries next turn; the turn already succeeded and can
  never become a terminal `ErrorEvent`.
- Empty summary output → `EmptySummary`, watermark held.
- Bounded fold still over the remaining budget → nothing folded this turn
  (deferred; retried next turn).

### 5. Good/Base/Bad Cases

- Good: long session, oldest turns evicted → early-turn facts remain
  answerable via the injected labeled summary.
- Base: nothing evicted yet → no summary, identical to token-window
  behavior.
- Bad: read-time folding (prelude latency + LLM call near the read txn —
  rejected); recomputing the whole summary from all evicted turns each turn
  (O(history), non-incremental — rejected); `SystemPromptPart` framing
  (collides with the agent's own instructions — rejected); membership-test
  watermark (re-folds on window widening).

### 6. Tests Required

- `tests/test_chat_service.py` (AC1–AC7, DB-marked): evicted turn folded
  into the stored summary; summary injected ahead of window turns in the
  recorded histories; fold is incremental (call count / watermark advance);
  disabled path pins cliff-shaped histories + `NULL`/`NULL` columns + zero
  summary log events; scripted maintenance failure still reaches
  `DoneEvent`; persisted rows unchanged; logs carry no summary/question
  text; Settings defaults pinned.
- `tests/test_history_window.py`: pure `select_turns_to_fold` coverage
  including never-refold-on-window-widening; `summary_prefix` shape.

### 7. Wrong vs Correct

#### Wrong

```python
# Membership watermark: widening the window later re-admits an already-
# folded turn and the summary folds it a second time.
if turn.assistant.id not in summarized_ids:
    fold(turn)
```

#### Correct

```python
# Monotone id-ordering watermark: once folded, never re-folded, whatever
# the window does afterwards.
if turn.assistant.id > summarized_through_id:
    fold(turn)
```

---

**Language**: All documentation is written in **English**.
