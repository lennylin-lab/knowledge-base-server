# Implementation plan

Single-concern tightening: vector rescue fires only when the BM25 leg is empty.
No new Settings, no calibration, no reindex. Conventions: `uv run <cmd>`; rescue
logic stays in `rag/`; docs in English.

## Stage A — Helper: BM25-empty precondition

- [ ] A1. `rag/retriever.py::filter_vector_rows_with_rescue`: add keyword-only
      `bm25_leg_empty: bool`. After the primary tier empties (`primary or not
      rows` returns as today), add an early `return [], 0` when
      `not bm25_leg_empty`, before the disable/trigger/window logic. Update the
      docstring to state rescue is the lexical-failure backstop (fires only when
      BM25 has no survivors).
- [ ] A2. Confirm the guard order composes with existing preconditions
      (disable sentinels, on-domain trigger) — all still short-circuit to
      `([], 0)`; `bm25_leg_empty=True` is bit-identical to prior behavior.

## Stage B — Caller wiring

- [ ] B1. `rag/retriever.py::Retriever.retrieve`: pass
      `bm25_leg_empty=(len(kept_es_hits) == 0)` into
      `filter_vector_rows_with_rescue`. `kept_es_hits` is already computed just
      above the vector gate — no reordering, no extra awaits.

## Stage C — Tests

- [ ] C1. `tests/test_search_gates.py`: update existing rescue unit cases to
      pass `bm25_leg_empty=True` (preserve current assertions). Add: with
      `bm25_leg_empty=False`, an emptied primary tier + on-domain leg_min
      returns `([], 0)` (no rescue). Keep the drift-guard case unchanged (no new
      Settings).
- [ ] C2. `tests/test_retriever.py`: integration —
      (a) BM25 non-empty + vector primary emptied (all rows > 0.45, leg_min ≤
          trigger) → `vector_rescued == 0`, no vector-only keys in results;
      (b) BM25 empty + on-domain short-keyword leg → rescued as before.
- [ ] C3. (Optional) `tests/test_search_api.py`: if a representative fixture
      corpus reproduces the topical-neighbor shape, assert a `python`-like query
      returns only the lexically-matched docs. Otherwise rely on C1/C2 + manual
      E2E.

## Stage D — Regression gate

- [ ] D1. `uv run ruff check && uv run mypy && uv run pytest` (offline suite,
      `live_llm` excluded) all green. Confirm task 09-10's out-of-domain tests
      still pass (BM25-empty + off-domain trigger path unchanged).

## Stage E — Manual E2E (real corpus)

- [ ] E1. `GET /api/v1/search?q=python&limit=10` → only Python/FastAPI chunks;
      Vue 3 / Flutter / PostgreSQL gone (AC1). Spot-check a vocabulary-mismatch
      short keyword (BM25 empty) still returns semantic neighbors (AC3).
- [ ] E2. Re-run the five archived out-of-domain queries → still 0 items (AC4).

## Validation commands

```bash
uv run ruff check
uv run mypy
uv run pytest
```

## Rollback points

- Pass `bm25_leg_empty=True` unconditionally at the call site (restores 09-10
  behavior) or revert the commit. No Settings, no migration, no reindex.

## Dependency note

- Assumes task 09-10-irrelevant-query-noise-gates is in place (on-domain trigger
  `SEARCH_VECTOR_RESCUE_TRIGGER_MAX_DISTANCE`, identity-group coverage). This
  task composes an additional AND-condition onto that rescue precondition.
