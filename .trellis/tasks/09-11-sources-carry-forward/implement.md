# Implementation plan

Persist per-run sources on the assistant message and re-emit the previous
run's sources as a citable first batch on follow-ups. Conventions:
`uv run <cmd>`; prompts are versioned files; docs in English; no user/
source text in logs; tests stay offline; defaults keep old call sites
byte-identical.

## Stage A — Schema (`models/chat.py` + migration)

- [ ] A1. `ChatMessage.sources: Mapped[list | None]` — `JSONB`, nullable,
      no default. Follow existing column style.
- [ ] A2. `alembic/versions/0008_chat_message_sources.py` (revises 0007):
      `add_column` nullable; `downgrade` drops. 0007 file shape.
- [ ] A3. `uv run alembic upgrade head` → `downgrade -1` → `upgrade head`
      against the dev DB; confirm via `information_schema`.

## Stage B — Collector + service (`agents/qa.py`, `services/chat.py`)

- [ ] B1. `SourceCollector.hits` property: flattened batches in collection
      order (no dedup).
- [ ] B2. `_carried_prefix(hits) -> list[ModelMessage]`: labeled synthetic
      pair rendering `format_context_blocks(hits, start=1)` under a
      "[Sources cited in the previous answer]" heading. Pure; module-level
      like `summary_prefix`.
- [ ] B3. `_prepare_turn`: when the feature is on and the newest assistant
      row has non-empty `sources`, carry them on `_ChatTurn` (and prepend
      `_carried_prefix` after the summary prefix).
- [ ] B4. `__init__`: keyword-only `carry_sources_forward: bool = False`.
- [ ] B5. `ask`: after `yield RunStartedEvent`, if carrying: seed
      `collector.append(carried)` and drain → yield the FIRST
      `SourcesEvent` batch before `run_stream`. Fresh batches continue
      numbering via `total_hits`.
- [ ] B6. `_persist_assistant_message(turn, *, content, run_id, sources)`:
      store `sources` as `SearchHit` JSON list (NULL when empty/feature
      off) in the existing transaction. Call site passes
      `collector.hits`.
- [ ] B7. `ask` docstring: carried first batch is the deliberate exception
      to "sources stay run-local"; `agent_run_finished` gains
      `carried_sources` count (numbers only).

## Stage C — Prompt (`agents/prompts/qa.md`)

- [ ] C1. Rule 7 carve-out: blocks under the carried-sources heading are
          legitimate citation targets; numbering continues across carried
          and fresh blocks. One sentence; no other prompt edits.

## Stage D — Config + wiring

- [ ] D1. `core/config.py`: `CHAT_SOURCES_CARRY_ENABLED: bool = True` with
          comments (runtime kill switch; off == today).
- [ ] D2. `.env.example`: `KB_CHAT_SOURCES_CARRY_ENABLED=true`.
- [ ] D3. `api/deps.py::build_chat_service`: pass
          `carry_sources_forward=settings.CHAT_SOURCES_CARRY_ENABLED`.

## Stage E — Tests (`tests/test_chat_service.py`, DB-marked)

- [ ] E1. AC1: DB test — after a retrieval turn, the assistant row's
          `sources` holds the StubRetriever hits in order.
- [ ] E2. AC2: next turn — first `sources` batch == carried hits (assert
          emitted events), fresh batch numbering continues after.
- [ ] E3. AC3: recorded histories show the labeled carried pair numbered
          `[1..k]` before fresh tool blocks starting `[k+1]`.
- [ ] E4. AC4: carried content/order identical to the previous run's
          retrieval (no dedup/renumber).
- [ ] E5. AC5: flag off — no `sources` write, no first batch, no preamble;
          pre-existing chat tests (no flag) pass unchanged.
- [ ] E6. AC6: NULL-sources prior turn / stateless → nothing carried.
- [ ] E7. AC7: `capture_logs` — no source/question text; settings-defaults
          pin.

## Stage F — Regression gate

- [ ] F1. `uv run alembic upgrade head && uv run ruff check && uv run mypy
          && uv run pytest` all green (offline suite, live markers
          excluded). Migration round-trip verified (A3). Search-gate
          drift-guard unaffected (no search settings).

## Stage G — Manual E2E (optional, live)

- [ ] G1. Two-turn session: turn 1 grounds an answer with citations; turn 2
          asks about "the cited source" without re-retrieving it — confirm
          the carried batch appears first in `sources` and the answer cites
          the carried numbers. Toggle the flag off to confirm old behavior.

## Validation commands

```bash
uv run alembic upgrade head
uv run ruff check
uv run mypy
uv run pytest
```

## Rollback points

- Runtime: `KB_CHAT_SOURCES_CARRY_ENABLED=false` → today's behavior (and
  the constructor default is off). Full revert: revert commit +
  `alembic downgrade -1`. No reindex.

## Review gates

- After Stage B: confirm carried numbering == previous run's citations
  (no dedup/renumber anywhere) and the first batch precedes `run_stream`.
- After Stage E: confirm no source/question text in logs and the disabled
  path is byte-identical (AC5), including log surface.

## Dependency note

- Builds on shipped: query rewrite (follow-up self-containment), token
  window + guardrail, rolling summary (0007 / `summary_prefix` patterns).
  Closes the grounding half of issue direction #3; the multi-turn issue
  then has all four directions addressed.
