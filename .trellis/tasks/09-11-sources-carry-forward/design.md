# Design: Carry prior-run sources into follow-up turns

Persist each run's retrieval hits on its assistant message; on follow-up
turns re-emit the previous run's sources as the first citable `sources`
batch and a labeled leading context pair, with fresh retrieval numbering
continuing after them. Reuses the shipped patterns: additive migration
(0007 shape), labeled synthetic pair (`summary_prefix`), feature-flag
wiring (`rewrite_model`/`summary_model`), pure helpers unit-tested offline.

## Data flow (new edges bracketed)

```
prelude (_prepare_turn): rows = list_recent_for_session(...)
  → [ carried = newest assistant row .sources  (may be None/empty) ]
  → select_history_window(...) → _summary_prefix? + to_message_history
  → [ carry_prefix = _carried_prefix(carried)  (labeled synthetic pair) ]

ask():
  yield RunStartedEvent
  → [ if carried: collector.append(carried); drain → yield SourcesEvent(carried) ]
      # collector seeding makes fresh blocks number from len(carried)+1
  → history = history + [carry_prefix]        # leading context, after summary prefix
  → rewrite → run_stream(...)
  → fresh batches drain as today (numbering continues)
  → _persist_assistant_message(turn, content, run_id,
          sources=[collector hits flattened])   # NEW param, same txn
```

## Storage (R1/D3) — migration 0008

`chat_messages.sources: JSONB, nullable` — the run's hits as a JSON array
of `SearchHit.model_dump(mode="json")`, in retrieval (batch-then-rank)
order. Assistant rows only; user rows and failures leave `NULL`.

- Full hits, not keys: replay must be exact (content, titles, scores) with
  zero extra reads; volume is bounded by per-run retrieval (`limit` ×
  batches, typically ≤ 8–16 hits). Rehydration-by-key would add a store
  read in the prelude and could 404 for deleted documents — rejected.
- No ORM-level default; only `services/chat.py` writes the column.
- Migration `0008_chat_message_sources` (revises 0007): `add_column`
  nullable, `downgrade` drops. 0007 file shape.

## Collector seeding + first batch (R2)

`ask`, immediately after `yield RunStartedEvent`:

```python
if turn is not None and carried:
    collector.append(carried)          # on_append → pending
    for batch in _drain(pending):
        yield SourcesEvent(items=batch)   # FIRST sources batch
```

- Seeding before `run_stream` makes `format_context_blocks`'s
  `start=total_hits+1` number every fresh batch after the carried ones —
  prompt numbering == client numbering, no renumber, no dedup (R1/AC4).
- `SourceCollector` gains a `hits` property (flattened batches) so
  `_persist_assistant_message` can store the run's sources without new
  plumbing.

## Leading context pair (R2) — `_carried_prefix`

Same shape as `summary_prefix` (labeled synthetic request/response pair),
prepended AFTER the summary prefix, BEFORE the in-window history:

```python
def _carried_prefix(hits: list[SearchHit]) -> list[ModelMessage]:
    blocks = format_context_blocks(hits, start=1)
    return [ModelRequest(parts=[UserPromptPart(
                content=f"[Sources cited in the previous answer]\n{blocks}")]),
            ModelResponse(parts=[TextPart(
                content="Noted — I'll treat these numbered sources as citable context.")])]
```

- Numbered `[1..k]` — identical numbers the previous answer cited (AC4).
- Pure function of the hits; unit-testable offline like
  `select_turns_to_fold`.
- Not persisted, not part of history budget accounting (same class as tool
  results per the token-budget task's non-goals).

## Prompt contract (`agents/prompts/qa.md`)

Rule 7 gains an explicit carve-out (wording at implementation time, keep it
one sentence): blocks under the "[Sources cited in the previous answer]"
heading are legitimate citation targets alongside fresh `search_knowledge`
blocks; numbering continues across both. No other prompt changes.

## Service + config wiring

`ChatService.__init__`: keyword-only `carry_sources_forward: bool = False`
(default off keeps every existing call site byte-identical — same pattern
as the other features' constructor params).

`core/config.py` / `.env.example`: `CHAT_SOURCES_CARRY_ENABLED: bool = True`
(comments: kills the feature at runtime; off == today byte-for-byte).
`api/deps.py::build_chat_service` passes
`carry_sources_forward=settings.CHAT_SOURCES_CARRY_ENABLED`.

`_persist_assistant_message(turn, *, content, run_id, sources)` stores
`sources` JSON (or `NULL` when empty/disabled) in the same transaction —
no new failure surface, no post-answer step needed.

## Docstring/log updates

- `ask`'s docstring: history still never enters `sources`; a carried FIRST
  batch (previous run's sources) is the one deliberate addition — citations
  remain run-local in numbering.
- `agent_run_finished` may gain `carried_sources=len(carried)` (count only,
  no text — logging-guidelines).

## Edge cases

| Case | Behavior |
|------|----------|
| First turn / no prior assistant msg | Nothing carried (no rows) |
| Prior assistant msg with NULL/empty sources | Nothing carried |
| Prior run errored (no assistant row persisted) | Nothing carried |
| Flag off | No write, no emission, no preamble — byte-identical |
| Stateless service | No DB, nothing carried (and nothing persisted) |
| Prior sources from before this feature (old sessions) | NULL → nothing carried |
| Fresh retrieval returns 0 hits | Carried blocks still [1..k]; no fresh batch |

## Compatibility / rollback

- Additive nullable column; existing sessions unaffected (NULL). Migration
  downgrade drops the column.
- Rollback: `KB_CHAT_SOURCES_CARRY_ENABLED=false` at runtime (constructor
  default is also off), or revert the commit + `alembic downgrade -1`.

## Files touched (expected)

| File | Change |
|------|--------|
| `models/chat.py` | `ChatMessage.sources: Mapped[list | None]` (JSONB nullable) |
| `alembic/versions/0008_chat_message_sources.py` | NEW migration |
| `agents/qa.py` | `SourceCollector.hits` property |
| `agents/prompts/qa.md` | rule-7 carve-out for the carried-sources preamble |
| `services/chat.py` | `_carried_prefix`, carried read in `_prepare_turn`, seeding + first batch in `ask`, `sources` param in `_persist_assistant_message`, `carry_sources_forward` param, docstring/log updates |
| `core/config.py`, `.env.example` | `CHAT_SOURCES_CARRY_ENABLED` |
| `api/deps.py` | pass the flag |
| `tests/test_chat_service.py` | AC1–AC6 coverage (DB-marked) |

## Tradeoffs / rejected

- **Chunk-keys + rehydration** storage: adds a prelude store read and breaks
  on deleted documents; full-hit JSON is bounded and replay-exact (D3).
- **Carry all session sources**: unbounded prompt growth, dedup/selection
  policy needed — deferred (D2).
- **Emit carried batch via a synthetic tool result**: would fabricate a
  tool-call message in history; the labeled pair is the established,
  honest mechanism (`summary_prefix` precedent).
- **Count carried blocks against the history token budget**: they are
  prompt context of the same class as tool results (token-budget task
  non-goal); budgeting them would couple two features for no measured need.
