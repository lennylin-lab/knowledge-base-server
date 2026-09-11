# Carry prior-run sources into follow-up turns

## Goal

Ground multi-turn follow-ups in what the assistant actually cited before:
persist each run's retrieval sources on its assistant message, and on
follow-up turns re-emit the previous run's sources as **citable** numbered
blocks ahead of fresh retrieval, so follow-up answers stay grounded in (and
can correctly cite) the documents already in play instead of guessing about
citations they can no longer see. This is the grounding half of direction #3
of the multi-turn hallucination-risks issue.

## Background — diagnosis (source-verified)

- **The dangling-citation vector.** The previous assistant answer (its text
  includes `[n]` citation handles) is replayed verbatim in `message_history`
  on every later turn (`services/chat.py::to_message_history`), but the
  sources behind those handles are ephemeral: `agents/qa.py::SourceCollector`
  accumulates hits per run, the service streams them once as `SourcesEvent`s,
  and nothing is persisted (`ChatMessage` has only content/run_id —
  `models/chat.py`). A follow-up like "详细讲讲你引用的那个方案" refers to a
  source the model can no longer see; it must re-search (may miss — the
  rewritten follow-up query returns different chunks) or hallucinate.
- **Prompt rule interaction.** `agents/prompts/qa.md` rule 7 currently
  restricts `[n]` citations to blocks returned by `search_knowledge`; the
  carried-sources preamble must be added as a legitimate citation target.
- **Numbering mechanism.** `format_context_blocks(hits, start=...)` numbers
  from `collector.total_hits + 1`; seeding the collector with the carried
  hits makes fresh blocks continue after them, keeping prompt numbering ==
  client numbering (clients concatenate `sources` batches; numbering is
  run-unique). The SSE contract (order/vocabulary) is otherwise unchanged.
- **Precedent available.** Toggled feature wiring (`CHAT_QUERY_REWRITE_ENABLED`
  → `rewrite_model`, `CHAT_ROLLING_SUMMARY_ENABLED` → `summary_model`),
  additive nullable-column migrations (0007), the labeled synthetic-pair
  preamble (`summary_prefix`), and never-raise post-answer steps are shipped
  patterns to reuse. `SearchHit` (`schemas/search.py`) is pydantic — JSON
  serializable for persistence.

## Requirements

### R1 — Source persistence

- Each run's retrieved sources are persisted on the run's assistant message
  (new nullable JSONB column `chat_messages.sources`, migration 0008), as
  the full `SearchHit` list in retrieval order — the exact numbered blocks
  the answer cited. No dedup, no filtering: replay must preserve the
  original `[1..N]` mapping.
- User messages and error paths persist nothing new (sources only on a
  successfully persisted assistant message).

### R2 — Citable re-emission on follow-ups (D1)

- On a turn whose session's most recent assistant message carries sources,
  the service: (1) emits those sources as the follow-up run's FIRST
  `sources` batch, right after `RunStartedEvent` and before any fresh
  batch; (2) seeds the collector so fresh retrieval numbering continues
  after them; (3) prepends a labeled synthetic pair carrying the same
  blocks (numbered `[1..k]`) as leading context, so the model can read and
  cite them; (4) `qa.md` gains an explicit rule that carried-source blocks
  are legitimate citation targets.
- Scope (D2): ONLY the immediately previous assistant turn's sources — no
  accumulation across the session. More than one prior turn of sources is
  a future task.
- The SSE event order/vocabulary, citation uniqueness, and the client's
  sources-batch handling are unchanged; the "sources stay run-local" note
  in `ask`'s docstring is updated to describe the carried first batch.

### R3 — Faithfulness & hygiene

- Persisted `ChatMessage.content` stays the answer text; `sources` is
  additional derived state. `to_message_history` and the rolling summary
  are unchanged by this feature.
- No user/summary/source text in logs (lengths/counts only). The run's
  `agent_run_finished` log may gain a `carried_sources` count.

### R4 — Operability & failure isolation

- A Settings flag (default on) disables the feature; when off: no
  `sources` write, no re-emission, no preamble, no seeding — byte-identical
  to today, including the log surface. Leftover rows with persisted sources
  are simply not carried.
- Old sessions (pre-feature, `sources` NULL) and stateless mode carry
  nothing. A prior turn that errored before persisting carries nothing.
- Injection is pure local computation on rows already read in the prelude —
  no new I/O, no LLM call, cannot raise into the stream.

## Acceptance Criteria

- [ ] AC1: After a multi-turn session turn with retrieval, the assistant
      message row's `sources` column holds the run's hits in retrieval
      order (verified against the test DB with StubRetriever).
- [ ] AC2: On the next turn, the carried sources are re-emitted as the
      FIRST `sources` batch (after `run_started`, before fresh batches),
      and fresh retrieval numbering continues after them (asserted via
      recorded sources events + the collector/`format_context_blocks`
      numbering).
- [ ] AC3: The model's prompt contains the carried blocks in a labeled
      leading pair numbered `[1..k]`, and fresh tool-result blocks start at
      `[k+1]` (asserted via the scripted model's recorded histories).
- [ ] AC4: Numbering fidelity — the carried blocks carry the same content
      and order as the previous run's citations (no dedup/renumber).
- [ ] AC5: With the flag disabled: no `sources` write, no first batch, no
      preamble; byte-identical to current behavior (existing chat tests
      unchanged and green).
- [ ] AC6: Old sessions (NULL `sources`), user messages, and stateless mode
      carry nothing.
- [ ] AC7: Full offline suite + `ruff` + `mypy` clean; migration up/down
      verified; no source/question text in logs.

## Out of scope

- Re-tuning retrieval gates or rewriting (measured and closed — archived
  `09-11-followup-noise-eval`).
- Carrying more than the immediately previous run's sources (D2); cross-
  session memory; summarizing evicted turns' sources.
- Any client/API schema change: `SearchHit` and `SourcesEvent` shapes are
  unchanged — carried sources reuse the existing wire schema.
- Rolling-summary changes.

## Decisions (resolved)

- D1 (MVP semantics — user-confirmed): **citable re-emission** (a), over
  context-only (b) and retrieval-side union (c).
- D2 (scope — user-confirmed): **only the immediately previous run's
  sources**; accumulation deferred.
- D3 (storage — design): full `SearchHit` JSON list on the assistant row
  (no key-rehydration round trip; bounded by per-run retrieval volume).
